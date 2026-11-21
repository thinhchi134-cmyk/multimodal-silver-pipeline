#!/usr/bin/env python3
"""Read-only Silver quality audit + safe structured export for handoff.
Source Bronze, Silver JSONL and search indexes are never modified.
Output directory MUST be new; report is evidence, not replacement Silver.
"""
from __future__ import annotations
import argparse, collections, csv, hashlib, json, re, sqlite3, sys
from pathlib import Path, PureWindowsPath

VERSION = 'silver-finalize-1.0.0'
IMAGE_EXT={'.jpg','.jpeg','.png','.tif','.tiff','.webp','.bmp','.jfif'}
TABLE_EXT={'.csv','.tsv','.xlsx','.xlsm'}
SPECIAL={'.dwg':'CAD extraction required', '.ai':'Adobe Illustrator extraction required',
         '.rvt':'Revit API or IFC export required', '.rar':'Archive manifest only',
         '.zip':'Archive manifest only', '.7z':'Archive manifest only'}

def dump(path, obj):
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')

def jsonline(f, obj): f.write(json.dumps(obj,ensure_ascii=False,default=str)+'\n')

def clean_name(v):
    return re.sub(r'\s+\((?:unidentified image|unknown file|unidentified file)\)$','',str(v or ''),flags=re.I)

def file_maps(root):
    rel={}; byname=collections.defaultdict(list)
    for p in root.rglob('*'):
        if p.is_file():
            rel[p.relative_to(root).as_posix().casefold()]=p
            byname[p.name.casefold()].append(p)
    return rel,byname

def resolve(r, root, rel, byname):
    parts=PureWindowsPath(str(r.get('file_path','')).replace('/','\\')).parts
    for i,v in enumerate(parts):
        if v.lower()=='03_projects' and i+1<len(parts):
            suffix=list(parts[i+1:]); suffix[-1]=clean_name(suffix[-1]); key='/'.join(suffix).casefold()
            if key in rel:return rel[key],None
    basename=clean_name(r.get('file_name') or (parts[-1] if parts else ''))
    matches=byname.get(basename.casefold(),[])
    if len(matches)==1:return matches[0],None
    return None, ('AMBIGUOUS' if matches else 'MISSING')

def name_ext(r):return Path(clean_name(r.get('file_name'))).suffix.lower() or '(none)'

def slug(value):
    cleaned=re.sub(r'[^a-zA-Z0-9_.-]','_',str(value))[:85]
    return cleaned or 'data'

def excel_rows(ws):
    return ws.iter_rows(values_only=True)

def clean_columns(header, count):
    seen=collections.Counter(); columns=[]
    for i in range(count):
        value=str(header[i]).strip() if i<len(header) and header[i] is not None else ''
        value=value or f'column_{i+1}'
        seen[value]+=1
        columns.append(f'{value}__{seen[value]}' if seen[value]>1 else value)
    return columns

def export_rows(rows, path, *, batch_size=10000):
    import pyarrow as pa
    import pyarrow.parquet as pq
    it=iter(rows)
    try: first=next(it)
    except StopIteration: first=[]
    header=list(first)
    # Standardized as string columns, preserving IDs, leading zeros and source precision.
    column_count=len(header)
    if column_count==0:
        # Empty sheet / empty CSV. Keep schema manifest without manufacturing rows.
        return {'rows':0,'columns':[],'empty':True,'parquet_path':None,'normalized_types':'string'}
    names=clean_columns(header,column_count)
    schema=pa.schema([(n,pa.string()) for n in names]); writer=None; total=0
    buffer=[]
    def flush(values, writer):
        cols={n:[r[i] if i<len(r) else None for r in values] for i,n in enumerate(names)}
        tab=pa.Table.from_pydict(cols,schema=schema)
        writer.write_table(tab)
    try:
        for row in it:
            if len(row)>column_count:
                raise ValueError(f'Row has {len(row)} fields but header has {column_count}; manual schema correction needed')
            buffer.append([None if x is None else str(x) for x in row]);total+=1
            if len(buffer)>=batch_size:
                if writer is None: writer=pq.ParquetWriter(str(path),schema,compression='zstd')
                flush(buffer,writer);buffer=[]
        if writer is None: writer=pq.ParquetWriter(str(path),schema,compression='zstd')
        if buffer:flush(buffer,writer)
    except BaseException:
        if writer:writer.close()
        if path.exists():path.unlink()
        raise
    finally:
        if writer:writer.close()
    return {'rows':total,'columns':names,'empty':total==0,'parquet_path':str(path),'normalized_types':'string',
            'notes':'Header is row 1; source units/formulas/types require domain validation.'}

def csv_export(path, outdir, base):
    # Try common encodings without silent character replacement.
    errors=[]
    for encoding in ('utf-8-sig','utf-16','cp1258','cp1252'):
        try:
            with path.open('r',encoding=encoding,newline='') as f:
                sample=f.read(16384); f.seek(0)
                try:dialect=csv.Sniffer().sniff(sample,delimiters=',;\t|')
                except csv.Error:dialect=csv.excel_tab if path.suffix.lower()=='.tsv' else csv.excel
                result=export_rows(csv.reader(f,dialect), outdir/(base+'.parquet'))
                result['encoding']=encoding;result['source_format']='csv';return [result]
        except (UnicodeError,csv.Error) as e:errors.append(f'{encoding}: {e}')
    raise ValueError('Cannot decode CSV ('+'; '.join(errors)+')')

def workbook_export(path,outdir,base):
    from openpyxl import load_workbook
    wb=load_workbook(path,read_only=True,data_only=True,keep_links=False)
    outputs=[]
    try:
        for idx,ws in enumerate(wb.worksheets,1):
            result=export_rows(excel_rows(ws),outdir/(f'{base}_sheet_{idx:03d}.parquet'))
            result.update({'sheet_name':ws.title,'sheet_position':idx,'source_format':'excel',
                           'warning':'Formula cells use cached values; may be empty/stale if workbook not recalculated.'})
            outputs.append(result)
    finally:wb.close()
    return outputs

def svg_extract(path):
    from defusedxml import ElementTree as ET
    # Parse only; do not rasterize SVG or resolve external entities.
    root=ET.parse(path).getroot()
    values=[(el.text or '').strip() for el in root.iter() if el.tag.split('}')[-1]=='text' and (el.text or '').strip()]
    return {'width':root.attrib.get('width'), 'height':root.attrib.get('height'),
            'viewBox':root.attrib.get('viewBox'),'text_samples':values[:40], 'text_nodes':len(values)}

def archive_manifest(path):
    ext=path.suffix.lower()
    if ext=='.zip':
        import zipfile
        with zipfile.ZipFile(path) as z:
            names=z.infolist()
            return {'members':len(names),'examples':[x.filename for x in names[:40]],
                    'uncompressed_bytes':sum(x.file_size for x in names), 'extracted':False}
    if ext=='.7z':
        import py7zr
        with py7zr.SevenZipFile(path,mode='r') as z:
            names=z.getnames()
            return {'members':len(names),'examples':names[:40],'extracted':False}
    return {'status':'MANUAL_INSPECTION_REQUIRED','reason':'RAR requires external decoder; archive is never extracted here'}

def main():
    p=argparse.ArgumentParser(description='Finalize Silver PoC into a non-mutating handoff package')
    p.add_argument('--silver',required=True,type=Path)
    p.add_argument('--raw-root',required=True,type=Path)
    p.add_argument('--image-index',type=Path,help='Optional v3 index folder to cross-check image vector IDs')
    p.add_argument('--out',required=True,type=Path,help='NEW directory; will never overwrite')
    p.add_argument('--verify-hash',action='store_true',help='Read all source files to verify SHA256 (slower)')
    p.add_argument('--max-files',type=int,default=0,help='For smoke testing only; 0 processes all records')
    args=p.parse_args()
    silver=args.silver.resolve(); raw=args.raw_root.resolve(); out=args.out.resolve()
    if not silver.is_file() or not raw.is_dir():p.error('Invalid --silver or --raw-root')
    if out.exists():p.error('Output already exists; use a NEW --out directory')
    if out==raw or raw in out.parents or out==silver.parent:p.error('Output must not be within Bronze or equal Silver directory')
    idx=args.image_index.resolve() if args.image_index else None
    indexed=set(); index_count=None
    if idx:
        refs=idx/'image_index.jsonl';vectors=idx/'image_vectors.npy'
        if not refs.is_file() or not vectors.is_file():p.error('Invalid image index directory')
        with refs.open(encoding='utf-8') as f:
            index_ids=[json.loads(x)['asset_id'] for x in f if x.strip()]
        indexed=set(index_ids); index_count=len(index_ids)
        try:
            import numpy as np
            if np.load(vectors,mmap_mode='r').shape[0]!=index_count:p.error('Vector count and image refs length mismatch')
        except ImportError:
            print('WARNING: numpy not installed; image vector file row count not verified')
    out.mkdir(parents=True); (out/'tables').mkdir()
    rel,byname=file_maps(raw)
    counts=collections.Counter(); ext_counts=collections.Counter(); issues=collections.Counter(); unique=set();docids=set(); chunks_count=0
    doc_seen=set(); i=0; indexed_seen=set(); parquet_count=0; exported_rows=0
    with silver.open(encoding='utf-8-sig') as source, \
         (out/'asset_audit.jsonl').open('w',encoding='utf8') as audit, \
         (out/'issues.jsonl').open('w',encoding='utf8') as warnings, \
         (out/'tables_manifest.jsonl').open('w',encoding='utf8') as tables, \
         (out/'special_metadata.jsonl').open('w',encoding='utf8') as special:
        for line_number,line in enumerate(source,1):
            if not line.strip():continue
            try:r=json.loads(line)
            except Exception as exc:raise ValueError(f'Invalid Silver JSONL line {line_number}: {exc}') from exc
            if not isinstance(r,dict):raise ValueError(f'Line {line_number}: expected JSON object')
            i+=1
            if args.max_files and i>args.max_files:break
            ext=name_ext(r);ext_counts[ext]+=1
            asset_id=str(r.get('version_id') or r.get('document_id') or f'line_{line_number}')
            doc_id=str(r.get('document_id') or '')
            status=str(r.get('status') or 'UNKNOWN');counts[status]+=1
            path,problem=resolve(r,raw,rel,byname)
            outissues=[]
            if asset_id in unique:outissues.append(('ERROR','DUPLICATE_VERSION_ID'))
            unique.add(asset_id)
            if not doc_id:outissues.append(('ERROR','MISSING_DOCUMENT_ID'))
            docids.add(doc_id)
            if problem:outissues.append(('ERROR',problem+'_RAW'))
            if not r.get('content_hash'):outissues.append(('WARNING','MISSING_CONTENT_HASH'))
            chunks=r.get('chunks') or []
            if not isinstance(chunks,list):outissues.append(('ERROR','CHUNKS_NOT_ARRAY'));chunks=[]
            for ch in chunks:
                if not isinstance(ch,dict):outissues.append(('ERROR','BAD_CHUNK'));continue
                if ch.get('content'): chunks_count+=1
            if status in {'FAILED','MISSING','UNSUPPORTED'}:
                outissues.append(('WARNING','SOURCE_STATUS_'+status))
            if idx and ext in IMAGE_EXT and asset_id not in indexed:outissues.append(('WARNING','IMAGE_NOT_INDEXED'))
            if idx and asset_id in indexed:indexed_seen.add(asset_id)
            hash_ok=True
            if args.verify_hash and path:
                expected=r.get('content_hash','')
                if re.fullmatch(r'[0-9a-f]{64}',str(expected),flags=re.I):
                    h=hashlib.sha256()
                    with path.open('rb') as f:
                        for block in iter(lambda:f.read(4*1024*1024),b''):h.update(block)
                    if h.hexdigest().lower()!=expected.lower():
                        outissues.append(('ERROR','HASH_MISMATCH'))
                        hash_ok=False
            if path and hash_ok and ext in TABLE_EXT:
                try:
                    entries=csv_export(path,out/'tables',slug(asset_id)) if ext in {'.csv','.tsv'} else workbook_export(path,out/'tables',slug(asset_id))
                    for entry in entries:
                        entry.update({'asset_id':asset_id,'document_id':doc_id,'file_name':r.get('file_name'),
                                      'source_path':str(path)})
                        jsonline(tables,entry)
                        if entry.get('parquet_path'):parquet_count+=1
                        exported_rows+=entry['rows']
                except Exception as exc:outissues.append(('ERROR','TABLE_EXPORT_FAILED'));jsonline(special,{'asset_id':asset_id,'file_name':r.get('file_name'),'exception':f'{type(exc).__name__}: {str(exc)[:450]}'})
            if path and ext=='.svg':
                try:jsonline(special,{'asset_id':asset_id,'kind':'svg','metadata':svg_extract(path)})
                except Exception as exc:outissues.append(('WARNING','SVG_PARSE_FAILED'));jsonline(special,{'asset_id':asset_id,'exception':str(exc)[:450]})
            if path and ext in {'.zip','.rar','.7z'}:
                try:jsonline(special,{'asset_id':asset_id,'kind':'archive','metadata':archive_manifest(path)})
                except Exception as exc:outissues.append(('WARNING','ARCHIVE_LIST_FAILED'));jsonline(special,{'asset_id':asset_id,'exception':str(exc)[:450]})
            if ext in SPECIAL and ext not in {'.zip','.7z','.rar'}:
                jsonline(special,{'asset_id':asset_id,'kind':'unsupported', 'ext':ext,'next_step':SPECIAL[ext]})
            for severity,code in outissues:
                issues[code]+=1
                jsonline(warnings,{'asset_id':asset_id,'document_id':doc_id,'file_name':r.get('file_name'),
                                   'severity':severity,'code':code,'source_status':status})
            jsonline(audit,{'asset_id':asset_id,'document_id':doc_id,'file_name':r.get('file_name'),
                            'extension':ext,'silver_status':status,'raw_found':bool(path),
                            'resolved_path':str(path) if path else None,
                            'in_image_index':asset_id in indexed if idx else None,
                            'text_chunks':len([c for c in chunks if isinstance(c,dict) and c.get('content')]),
                            'issues':[code for _,code in outissues]})
    summary={'tool_version':VERSION,'silver_file':str(silver),'raw_root':str(raw),
             'image_index':str(idx) if idx else None,'records':min(i,args.max_files) if args.max_files else i,
             'status_counts':dict(counts),'extension_counts':dict(ext_counts),'issues':dict(issues),
             'chunks_with_content':chunks_count,'parquet_files':parquet_count,'table_data_rows':exported_rows,
             'image_vectors_in_index':index_count,'image_assets_matched_in_silver':len(indexed_seen),
             'image_vectors_without_silver_match':len(indexed-indexed_seen) if idx else None,
             'image_index_complete_for_indexed_candidates':len(indexed_seen)==index_count if idx else None,
             'note':'Search index statuses do not rewrite Silver parser history. No accuracy claim is inferred from vector count.'}
    dump(out/'quality_summary.json',summary)
    report=['# Silver Data Quality Report','',f'- Assets checked: **{summary["records"]}**',
            f'- Text chunks with content: **{chunks_count}**',
            f'- Parquet tables exported: **{parquet_count}** (data rows: {exported_rows})',
            f'- Image vectors in supplied index: **{index_count if idx else "not checked"}**','',
            '## Source statuses','']
    report += [f'- `{k}`: {v}' for k,v in counts.most_common()]
    report += ['','## Quality issues','']+[f'- `{k}`: {v}' for k,v in issues.most_common()]
    report += ['','## Interpretation','',
               '- `METADATA_ONLY` for image assets can be valid Silver; image vectors belong to the search index.',
               '- Silver `FAILED` can remain as historical parser status even when separate image embedding later succeeds.',
               '- CSV/Excel Parquet columns are strings; business types, formula caches and units need validation.',
               '- `UNSUPPORTED` format is not silently marked successful.',
               '- This report validates consistency and export, not full semantic accuracy of PDF/PPT text.']
    (out/'QUALITY_REPORT.md').write_text('\n'.join(report)+'\n',encoding='utf8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print('Handoff directory:',out)

if __name__=='__main__':main()
