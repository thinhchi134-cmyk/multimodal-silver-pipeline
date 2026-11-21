"""Non-destructive recovery/triage of unsupported Silver assets."""
from __future__ import annotations
import argparse, collections, datetime, json, os, pathlib, re, sys, zipfile
from pathlib import Path

MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_ITEMS = 10000
MAX_XML_BYTES = 25 * 1024 * 1024


def extension(rec):
    p = rec.get('file_name') or rec.get('file_path') or ''
    return Path(str(p)).suffix.lower()


def resolve(rec, root):
    raw = str(rec.get('file_path') or '').replace('\\', '/')
    name = str(rec.get('file_name') or '')
    norm = raw.lower()
    marker = '03_projects/'
    k = norm.rfind(marker)
    if k != -1:
        tail = raw[k+len(marker):].lstrip('/')
        candidate = root.joinpath(*[part for part in tail.split('/') if part])
        if candidate.is_file(): return candidate
    candidate = root / name
    if candidate.is_file(): return candidate
    return None


def inspect_svg(path):
    if path.stat().st_size > MAX_XML_BYTES:
        return {'status':'NEEDS_REVIEW','reason':'SVG exceeds XML size cap'}
    try:
        from defusedxml import ElementTree as ET
    except ImportError:
        return {'status':'NEEDS_DEPENDENCY','reason':'Install defusedxml'}
    try:
        # parse without evaluating unsafe external entities
        tree = ET.parse(path)
        root = tree.getroot()
        tags=collections.Counter()
        words=[]
        for el in root.iter():
            tags[el.tag.split('}')[-1]] += 1
            if el.tag.split('}')[-1] in ('text','tspan','title','desc'):
                t = ' '.join(' '.join(el.itertext()).split())
                if t: words.append(t[:2000])
                if len(words) >= 500: break
        return {'status':'EXTRACTED_METADATA', 'kind':'svg', 'width':root.attrib.get('width'), 'height':root.attrib.get('height'), 'viewBox':root.attrib.get('viewBox'), 'element_counts':dict(tags.most_common(25)), 'extracted_text':words[:500]}
    except Exception as e:
        return {'status':'PARSE_FAILED','reason':f'{type(e).__name__}: {str(e)[:400]}'}


def inspect_zip(path):
    try:
        with zipfile.ZipFile(path) as z:
            infos=z.infolist()
            if len(infos)>MAX_ARCHIVE_ITEMS:
                return {'status':'NEEDS_REVIEW','reason':'Too many archive entries','item_count':len(infos)}
            names=[i.filename for i in infos]
            return {'status':'EXTRACTED_METADATA','kind':'archive','item_count':len(infos), 'total_uncompressed_bytes':sum(i.file_size for i in infos),'encrypted_items':sum(bool(i.flag_bits & 1) for i in infos),'members':names[:200], 'members_truncated':len(names)>200}
    except Exception as e:
        return {'status':'PARSE_FAILED','reason':f'{type(e).__name__}: {str(e)[:400]}'}


def inspect_rar(path):
    try: import rarfile
    except ImportError: return {'status':'NEEDS_DEPENDENCY','reason':'pip install rarfile'}
    try:
        with rarfile.RarFile(path) as z:
            infos=z.infolist()
            if len(infos)>MAX_ARCHIVE_ITEMS: return {'status':'NEEDS_REVIEW','reason':'Too many archive entries','item_count':len(infos)}
            return {'status':'EXTRACTED_METADATA','kind':'archive','item_count':len(infos),'members':[i.filename for i in infos[:200]],'members_truncated':len(infos)>200}
    except Exception as e: return {'status':'PARSE_FAILED','reason':f'{type(e).__name__}: {str(e)[:400]}'}


def inspect_7z(path):
    try: import py7zr
    except ImportError: return {'status':'NEEDS_DEPENDENCY','reason':'pip install py7zr'}
    try:
        with py7zr.SevenZipFile(path,'r') as z:
            names=z.getnames()
            if len(names)>MAX_ARCHIVE_ITEMS: return {'status':'NEEDS_REVIEW','reason':'Too many archive entries','item_count':len(names)}
            return {'status':'EXTRACTED_METADATA','kind':'archive','item_count':len(names),'members':names[:200],'members_truncated':len(names)>200}
    except Exception as e: return {'status':'PARSE_FAILED','reason':f'{type(e).__name__}: {str(e)[:400]}'}


def inspect_dwg(path):
    with path.open('rb') as f: signature=f.read(6).decode('ascii','replace')
    result={'kind':'dwg','dwg_signature':signature,'status':'NEEDS_CONVERSION', 'next_step':'Create DXF copy with ODA File Converter; then parse DXF using ezdxf'}
    # Optional user-supplied DXF companion file in a converted folder
    return result


def inspect_ai(path):
    with path.open('rb') as f: header=f.read(64)
    is_pdf=header.startswith(b'%PDF-')
    return {'kind':'ai','status':'NEEDS_CONVERSION','pdf_compatible_header':is_pdf,'next_step':'Use Illustrator/Inkscape to export a copy as PDF or SVG, then parse/preview; original AI stays untouched'}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--silver',required=True)
    p.add_argument('--raw-root',required=True)
    p.add_argument('--out',required=True)
    a=p.parse_args()
    silver=Path(a.silver).resolve(); root=Path(a.raw_root).resolve(); out=Path(a.out).resolve()
    if not silver.is_file() or not root.is_dir(): p.error('Silver file or raw root not found')
    if out.exists(): p.error('Output already exists. Choose a NEW --out directory')
    out.mkdir(parents=True)
    counts=collections.Counter(); by_ext=collections.Counter(); total=0
    with silver.open('r',encoding='utf-8-sig') as inp, (out/'unsupported_enrichment.jsonl').open('w',encoding='utf-8') as dest:
        for line_no,line in enumerate(inp,1):
            if not line.strip(): continue
            try: r=json.loads(line)
            except json.JSONDecodeError: continue
            if str(r.get('status','')).upper()!='UNSUPPORTED': continue
            total+=1; ext=extension(r); by_ext[ext or '(none)']+=1
            path=resolve(r,root)
            if path is None: result={'status':'MISSING','reason':'Unable to resolve source file'}
            elif path.stat().st_size>MAX_FILE_BYTES: result={'status':'NEEDS_REVIEW','reason':'Source file exceeds safety threshold'}
            else:
                try:
                    if ext=='.svg': result=inspect_svg(path)
                    elif ext=='.zip': result=inspect_zip(path)
                    elif ext=='.rar': result=inspect_rar(path)
                    elif ext=='.7z': result=inspect_7z(path)
                    elif ext=='.dwg': result=inspect_dwg(path)
                    elif ext=='.ai': result=inspect_ai(path)
                    else: result={'status':'NEEDS_REVIEW','reason':f'No adapter for {ext or "extensionless file"}'}
                except Exception as e:
                    result={'status':'PARSE_FAILED','reason':f'{type(e).__name__}: {str(e)[:400]}'}
            counts[result['status']]+=1
            item={'document_id':r.get('document_id'),'version_id':r.get('version_id'), 'file_name':r.get('file_name'),'source_status':r.get('status'), 'source_path':str(path) if path else r.get('file_path'), 'extension':ext, 'enrichment':result, 'processor':'unsupported-recovery-v1'}
            dest.write(json.dumps(item,ensure_ascii=False)+'\n')
    summary={'records_examined':total,'extension_counts':dict(by_ext),'recovery_status_counts':dict(counts), 'silver_input':str(silver), 'raw_root':str(root), 'note':'Enrichment is separate from Silver history; NEEDS_CONVERSION is not a failure of inventory.'}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print('Wrote:',out/'unsupported_enrichment.jsonl')

if __name__=='__main__': main()
