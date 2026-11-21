"""Safe additive SVG/DWG converter for Silver. No original input modifications.
Requires ODA File Converter installed for DWG -> DXF.
"""
from __future__ import annotations
import argparse, collections, hashlib, json, os, re, shutil, subprocess, tempfile
from pathlib import Path

VERSION = 'silver-auto-converter-1.0'

def records(path):
    with Path(path).open(encoding='utf-8-sig') as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                try: yield json.loads(line)
                except json.JSONDecodeError as e: raise ValueError(f'Line {n}: {e}')

def put(fp, value): fp.write(json.dumps(value, ensure_ascii=False, default=str) + '\n')

def source_of(row, root):
    p = Path(row.get('source_path') or row.get('file_path') or '')
    if p.is_file(): return p
    s = str(p).replace('\\','/')
    parts = s.split('/')
    ids = [i for i,v in enumerate(parts) if v.lower() == '03_projects']
    if ids:
        cand = root.joinpath(*parts[ids[-1]+1:])
        if cand.is_file(): return cand
    return None

def find_oda(spec=None):
    if spec:
        p=Path(spec)
        return p if p.is_file() else None
    candidates=[]
    for name in ('ODAFileConverter.exe', 'ODAFileConverter'):
        found=shutil.which(name)
        if found: candidates.append(Path(found))
    for parent in (Path('C:/Program Files/ODA'),Path('C:/Program Files (x86)/ODA')):
        if parent.exists(): candidates += list(parent.glob('**/ODAFileConverter.exe'))
    return next((p for p in candidates if p.is_file()),None)

def svg_extract(src, target, max_side):
    from defusedxml import ElementTree as safe_et
    import cairosvg
    # Verify parsed XML before rendering. Disallow SVG images/uses referencing external resources.
    root=safe_et.parse(str(src)).getroot()
    texts=[]; count=collections.Counter(); external=[]
    for e in root.iter():
        tag=e.tag.rsplit('}',1)[-1];count[tag]+=1
        if tag in ('text','tspan','title','desc'):
            t=' '.join(''.join(e.itertext()).split())
            if t and t not in texts: texts.append(t[:2000])
        for k,v in e.attrib.items():
            if k.rsplit('}',1)[-1]=='href' and (v.startswith(('http:', 'https:', 'file:', '//')) or (v and not v.startswith(('#','data:')))):
                external.append(v[:160])
    metadata={'element_counts':dict(count),'width':root.get('width'),'height':root.get('height'),'viewBox':root.get('viewBox')}
    if external: return metadata,'\n'.join(texts[:250]),None,'EXTERNAL_REFERENCE_BLOCKED',f'External refs ({len(external)}) not loaded; preview skipped'
    # Use in-memory bytestring not external URL; CairoSVG unsafe=False.
    data=src.read_bytes()
    if len(data)>30*1024*1024: return metadata,'\n'.join(texts[:250]),None,'TOO_LARGE','SVG >30MB; render skipped'
    cairosvg.svg2png(bytestring=data,write_to=str(target),output_width=max_side,unsafe=False)
    return metadata,'\n'.join(texts[:250]),target,'SUCCESS',''

def dxf_extract(path):
    import ezdxf
    doc=ezdxf.readfile(str(path))
    counts=collections.Counter();labels=[]; blocks=[]
    msp=doc.modelspace()
    for i,ent in enumerate(msp):
        if i>=2000000: raise ValueError('DXF too many entities; capped at 2,000,000')
        typ=ent.dxftype();counts[typ]+=1
        if typ=='TEXT': labels.append(ent.dxf.get('text',''))
        elif typ=='MTEXT': labels.append(ent.plain_text())
        elif typ=='INSERT':
            name=ent.dxf.get('name','');blocks.append(name)
            for att in ent.attribs: labels.append(att.dxf.get('text',''))
    meta={'layers':[l.dxf.name for l in doc.layers], 'entity_counts':dict(counts), 'block_names':sorted(set(blocks))[:5000], 'total_entities':sum(counts.values())}
    return meta,'\n'.join(filter(None,labels))[:300000]

def dwg_convert(src, output, oda, version='ACAD2018', timeout=180):
    # Isolate each drawing so identical filenames never collide, and avoid passing the raw tree to ODA.
    with tempfile.TemporaryDirectory(prefix='silver_dwg_') as td:
        td=Path(td); inp=td/'input'; out=td/'output';inp.mkdir();out.mkdir()
        copied=inp/'drawing.dwg';shutil.copy2(src,copied)
        args=[str(oda),str(inp),str(out),version,'DXF','0','1','*.dwg']
        cp=subprocess.run(args,capture_output=True,text=True,errors='replace',timeout=timeout)
        produced=out/'drawing.dxf'
        if not produced.is_file() or produced.stat().st_size==0:
            raise RuntimeError(f'ODA did not produce DXF (exit={cp.returncode}): {(cp.stderr or cp.stdout)[-450:]}')
        shutil.copy2(produced,output)
        return {'oda_exit_code':cp.returncode,'dxf_bytes':output.stat().st_size}

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--enrichment',type=Path,required=True)
    p.add_argument('--raw-root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--oda-path',type=str)
    p.add_argument('--max-side',type=int,default=1024)
    p.add_argument('--timeout',type=int,default=180)
    a=p.parse_args(argv)
    if not a.enrichment.is_file(): p.error('enrichment JSONL not found')
    if not a.raw_root.is_dir(): p.error('raw-root folder not found')
    if a.out.exists(): p.error('Output exists; use a NEW --out folder (never overwrite)')
    if not (256<=a.max_side<=2048): p.error('--max-side must be 256..2048')
    oda=find_oda(a.oda_path)
    entries=[r for r in records(a.enrichment) if (r.get('extension') or Path(r.get('file_name','')).suffix).lower() in ('.svg','.dwg')]
    a.out.mkdir(parents=True)
    for sub in ('previews','converted_dxf'): (a.out/sub).mkdir()
    counter=collections.Counter()
    with (a.out/'results.jsonl').open('w',encoding='utf-8') as log, (a.out/'text_chunks.jsonl').open('w',encoding='utf-8') as txt, (a.out/'preview_manifest.jsonl').open('w',encoding='utf-8') as previews:
        for row in entries:
            uid=str(row.get('document_id') or hashlib.sha256(row['file_name'].encode()).hexdigest()[:20])
            ext=(row.get('extension') or Path(row.get('file_name','')).suffix).lower()
            src=source_of(row,a.raw_root)
            rec={'document_id':uid,'version_id':row.get('version_id'),'file_name':row.get('file_name'),'extension':ext,'source_path':str(src) if src else row.get('source_path'),'status':'PENDING','metadata':{},'error':None,'tool_version':VERSION}
            try:
                if not src: rec['status']='MISSING'
                elif ext=='.svg':
                    target=a.out/'previews'/f'{uid}.png'
                    meta,content,preview,status,error=svg_extract(src,target,a.max_side)
                    rec.update(status=status,metadata=meta,error=error or None)
                    if content: put(txt,{'document_id':uid,'chunk_id':f'{uid}:svg:text','content':content,'source_path':str(src),'chunk_type':'text'})
                    if preview: put(previews,{'document_id':uid,'preview_path':str(preview),'source_path':str(src),'kind':'svg'})
                elif not oda: rec.update(status='NEEDS_ODA',error='Install ODA File Converter and pass --oda-path to ODAFileConverter.exe')
                else:
                    dxf=a.out/'converted_dxf'/f'{uid}.dxf'
                    conv=dwg_convert(src,dxf,oda,timeout=a.timeout)
                    meta,content=dxf_extract(dxf)
                    rec.update(status='SUCCESS',metadata={**meta,**conv},converted_path=str(dxf))
                    if content: put(txt,{'document_id':uid,'chunk_id':f'{uid}:dxf:text','content':content,'source_path':str(src),'chunk_type':'text'})
            except Exception as exc:
                rec.update(status='ERROR',error=f'{type(exc).__name__}: {str(exc)[:500]}')
            counter[rec['status']]+=1
            put(log,rec)
            print(f"{rec['status']:<25} {rec['file_name']}")
    summary={'total':len(entries),'counts':dict(counter),'oda_detected':str(oda) if oda else None,'out':str(a.out)}
    (a.out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
