import argparse, csv, hashlib, json, mimetypes
from collections import Counter
from pathlib import Path, PureWindowsPath

CONFIG_VERSION='silver-parser-poc-v2'

def sha256(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for piece in iter(lambda:f.read(1024*1024),b''):h.update(piece)
    return h.hexdigest()

def parser(path, max_chars):
    suffix=path.suffix.lower()
    if suffix in ('.jpg','.jpeg','.png','.webp','.tif','.tiff','.jfif') or path.name.lower().endswith('.png (unidentified image)'):
        from PIL import Image
        with Image.open(path) as im:
            meta={'kind':'image','width':im.width,'height':im.height,'format':im.format,'mode':im.mode}
        return 'pillow',meta,[],None
    if suffix in ('.txt','.md','.log'):
        s=path.read_text(encoding='utf-8',errors='replace')
        return 'text',{'kind':'text','char_count':len(s)},[s[:max_chars]] if s.strip() else [],None
    if suffix=='.csv':
        with path.open('r',encoding='utf-8-sig',errors='replace',newline='') as f:
            sample=f.read(32768); f.seek(0)
            try: dialect=csv.Sniffer().sniff(sample,delimiters=',;\t|')
            except csv.Error: dialect=csv.excel
            reader=csv.reader(f,dialect)
            header=next(reader,[])
            count=sum(1 for _ in reader)
        return 'csv',{'kind':'table','columns':header,'data_rows':count},[],None
    if suffix=='.pdf':
        import fitz
        pages=[]; truncated=False
        with fitz.open(path) as doc:
            total=len(doc)
            for p in doc:
                t=p.get_text('text').strip()
                if len(t)>max_chars:truncated=True
                if t:pages.append((p.number+1,t[:max_chars]))
        return 'pymupdf',{'kind':'document','page_count':total,'text_pages':len(pages),'truncated':truncated},pages,total
    if suffix=='.pptx':
        from pptx import Presentation
        prs=Presentation(str(path)); pages=[]; truncated=False
        for idx,slide in enumerate(prs.slides,1):
            texts=[sh.text.strip() for sh in slide.shapes if sh.has_text_frame and sh.text.strip()]
            if texts:
                merged='\n'.join(texts)
                if len(merged)>max_chars:truncated=True
                pages.append((idx,merged[:max_chars]))
        return 'python-pptx',{'kind':'presentation','slide_count':len(prs.slides),'truncated':truncated},pages,len(prs.slides)
    if suffix=='.docx':
        from docx import Document
        doc=Document(str(path)); paras=[p.text for p in doc.paragraphs if p.text.strip()]
        merged='\n'.join(paras)
        return 'python-docx',{'kind':'document','truncated':len(merged)>max_chars},[merged[:max_chars]] if merged else [],None
    if suffix in ('.xlsx','.xlsm'):
        from openpyxl import load_workbook
        wb=load_workbook(path,read_only=True,data_only=True)
        sheets=[]
        for s in wb:
            it=s.iter_rows(values_only=True)
            header=next(it,())
            sheets.append({'sheet':s.title,'columns':[str(x) if x is not None else '' for x in header], 'max_row_reported':s.max_row})
        wb.close()
        return 'openpyxl',{'kind':'workbook','sheets':sheets},[],None
    return None,{'kind':'unsupported','extension':suffix},[],None

def fill(row, max_chars, source_path=None):
    out=dict(row)
    p=source_path if source_path is not None else Path(row.get('file_path',''))
    config_hash=hashlib.sha256(f'{CONFIG_VERSION}:max_chars={max_chars}'.encode()).hexdigest()
    out['processing']={'parser_name':'none','parser_version':CONFIG_VERSION,'config_hash':config_hash}
    out.pop('processing_error', None)
    out.pop('actual_content_hash', None)
    out.pop('extracted_metadata', None)
    out['chunks']=[];out['detail']='';out['content_complete']=False
    out['page_count']=None
    if not p.is_file():
        out['status']='MISSING';out['detail']=out['processing_error']='Source file not accessible on this computer'
        return out
    try:
        actual=sha256(p)
        if row.get('content_hash') and row['content_hash'].lower()!=actual:
            out['status']='HASH_MISMATCH';out['detail']=out['processing_error']='Content differs from inventoried version'
            out['actual_content_hash']=actual
            return out
        name, metadata, text_items, count=parser(p,max_chars)
        out['extracted_metadata']=metadata
        if name is None:
            out['status']='UNSUPPORTED'
            out['detail']=f'No parser for extension {p.suffix}'
            return out
        out['processing']['parser_name']=name
        out['page_count']=count
        for i, item in enumerate(text_items, 1):
            page, content = item if isinstance(item, tuple) else (None, item)
            out['chunks'].append({'chunk_no': i, 'page_number': page, 'section_title': None, 'content': content})
        # No OCR/caption for images; no full rows for tabular files. This parser only indexes their metadata.
        if metadata['kind'] in ('image','table','workbook'):
            out['status']='METADATA_ONLY';out['content_complete']=False
            out['detail']='Image metadata only; OCR and visual content not extracted' if metadata['kind']=='image' else 'Table metadata only; data rows not extracted'
        elif metadata['kind'] in ('document','presentation') and name in ('pymupdf','python-pptx') and not out['chunks']:
            out['status']='NEEDS_OCR_OR_VISUAL_EXTRACTION'
            out['detail']='No text extracted by the parser'
        elif name in ('pymupdf', 'python-pptx') and len(out['chunks']) < count:
            out['status']='PARTIAL'
            out['detail']='Some pages or slides have no extracted text'
        else:
            out['status']='SUCCESS';out['content_complete']=True
        if name in ('text','pymupdf','python-pptx','python-docx'):
            if metadata.get('truncated') or (name=='text' and metadata['char_count']>max_chars):
                out['content_complete']=False;out['status']='PARTIAL'
                out['detail']='Extracted text exceeded the character limit. ' + out['detail']
        return out
    except Exception as e:
        out['status']='FAILED';out['detail']=out['processing_error']=f'{type(e).__name__}: {e}'
        return out

def map_source_path(file_path, template_root, source_root):
    try:
        relative = PureWindowsPath(file_path).relative_to(template_root)
    except ValueError:
        return None
    local_path = source_root.joinpath(*relative.parts).resolve()
    return local_path if local_path.is_relative_to(source_root) else None

def main():
    ap=argparse.ArgumentParser(description='Fill existing silver JSONL template from local source files, without VLM')
    ap.add_argument('--template',required=True)
    ap.add_argument('--output',required=True)
    ap.add_argument('--split-by-type',action='store_true')
    ap.add_argument('--max-chars',type=int,default=10000)
    ap.add_argument('--template-root', type=PureWindowsPath, help='Original Windows root in template file_path')
    ap.add_argument('--source-root', type=Path, help='Local folder corresponding to the original root')
    args=ap.parse_args()
    if not 1 <= args.max_chars <= 20000:
        ap.error('--max-chars must be between 1 and 20000')
    if bool(args.template_root) != bool(args.source_root):
        ap.error('--template-root and --source-root must be used together')
    if args.source_root:
        args.source_root = args.source_root.resolve()
        if not args.source_root.is_dir():
            ap.error(f'Source directory not found: {args.source_root}')
    if Path(args.template).resolve() == Path(args.output).resolve():
        ap.error('--output must differ from --template')
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    count=Counter(); handles={}
    try:
        with open(args.template,encoding='utf-8') as src,output.open('w',encoding='utf-8') as dest:
            for line in src:
                if not line.strip():continue
                row = json.loads(line)
                local_path = None
                if args.source_root:
                    local_path = map_source_path(row['file_path'], args.template_root, args.source_root)
                    if local_path is None:
                        raise ValueError(f"Template path outside mapped root: {row['file_path']}")
                obj=fill(row,args.max_chars,local_path)
                serialized=json.dumps(obj,ensure_ascii=False)+'\n'
                dest.write(serialized);count[obj['status']]+=1
                if args.split_by_type:
                    suffix=Path(obj.get('file_name','')).suffix.lower().lstrip('.') or 'unknown'
                    if suffix not in handles:handles[suffix]=(output.parent/f'{output.stem}_{suffix}.jsonl').open('w',encoding='utf-8')
                    handles[suffix].write(serialized)
    finally:
        for h in handles.values():h.close()
    print('Result:',str(output))
    print('Statuses:',dict(count))

if __name__=='__main__':main()
