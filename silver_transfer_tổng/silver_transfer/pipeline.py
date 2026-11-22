"""Bronze -> Silver local PoC. Original raw files are never changed.
Python >=3.10; optional dependencies in requirements.txt.
"""
import argparse, csv, hashlib, json, mimetypes, re, sqlite3, sys, traceback
from datetime import datetime, timezone
from pathlib import Path

VERSION='0.1.0'
TEXT_EXT={'.txt','.md','.log','.json','.jsonl','.xml','.yaml','.yml'}
IMAGE_EXT={'.jpg','.jpeg','.png','.webp','.tif','.tiff','.bmp'}
TABULAR_EXT={'.csv','.tsv','.xlsx'}
DOCUMENT_EXT={'.pdf','.docx','.pptx'}
BIM_EXT={'.rvt','.ifc','.dwg'}
SUPPORTED=TEXT_EXT|IMAGE_EXT|TABULAR_EXT|DOCUMENT_EXT|BIM_EXT

def now(): return datetime.now(timezone.utc).isoformat()
def js(x): return json.dumps(x,ensure_ascii=False,default=str)
def append_jsonl(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a',encoding='utf-8') as f: f.write(js(obj)+'\n')
def sha256_file(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()
def sanitize(s): return re.sub(r'[^a-zA-Z0-9_\-]','_',s)[:90]
def connect(db):
    con=sqlite3.connect(db)
    con.execute('CREATE TABLE IF NOT EXISTS files (source_path TEXT PRIMARY KEY, sha256 TEXT, status TEXT, asset_id TEXT, error TEXT, processed_at TEXT)')
    con.execute('CREATE TABLE IF NOT EXISTS assets (asset_id TEXT PRIMARY KEY, source_path TEXT, project_id TEXT, kind TEXT, title TEXT, sha256 TEXT, status TEXT, metadata TEXT)')
    con.execute('CREATE TABLE IF NOT EXISTS content (chunk_id TEXT PRIMARY KEY, asset_id TEXT, location TEXT, text TEXT)')
    try: con.execute('CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(chunk_id UNINDEXED,asset_id UNINDEXED,location UNINDEXED,text,tokenize="unicode61")')
    except sqlite3.OperationalError: print('WARNING: SQLite FTS5 unavailable')
    con.commit();return con

def chunks_of(text,max_chars=1200,overlap=120):
    text=re.sub(r'\s+',' ',text).strip()
    if not text:return []
    out=[];i=0
    while i<len(text):
        end=min(i+max_chars,len(text));out.append(text[i:end])
        if end==len(text):break
        i=end-overlap
    return out

def write_table(df,dest):
    try:
        df.to_parquet(dest,index=False)
        return dest
    except ImportError:
        alt=dest.with_suffix('.csv')
        df.to_csv(alt,index=False,encoding='utf-8-sig')
        return alt

def category(ext):
    if ext in IMAGE_EXT:return 'image'
    if ext in TABULAR_EXT:return 'table'
    if ext in DOCUMENT_EXT:return 'document'
    if ext in BIM_EXT:return 'bim'
    if ext in TEXT_EXT:return 'text'
    return 'unsupported'

def emit(rows,text,location,role='text'):
    for n,chunk in enumerate(chunks_of(text)):
        rows.append({'location':str(location),'part':n,'text':chunk,'role':role})

def extract(path,kind,output_root,asset_id,args):
    ext=path.suffix.lower(); rows=[]; props={}; tab=[]
    if kind=='image':
        from PIL import Image
        with Image.open(path) as im:
            props.update({'width':im.width,'height':im.height,'format':im.format,'mode':im.mode})
            # No VLM, no image embedding in Bronze->Silver normalization.
    elif ext=='.pdf':
        import fitz
        with fitz.open(path) as doc:
            props['page_count']=len(doc)
            for i,page in enumerate(doc):
                if i>=args.max_pages:props['truncated_at_pages']=args.max_pages;break
                txt=page.get_text('text',sort=True)
                if txt.strip():emit(rows,txt,f'page:{i+1}')
                else: props['pages_requiring_ocr']=props.get('pages_requiring_ocr',[])+[i+1]
    elif ext=='.pptx':
        from pptx import Presentation
        prs=Presentation(str(path));props['slide_count']=len(prs.slides)
        for i,slide in enumerate(prs.slides):
            if i>=args.max_pages:props['truncated_at_slides']=args.max_pages;break
            parts=[]
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text.strip():parts.append(shape.text)
                if shape.has_table:
                    for row in shape.table.rows:parts.append(' | '.join(c.text for c in row.cells))
            if slide.has_notes_slide:
                try:
                    notes=slide.notes_slide.notes_text_frame.text
                    if notes.strip():parts.append(notes)
                except Exception:pass
            emit(rows,'\n'.join(parts),f'slide:{i+1}')
        props['visual_slides_need_render']=True
    elif ext=='.docx':
        from docx import Document
        d=Document(str(path));text='\n'.join(p.text for p in d.paragraphs)
        for t in d.tables:
            text+='\n'+'\n'.join(' | '.join(c.text for c in r.cells) for r in t.rows)
        emit(rows,text,'document')
    elif ext in ('.csv','.tsv'):
        import pandas as pd
        delim='\t' if ext=='.tsv' else ','
        # stream into individual bounded parquet parts; no full file in RAM
        table_dir=output_root/'tables'/asset_id;table_dir.mkdir(parents=True,exist_ok=True)
        reader=pd.read_csv(path,sep=delim,chunksize=args.csv_rows,encoding='utf-8-sig',encoding_errors='replace',on_bad_lines='skip',low_memory=False)
        count=0;cols=[]
        for i,df in enumerate(reader):
            if i>=args.max_table_parts:props['truncated_at_parts']=args.max_table_parts;break
            if not cols:cols=[str(c) for c in df.columns]
            dest=write_table(df,table_dir/f'part-{i:05}.parquet')
            tab.append(str(dest.relative_to(output_root)));count+=len(df)
        props.update({'rows_exported':count,'columns':cols,'table_parts':len(tab)})
        emit(rows,f'Table {path.stem}. Columns: '+', '.join(cols)+'.','table:schema','table_schema')
    elif ext=='.xlsx':
        from openpyxl import load_workbook
        import pandas as pd
        wb=load_workbook(path,read_only=True,data_only=True)
        for ws in wb.worksheets:
            folder=output_root/'tables'/asset_id/sanitize(ws.title);folder.mkdir(parents=True,exist_ok=True)
            iterator=ws.iter_rows(values_only=True)
            try: header=next(iterator)
            except StopIteration:continue
            cols=[str(v) if v is not None else f'column_{i+1}' for i,v in enumerate(header)]
            emit(rows,'Sheet '+ws.title+'. Columns: '+', '.join(cols),f'sheet:{ws.title}','table_schema')
            buf=[];num=0
            for r in iterator:
                buf.append(list(r))
                if len(buf)>=args.csv_rows:
                    target=write_table(pd.DataFrame(buf,columns=cols),folder/f'part-{num:05}.parquet')
                    tab.append(str(target.relative_to(output_root)));num+=1;buf=[]
                    if num>=args.max_table_parts:break
            if buf and num<args.max_table_parts:
                target=write_table(pd.DataFrame(buf,columns=cols),folder/f'part-{num:05}.parquet')
                tab.append(str(target.relative_to(output_root)))
        wb.close();props['table_parts']=len(tab);props['formula_values_are_cached']=True
    elif ext=='.ifc':
        # Optional lightweight IFC metadata from IfcOpenShell; if absent preserve and flag.
        try:
            import ifcopenshell
            m=ifcopenshell.open(str(path));proj=m.by_type('IfcProject');props['ifc_projects']=[x.Name for x in proj]
            types={}
            for obj in m.by_type('IfcProduct'):
                k=obj.is_a();types[k]=types.get(k,0)+1
            props['element_type_counts']=types
            emit(rows,'IFC project: '+', '.join(props['ifc_projects'])+'. Elements: '+js(types),'ifc:summary','bim_summary')
        except ImportError:props['requires_ifcopenshell']=True
    elif ext in ('.rvt','.dwg'):
        props['extraction_status']='needs_specialized_export'
        props['next_step']='Export RVT to IFC via Revit API; DWG via licensed CAD parser or DXF export.'
    elif ext in TEXT_EXT:
        if ext=='.jsonl':
            # JSONL provenance/enrichment, not mandatory vector storage
            n=0
            with path.open('r',encoding='utf-8-sig',errors='replace') as f:
                for line in f:
                    if not line.strip():continue
                    try:
                        obj=json.loads(line);parts=[str(obj.get('title') or ''),str(obj.get('detail') or '')]
                        for c in (obj.get('chunks') or []):
                            if isinstance(c,dict):parts.append(str(c.get('content') or c.get('text') or ''))
                        emit(rows,'\n'.join(parts),f'record:{n+1}','legacy_jsonl')
                    except json.JSONDecodeError:props['bad_jsonl_lines']=props.get('bad_jsonl_lines',0)+1
                    n+=1
                    if n>=args.max_jsonl_records:props['truncated_at_records']=args.max_jsonl_records;break
            props['jsonl_records_read']=n
        else:
            # bound read to avoid massive text artifacts
            with path.open('r',encoding='utf-8-sig',errors='replace') as f:text=f.read(args.max_text_chars)
            emit(rows,text,'text')
            if path.stat().st_size>args.max_text_chars:props['text_may_be_truncated']=True
    return rows,props,tab

def ingest(args):
    bronze=Path(args.bronze).resolve();silver=Path(args.silver).resolve()
    if not bronze.is_dir():raise SystemExit(f'Bronze folder not found: {bronze}')
    if bronze==silver or silver in bronze.parents or bronze in silver.parents:raise SystemExit('Silver must be outside Bronze')
    silver.mkdir(parents=True,exist_ok=True)
    db=connect(silver/'silver_catalog.sqlite'); manifest=silver/'assets.jsonl'; chunkfile=silver/'chunks.jsonl';stats={}
    paths=sorted(p for p in bronze.rglob('*') if p.is_file())
    for p in paths:
        rel=p.relative_to(bronze);ext=p.suffix.lower();kind=category(ext)
        if kind=='unsupported':stats['unsupported']=stats.get('unsupported',0)+1;continue
        project=rel.parts[0] if len(rel.parts)>1 else 'unassigned'
        source=str(rel).replace('\\','/');size=p.stat().st_size
        try:
            digest=sha256_file(p);asset_id=digest[:24]
            old=db.execute('SELECT sha256,status FROM files WHERE source_path=?',(source,)).fetchone()
            if old and old[0]==digest and old[1] in ('ok','needs_export') and not args.force:
                stats['skipped_unchanged']=stats.get('skipped_unchanged',0)+1;continue
            if size>args.max_file_gb*1e9:raise RuntimeError('exceeds max_file_gb limit')
            rows,props,parts=extract(p,kind,silver,asset_id,args)
            status='needs_export' if props.get('extraction_status') else 'ok'
            record={'asset_id':asset_id,'project_id':project,'source_path':source,'source_uri':p.as_uri(),
                'file_name':p.name,'kind':kind,'extension':ext,'mime_type':mimetypes.guess_type(p.name)[0],
                'size_bytes':size,'sha256':digest,'mtime_utc':datetime.fromtimestamp(p.stat().st_mtime,timezone.utc).isoformat(),
                'parser_version':VERSION,'status':status,'properties':props,'parquet_parts':parts,'processed_at':now()}
            # remove previous FTS & chunks for this path to support modifications
            prev=db.execute('SELECT asset_id FROM files WHERE source_path=?',(source,)).fetchone()
            if prev:
                db.execute('DELETE FROM content WHERE asset_id=?',(prev[0],))
                try:db.execute('DELETE FROM content_fts WHERE asset_id=?',(prev[0],))
                except sqlite3.OperationalError:pass
                db.execute('DELETE FROM assets WHERE asset_id=?',(prev[0],))
            append_jsonl(manifest,record)
            db.execute('INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?,?,?,?)',(asset_id,source,project,kind,p.name,digest,status,js(props)))
            for i,row in enumerate(rows):
                cid=f'{asset_id}:{i:06}';out={'chunk_id':cid,'asset_id':asset_id,'project_id':project,'source_path':source,**row}
                append_jsonl(chunkfile,out)
                db.execute('INSERT OR REPLACE INTO content VALUES (?,?,?,?)',(cid,asset_id,row['location'],row['text']))
                try:db.execute('INSERT INTO content_fts VALUES (?,?,?,?)',(cid,asset_id,row['location'],row['text']))
                except sqlite3.OperationalError:pass
            db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)',(source,digest,status,asset_id,None,now()))
            db.commit();stats[status]=stats.get(status,0)+1
        except Exception as e:
            db.rollback();msg=f'{type(e).__name__}: {e}'
            append_jsonl(silver/'errors.jsonl',{'source_path':source,'error':msg,'at':now()})
            db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)',(source,None,'error',None,msg,now()));db.commit()
            stats['error']=stats.get('error',0)+1
            print('ERROR',source,msg,file=sys.stderr)
    print(js({'bronze':str(bronze),'silver':str(silver),'files_found':len(paths),'stats':stats}))

def search(args):
    db=connect(Path(args.silver)/'silver_catalog.sqlite')
    try:
        # FTS5 syntax escaped as a quoted phrase; matches exact token sequence
        q='"'+args.query.replace('"','""')+'"'
        rows=db.execute('SELECT a.project_id,a.source_path,c.location,snippet(content_fts,3,"[","]","…",10) FROM content_fts JOIN content c ON c.chunk_id=content_fts.chunk_id JOIN assets a ON a.asset_id=c.asset_id WHERE content_fts MATCH ? LIMIT ?', (q,args.limit)).fetchall()
    except sqlite3.OperationalError as e:
        print(f'FTS search unavailable: {e}');return
    for r in rows:print(js({'project':r[0],'file':r[1],'location':r[2],'excerpt':r[3]}))

def main(argv=None):
    ap=argparse.ArgumentParser(description='Bronze -> Silver PoC')
    sub=ap.add_subparsers(dest='cmd',required=True)
    ing=sub.add_parser('ingest'); ing.add_argument('--bronze',default='data/bronze');ing.add_argument('--silver',default='data/silver');ing.add_argument('--force',action='store_true')
    ing.add_argument('--csv-rows',type=int,default=10000);ing.add_argument('--max-table-parts',type=int,default=10000)
    ing.add_argument('--max-pages',type=int,default=500);ing.add_argument('--max-jsonl-records',type=int,default=50000)
    ing.add_argument('--max-text-chars',type=int,default=5_000_000);ing.add_argument('--max-file-gb',type=float,default=3)
    sr=sub.add_parser('search');sr.add_argument('--silver',default='data/silver');sr.add_argument('query');sr.add_argument('--limit',type=int,default=10)
    a=ap.parse_args(argv);ingest(a) if a.cmd=='ingest' else search(a)


if __name__ == '__main__':
    main()
