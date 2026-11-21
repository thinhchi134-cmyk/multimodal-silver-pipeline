"""Recover metadata, document text, and visual previews from exotic project files.
Read-only inputs: unsupported_enrichment.jsonl and Bronze originals.
Outputs are additive, under a new --out folder. No AI calls.
"""
from __future__ import annotations
import argparse, collections, hashlib, io, json, mimetypes, os, re, shutil, subprocess, sys, tempfile, zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

TOOL_VERSION='exotic-recovery-2.0.0'
IMAGE_EXT={'.jpg','.jpeg','.jfif','.png','.webp','.tif','.tiff','.gif','.bmp'}
DOC_EXT={'.pdf','.pptx','.docx','.csv','.xlsx','.xlsm','.md','.txt'}
IGNORE={'thumbs.db','.ds_store'}

def json_write(fp, obj): fp.write(json.dumps(obj,ensure_ascii=False,default=str)+'\n')
def load_rows(path):
    with path.open(encoding='utf-8-sig') as f:
        for lineno, line in enumerate(f,1):
            if line.strip():
                try: yield json.loads(line)
                except json.JSONDecodeError as exc: raise ValueError(f'Invalid JSONL line {lineno}: {exc}')
def digest(data): return hashlib.sha256(data).hexdigest()
def file_sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()
def safe_member(name):
    name=name.replace('\\','/')
    p=PurePosixPath(name)
    if not name or name.startswith('/') or '\x00' in name or any(part in ('..','') for part in p.parts) or re.match(r'^[A-Za-z]:',name): return None
    return '/'.join(p.parts)
def resolve_source(row, raw_root):
    src=Path(row.get('source_path',''))
    if src.is_file(): return src
    # Keep nested path beneath 03_PROJECTS, if present in Windows path in JSONL
    original=str(row.get('source_path','')).replace('\\','/')
    parts=original.split('/')
    matches=[i for i,p in enumerate(parts) if p.casefold()=='03_projects']
    if matches:
        relative=Path(*parts[matches[-1]+1:])
        candidate=raw_root/relative
        if candidate.is_file(): return candidate
    return None

def safe_image_preview(source, destination, max_side):
    from PIL import Image
    with Image.open(source) as im:
        im.verify()
    with Image.open(source) as im:
        im.thumbnail((max_side,max_side))
        if im.mode not in ('RGB','L'): im=im.convert('RGB')
        im.save(destination,'JPEG',quality=85)
    return str(destination)

def emit_preview(src,dest,max_side):
    # Never disables Pillow's decompression bomb protection.
    return safe_image_preview(src,dest,max_side)

def ai_pdf(source,dest,max_side):
    import pymupdf
    previews=[];texts=[]
    with pymupdf.open(str(source),filetype='pdf') as doc:
        for i,page in enumerate(doc):
            if i>=8: break  # bounded work per file
            text=page.get_text('text').strip()
            if text: texts.append({'page_number':i+1,'content':text[:200000]})
            scale=min(2.0,max_side/max(page.rect.width,page.rect.height,1))
            pix=page.get_pixmap(matrix=pymupdf.Matrix(scale,scale),alpha=False)
            target=dest/f'page_{i+1:03d}.jpg';pix.pil_save(str(target),format='JPEG')
            previews.append((target,i+1))
        return texts,previews,len(doc)

def svg_info(source):
    # defusedxml prevents entity expansion / external entity issues
    from defusedxml import ElementTree as DET
    root=DET.parse(str(source)).getroot()
    nodes=collections.Counter();texts=[]
    for e in root.iter():
        tag=e.tag.split('}')[-1];nodes[tag]+=1
        if tag in ('text','tspan','title','desc'):
            s=' '.join(''.join(e.itertext()).split())
            if s and s not in texts: texts.append(s[:1000])
    return {'tag_counts':dict(nodes),'width':root.attrib.get('width'),'height':root.attrib.get('height'),'viewBox':root.attrib.get('viewBox')}, '\n'.join(texts[:200])

def svg_preview(source,dest,max_side):
    # SVG must be trusted; only local project files. CairoSVG may follow resources.
    # Do not render remote/untrusted SVGs in the main application service.
    import cairosvg
    png=dest.with_suffix('.png')
    cairosvg.svg2png(url=str(source),write_to=str(png),output_width=max_side,unsafe=False)
    emit_preview(png,dest,max_side)
    png.unlink(missing_ok=True)
    return dest

def parse_dxf(source):
    import ezdxf
    doc=ezdxf.readfile(str(source))
    msp=doc.modelspace();counter=collections.Counter();texts=[]
    for obj in msp:
        kind=obj.dxftype();counter[kind]+=1
        if kind=='TEXT': texts.append(obj.dxf.get('text',''))
        elif kind=='MTEXT': texts.append(obj.plain_text())
        elif kind=='INSERT':
            texts.append(str(obj.dxf.get('name','')))
            for a in obj.attribs: texts.append(str(a.dxf.get('text','')))
    return {'layers':[l.dxf.name for l in doc.layers], 'entity_counts':dict(counter)},'\n'.join(x for x in texts if x)[:200000]

def archive_zip(source,out,archive_id,limit_count,limit_bytes,max_each,expand):
    results=[];total=0;expanded=0
    with zipfile.ZipFile(source) as z:
        for i,info in enumerate(z.infolist()):
            if i>=limit_count: raise ValueError(f'Archive has more than {limit_count} entries')
            if info.is_dir(): continue
            name=safe_member(info.filename)
            if name is None: results.append({'name':info.filename,'status':'REJECTED_PATH'});continue
            if info.file_size>max_each: results.append({'name':name,'status':'TOO_LARGE'});continue
            total+=info.file_size
            if total>limit_bytes: raise ValueError(f'Archive expanded size > {limit_bytes} bytes')
            # Unix symbolic link
            if ((info.external_attr>>16)&0o170000)==0o120000: results.append({'name':name,'status':'REJECTED_SYMLINK'});continue
            if info.flag_bits&1: results.append({'name':name,'status':'ENCRYPTED'});continue
            rec={'name':name,'size':info.file_size,'status':'INVENTORIED'}
            if expand and Path(name).name.casefold() not in IGNORE:
                ident=digest((archive_id+'|'+name).encode())[:24]
                dest=out/'archive_children'/f'{ident}{Path(name).suffix.lower()}'
                tmp=dest.with_suffix(dest.suffix+'.part')
                dest.parent.mkdir(exist_ok=True,parents=True)
                n=0
                try:
                    with z.open(info) as inf, tmp.open('wb') as outf:
                        while True:
                            chunk=inf.read(1024*1024)
                            if not chunk: break
                            n+=len(chunk)
                            if n>max_each or n>info.file_size: raise ValueError('Decompressed data exceeds declared/allowed size')
                            outf.write(chunk)
                    tmp.replace(dest)
                    rec.update({'status':'EXTRACTED','child_path':str(dest),'child_id':digest((archive_id+'|'+name).encode()),'sha256':file_sha(dest)})
                    expanded+=1
                finally: tmp.unlink(missing_ok=True)
            results.append(rec)
    return results,expanded

def archive_external_listing(source):
    # Inventory only. Does NOT extract external archives into the filesystem.
    exe=shutil.which('7z') or shutil.which('7za')
    if not exe: raise RuntimeError('7z CLI not found; install 7-Zip and add 7z.exe to PATH (or inspect with 7z l)')
    cp=subprocess.run([exe,'l','-slt',str(source)],capture_output=True,text=True,errors='replace',timeout=120)
    if cp.returncode: raise RuntimeError('7z list failed: '+cp.stderr[:300])
    paths=[]
    for ln in cp.stdout.splitlines():
        if ln.startswith('Path = '):
            name=ln[7:].strip()
            if name and name!=str(source) and safe_member(name): paths.append(name)
    return paths

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--enrichment',type=Path,required=True,help='unsupported_enrichment.jsonl')
    p.add_argument('--raw-root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--converted-dxf',type=Path,default=None,help='Folder of DXF converted OUTSIDE original Bronze')
    p.add_argument('--max-side',type=int,default=1024)
    p.add_argument('--max-archive-entries',type=int,default=2000)
    p.add_argument('--max-archive-mb',type=int,default=1024)
    p.add_argument('--max-child-mb',type=int,default=200)
    p.add_argument('--expand-zip',action='store_true',help='Only ZIP is extracted. No automatic RAR/7Z expansion.')
    a=p.parse_args(argv)
    if not a.enrichment.is_file() or not a.raw_root.is_dir(): p.error('Input enrichment JSONL or raw-root not found')
    if a.out.exists(): p.error('Output exists. Choose a NEW --out folder')
    if not 128<=a.max_side<=2048: p.error('--max-side must be 128..2048')
    rows=list(load_rows(a.enrichment)); a.out.mkdir(parents=True)
    (a.out/'previews').mkdir(); (a.out/'archive_children').mkdir()
    statuses=collections.Counter()
    with (a.out/'recovery_v2.jsonl').open('w',encoding='utf-8') as outf, (a.out/'text_chunks.jsonl').open('w',encoding='utf-8') as txtf, (a.out/'preview_manifest.jsonl').open('w',encoding='utf-8') as prevf, (a.out/'archive_children.jsonl').open('w',encoding='utf-8') as childf:
        for row in rows:
            asset=row['document_id'];name=row.get('file_name','');ext=row.get('extension',Path(name).suffix.lower());source=resolve_source(row,a.raw_root)
            rec={'document_id':asset,'version_id':row.get('version_id'),'file_name':name,'extension':ext,'source_status':row.get('source_status'),'parser_version':TOOL_VERSION,'source_path':str(source) if source else row.get('source_path'),'result':'PENDING','warnings':[],'metadata':{}}
            def write_text(content,where='file',page=None):
                if not content or not content.strip():return
                json_write(txtf,{'document_id':asset,'chunk_id':digest(f'{asset}|{where}|{page}'.encode())[:24], 'content':content[:200000],'source_path':rec['source_path'],'locator':where,'page_number':page})
            def write_preview(path,page=None):
                json_write(prevf,{'document_id':asset,'preview_path':str(path.resolve()),'page_number':page,'encoder_suggestion':'siglip2'})
            if not source:
                rec['result']='MISSING_RAW'
            else:
                try:
                    d=a.out/'previews'/asset;d.mkdir(exist_ok=True)
                    if ext in IMAGE_EXT:
                        dest=d/'preview.jpg';emit_preview(source,dest,a.max_side);write_preview(dest)
                        rec['result']='PREVIEW_READY';rec['metadata']['image_sha256']=file_sha(source)
                    elif ext=='.ai':
                        texts,previews,page_count=ai_pdf(source,d,a.max_side)
                        rec['result']='PREVIEW_READY' if previews else 'METADATA_ONLY';rec['metadata']['page_count']=page_count
                        for chunk in texts:write_text(chunk['content'],'ai_pdf_page',chunk['page_number'])
                        for pic,page in previews:write_preview(pic,page)
                    elif ext=='.svg':
                        try:
                            info,content=svg_info(source);rec['metadata'].update(info);write_text(content,'svg')
                        except Exception as ex: rec['warnings'].append('SVG_XML_PARSE_FAILED: '+str(ex)[:200])
                        dest=d/'preview.jpg'
                        try:
                            svg_preview(source,dest,a.max_side);write_preview(dest)
                            rec['result']='PREVIEW_READY'
                        except Exception as ex:
                            rec['warnings'].append('SVG_RENDER_FAILED: '+str(ex)[:200]);rec['result']='PARTIAL' if rec['metadata'] else 'NEEDS_SOURCE_REEXPORT'
                    elif ext=='.dwg':
                        if a.converted_dxf:
                            candidate=a.converted_dxf/(Path(name).stem+'.dxf')
                            if candidate.is_file():
                                info,content=parse_dxf(candidate);rec['metadata'].update(info);rec['metadata']['converted_dxf_path']=str(candidate);write_text(content,'dxf');rec['result']='STRUCTURE_EXTRACTED'
                            else: rec['result']='NEEDS_DXF_CONVERSION'
                        else: rec['result']='NEEDS_DXF_CONVERSION'
                    elif ext=='.zip':
                        children,count=archive_zip(source,a.out,asset,a.max_archive_entries,a.max_archive_mb*1024*1024,a.max_child_mb*1024*1024,a.expand_zip)
                        rec['metadata']['child_count']=len(children);rec['metadata']['extracted_count']=count
                        rec['result']='CHILD_ASSETS_READY' if count else 'ARCHIVE_INVENTORIED'
                        for ch in children:json_write(childf,{'parent_document_id':asset,**ch})
                    elif ext in ('.rar','.7z'):
                        names=archive_external_listing(source)
                        if len(names)>a.max_archive_entries: raise ValueError('Archive entry count exceeds configured limit')
                        for n in names:json_write(childf,{'parent_document_id':asset,'name':n,'status':'INVENTORIED' if safe_member(n) else 'REJECTED_PATH'})
                        rec['result']='ARCHIVE_INVENTORIED';rec['metadata']['child_count']=len(names)
                    else: rec['result']='UNSUPPORTED'
                except Exception as ex:
                    rec['result']='ERROR';rec['warnings'].append(f'{type(ex).__name__}: {str(ex)[:300]}')
            statuses[rec['result']]+=1;json_write(outf,rec)
            print(f"{rec['result']:<23} {name}")
    summ={'tool_version':TOOL_VERSION,'records':len(rows),'status_counts':dict(statuses),'note':'All outputs additive. Existing Silver and image index untouched. ARCHIVE_INVENTORIED is NOT content indexed.'}
    (a.out/'summary.json').write_text(json.dumps(summ,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summ,indent=2))
if __name__=='__main__':main()
