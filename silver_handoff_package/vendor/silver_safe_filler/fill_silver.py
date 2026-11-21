"""Copy-and-fill an existing Silver JSONL template without modifying raw files or template.
Run from project root. Requires Python 3.10+. Optional packages in requirements.txt.
"""
import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path, PureWindowsPath

VERSION = 'safe-silver-filler-1.0'
IMAGE = {'.jpg','.jpeg','.png','.webp','.bmp','.tif','.tiff','.gif'}
TEXT = {'.txt','.md','.log','.json','.yaml','.yml','.xml','.html','.htm','.py','.sql'}
TABULAR = {'.csv','.tsv','.xlsx','.xlsm'}
SUPPORTED = IMAGE | TEXT | TABULAR | {'.pdf','.pptx','.docx'}


def clean_filename(name):
    # Some manifest records contain explanatory suffixes appended to filenames.
    return re.sub(r'\s+\((?:unidentified image|unknown file|unidentified file)\)$', '', name, flags=re.I).strip()


def file_index(root):
    by_name = {}
    by_relative = {}
    for p in root.rglob('*'):
        if not p.is_file():
            continue
        by_name.setdefault(p.name.casefold(), []).append(p)
        by_relative[p.relative_to(root).as_posix().casefold()] = p
    return by_name, by_relative


def resolve_file(record, root, by_name, by_relative):
    raw_path = str(record.get('file_path') or '').replace('/', '\\')
    pieces = list(PureWindowsPath(raw_path).parts)
    # More than one 03_PROJECTS component can occur in real folder layouts.
    suffixes = []
    for i, part in enumerate(pieces):
        if part.casefold() == '03_projects':
            suffixes.append(pieces[i + 1:])
    if not suffixes:
        suffixes = [pieces[-2:]]
    for suffix in suffixes:
        if not suffix: continue
        suffix = list(suffix)
        suffix[-1] = clean_filename(suffix[-1])
        rel = '/'.join(suffix).casefold()
        if rel in by_relative:
            return by_relative[rel], None
        candidate = root.joinpath(*suffix)
        if candidate.is_file():
            return candidate, None
    name = clean_filename(record.get('file_name') or (pieces[-1] if pieces else ''))
    matches = by_name.get(name.casefold(), [])
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, 'raw file not found (check --raw-root or source path)'
    return None, f'ambiguous filename: {len(matches)} matching raw files; cannot safely choose'


def chunk(n, text, label):
    return {'chunk_no': n, 'page_number': None, 'section_title': label, 'content': text}


def extract(path, max_chars=120000):
    ext = path.suffix.casefold()
    chunks = []
    page_count = None
    detail = ''
    if ext in IMAGE:
        from PIL import Image
        with Image.open(path) as im:
            size = im.size
            fmt = im.format or ext[1:].upper()
            frames = getattr(im, 'n_frames', 1)
        # Do not put invented visual descriptions in text chunks.
        detail = f'Image metadata only: format={fmt}; width={size[0]}; height={size[1]}; frames={frames}. No caption/OCR generated.'
        return [], None, detail, 'METADATA_ONLY', False
    if ext in TEXT:
        content = path.read_text(encoding='utf-8-sig', errors='replace')[:max_chars]
        if content.strip():
            chunks = [chunk(1, content, 'Extracted text')]
        return chunks, None, '', 'SUCCESS', len(content) < max_chars
    if ext == '.pdf':
        import fitz
        with fitz.open(path) as doc:
            page_count = len(doc)
            for i, page in enumerate(doc):
                text = page.get_text('text')
                if text.strip():
                    c = chunk(len(chunks)+1, text[:max_chars], 'PDF text')
                    c['page_number'] = i + 1
                    chunks.append(c)
        state = 'SUCCESS' if chunks else 'METADATA_ONLY'
        return chunks, page_count, '' if chunks else 'No embedded text extracted; scanned PDF may need OCR.', state, bool(chunks)
    if ext == '.pptx':
        from pptx import Presentation
        prs = Presentation(str(path))
        page_count = len(prs.slides)
        for i, slide in enumerate(prs.slides):
            pieces = []
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text.strip(): pieces.append(shape.text)
                if shape.has_table:
                    for row in shape.table.rows:
                        pieces.append(' | '.join(cell.text for cell in row.cells))
            if pieces:
                c = chunk(len(chunks)+1, '\n'.join(pieces)[:max_chars], 'Slide text')
                c['page_number'] = i + 1
                chunks.append(c)
        return chunks, page_count, 'Embedded slide images not visually described.', 'SUCCESS' if chunks else 'METADATA_ONLY', False
    if ext == '.docx':
        from docx import Document
        doc = Document(str(path))
        blocks = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                blocks.append(' | '.join(c.text for c in row.cells))
        joined = '\n'.join(blocks)
        if joined.strip(): chunks = [chunk(1,joined[:max_chars],'Document text')]
        return chunks,None,'Embedded images not visually described.','SUCCESS' if chunks else 'METADATA_ONLY',False
    if ext in {'.csv','.tsv'}:
        delimiter = '\t' if ext == '.tsv' else ','
        with path.open('r',encoding='utf-8-sig',errors='replace',newline='') as f:
            sample=f.read(8192);f.seek(0)
            try: dialect=csv.Sniffer().sniff(sample, delimiters=',;\t|')
            except csv.Error: dialect=csv.excel_tab if ext=='.tsv' else csv.excel
            r=csv.reader(f,dialect)
            header=next(r,[])
            # Count rows without retaining them in memory. Original CSV is left unchanged.
            count=sum(1 for _ in r)
        desc=f'Table columns: {", ".join(header)}. Data rows: {count}. Source: {path.name}.'
        return [chunk(1,desc,'Table schema')],None,'Only table schema is indexed here; rows remain in raw CSV.','SUCCESS',False
    if ext in {'.xlsx','.xlsm'}:
        from openpyxl import load_workbook
        wb=load_workbook(path,read_only=True,data_only=True)
        for ws in wb.worksheets:
            header=next(ws.iter_rows(min_row=1,max_row=1,values_only=True),())
            values=', '.join(str(x) for x in header if x is not None)
            text=f'Worksheet: {ws.title}. Columns: {values}. Approximate total rows: {ws.max_row}.'
            chunks.append(chunk(len(chunks)+1,text[:max_chars],f'Worksheet: {ws.title}'))
        wb.close()
        return chunks,None,'Only worksheet schema recorded; values remain in workbook.','SUCCESS',False
    raise ValueError('unsupported extension')


def main():
    ap=argparse.ArgumentParser(description='Fill COPY of Silver template JSONL using local raw files')
    ap.add_argument('--raw-root',required=True,type=Path,help='root folder containing raw project files')
    ap.add_argument('--template',required=True,type=Path)
    ap.add_argument('--output',required=True,type=Path)
    ap.add_argument('--force-output',action='store_true',help='explicitly allow replacing output, never template')
    ap.add_argument('--verify-hash',action='store_true',help='compute SHA256 to warn about source mismatch (slower)')
    args=ap.parse_args()
    root=args.raw_root.resolve(); tmpl=args.template.resolve(); out=args.output.resolve()
    if not root.is_dir() or not tmpl.is_file(): ap.error('raw-root must be a folder and template must exist')
    if out == tmpl: ap.error('Refusing to overwrite template')
    if out.exists() and not args.force_output: ap.error('Output exists. Use a NEW filename or --force-output')
    if root in out.parents: ap.error('Output must not be inside raw-root')
    out.parent.mkdir(parents=True,exist_ok=True)
    by_name,by_relative=file_index(root)
    counts=Counter(); example=[]
    cfg=hashlib.sha256((VERSION+'|metadata-and-text|no-caption').encode()).hexdigest()
    with tmpl.open(encoding='utf-8-sig') as source, out.open('x' if not args.force_output else 'w',encoding='utf-8') as dest:
        for num,line in enumerate(source,1):
            if not line.strip(): continue
            try: r=json.loads(line)
            except json.JSONDecodeError as e: raise RuntimeError(f'Invalid template JSON at line {num}: {e}') from e
            if not isinstance(r,dict): raise RuntimeError(f'Expected object at line {num}')
            p,error=resolve_file(r,root,by_name,by_relative)
            status='MISSING'
            detail=''
            if error:
                detail=error
                if error.startswith('ambiguous'): status='AMBIGUOUS'
            elif p.suffix.casefold() not in SUPPORTED:
                status='UNSUPPORTED'
                detail=f'No parser registered for extension {p.suffix}. Raw file remains searchable by filename/metadata only.'
            else:
                try:
                    if args.verify_hash:
                        actual=hashlib.sha256(p.read_bytes()).hexdigest()
                        expected=str(r.get('content_hash') or '')
                        if re.fullmatch(r'[a-fA-F0-9]{64}',expected) and actual.lower()!=expected.lower():
                            status='HASH_MISMATCH'
                            detail='Raw file SHA256 does not match template. Skipped extraction to avoid mixing versions.'
                    if status != 'HASH_MISMATCH':
                        chunks,pages,detail,status,complete=extract(p)
                        r['chunks']=chunks
                        r['page_count']=pages
                        r['content_complete']=complete
                except Exception as e:
                    status='FAILED'
                    detail=f'{type(e).__name__}: {str(e)[:300]}'
            r['status']=status
            r['processing']={'parser_name':VERSION if status not in {'MISSING','AMBIGUOUS','UNSUPPORTED'} else 'not_processed',
                             'parser_version':VERSION,'config_hash':cfg}
            r['detail']=detail
            # Existing source fields and identifiers are never modified.
            dest.write(json.dumps(r,ensure_ascii=False)+'\n')
            counts[status]+=1
            if len(example)<8 and status in {'MISSING','AMBIGUOUS','FAILED','HASH_MISMATCH'}:
                example.append((r.get('file_name'),status,detail))
    print(f'Input template: {tmpl}\nRaw folder: {root}\nNew output: {out}')
    print('Results:',dict(counts))
    for name,state,note in example: print('  ',state,name, note)
    print('Original template and all raw files were not modified.')

if __name__=='__main__': main()
