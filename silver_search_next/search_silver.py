"""Read-only Silver search prototype: keyword FTS5 + optional SigLIP2 image/text retrieval.
Input JSONL and raw files are never modified. Python 3.11+.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path, PureWindowsPath

import numpy as np

IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}
DEFAULT_MODEL = 'google/siglip2-base-patch16-224'


def clean_name(name):
    return re.sub(r'\s+\((?:unidentified image|unknown file|unidentified file)\)$', '', str(name), flags=re.I).strip()


def index_paths(root):
    by_rel, by_name = {}, {}
    for p in root.rglob('*'):
        if p.is_file():
            by_rel[p.relative_to(root).as_posix().casefold()] = p
            by_name.setdefault(p.name.casefold(), []).append(p)
    return by_rel, by_name


def resolve_path(record, root, by_rel, by_name):
    """Never pick an ambiguous basename: wrong-project retrieval is worse than skip."""
    path = str(record.get('file_path') or '').replace('/', '\\')
    parts = PureWindowsPath(path).parts
    candidates = []
    for i, part in enumerate(parts):
        if part.casefold() == '03_projects' and i + 1 < len(parts):
            candidates.append(parts[i + 1:])
    # Prefer longest suffix including folder hierarchy, then shorter ones.
    for suffix in candidates:
        ss = list(suffix)
        ss[-1] = clean_name(ss[-1])
        found = by_rel.get('/'.join(ss).casefold())
        if found:
            return found, ''
    name = clean_name(record.get('file_name') or (parts[-1] if parts else ''))
    matches = by_name.get(name.casefold(), [])
    if len(matches) == 1:
        return matches[0], ''
    if matches:
        return None, f'ambiguous basename ({len(matches)} matches)'
    return None, 'missing raw file'


def read_records(path):
    with path.open('r', encoding='utf-8-sig') as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError(f'Line {line_no}: JSON object expected')
            yield line_no, obj


def make_db(db):
    conn = sqlite3.connect(str(db))
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE assets (asset_id TEXT PRIMARY KEY, document_id TEXT, file_name TEXT, source_path TEXT, status TEXT, is_image INTEGER, title TEXT)')
    conn.execute('CREATE TABLE chunks (chunk_key TEXT PRIMARY KEY, asset_id TEXT NOT NULL, page_number INTEGER, content TEXT NOT NULL)')
    conn.execute('CREATE VIRTUAL TABLE text_fts USING fts5(chunk_key UNINDEXED, file_name, title, content, tokenize="unicode61")')
    return conn


def normalize(vectors):
    vec = np.asarray(vectors, dtype=np.float32)
    return vec / np.maximum(np.linalg.norm(vec, axis=-1, keepdims=True), 1e-12)


def device_choice(choice):
    import torch
    if choice == 'auto':
        return 'cuda' if torch.cuda.is_available() else 'cpu'
    if choice == 'cuda' and not torch.cuda.is_available():
        raise RuntimeError('CUDA unavailable; use --device cpu or install CUDA-enabled PyTorch')
    return choice


def load_vision(model_id, device):
    import torch
    from transformers import AutoModel, AutoProcessor
    print(f'Loading {model_id} on {device}...', flush=True)
    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModel.from_pretrained(model_id).to(device).eval()
    return processor, model


def to_numpy_feature(output):
    import torch
    if hasattr(output, 'pooler_output') and output.pooler_output is not None:
        output = output.pooler_output
    elif hasattr(output, 'image_embeds') and output.image_embeds is not None:
        output = output.image_embeds
    elif hasattr(output, 'text_embeds') and output.text_embeds is not None:
        output = output.text_embeds
    elif not isinstance(output, torch.Tensor) and hasattr(output, 'last_hidden_state'):
        output = output.last_hidden_state[:, 0]
    if not isinstance(output, torch.Tensor):
        raise TypeError(f'Unsupported embedding output type: {type(output)}')
    return normalize(output.detach().float().cpu().numpy())


def image_features(processor, model, images, device):
    import torch
    batch = processor(images=images, return_tensors='pt')
    batch = {k: v.to(device) for k, v in batch.items()}
    with torch.inference_mode():
        return to_numpy_feature(model.get_image_features(**batch))


def text_feature(processor, model, prompt, device):
    import torch
    inputs = processor(text=[prompt], padding='max_length', truncation=True, return_tensors='pt')
    inputs = {k: v.to(device) for k, v in inputs.items()}
    with torch.inference_mode():
        return to_numpy_feature(model.get_text_features(**inputs))[0]


def main_build(args):
    root = args.raw_root.resolve()
    silver = args.silver.resolve()
    output = args.out.resolve()
    if not root.is_dir():
        raise SystemExit(f'Raw directory not found: {root}')
    if not silver.is_file():
        raise SystemExit(f'Silver JSONL not found: {silver}')
    if output.exists():
        raise SystemExit(f'Index folder already exists: {output}. Specify a NEW --out path.')
    if output == root or root in output.parents:
        raise SystemExit('Index output must be outside the raw folder.')
    output.mkdir(parents=True)
    conn = make_db(output / 'search.sqlite')
    by_rel, by_name = index_paths(root)
    totals = Counter()
    image_jobs = []
    entries = []
    for line_no, r in read_records(silver):
        asset_id = str(r.get('version_id') or r.get('document_id') or f'line_{line_no}')
        # version_id is asset identity for this snapshot. Preserve document_id in outputs.
        filename = str(r.get('file_name') or '')
        title = str(r.get('title') or '')
        ext = Path(clean_name(filename)).suffix.casefold()
        image_flag = int(ext in IMAGE_EXT)
        path, problem = resolve_path(r, root, by_rel, by_name)
        if problem:
            totals['unresolved'] += 1
        status = str(r.get('status') or '')
        try:
            conn.execute('INSERT INTO assets VALUES (?,?,?,?,?,?,?)',
                         (asset_id, str(r.get('document_id') or ''), filename,
                          str(path) if path else '', status, image_flag, title))
        except sqlite3.IntegrityError:
            totals['duplicate_version_id'] += 1
            continue
        # Filename/title are indexed even with no text chunks (including image-only assets).
        chunk_list = r.get('chunks') or []
        if not chunk_list:
            chunk_list = [{'content': '', 'page_number': None}]
        for i, chunk in enumerate(chunk_list):
            if not isinstance(chunk, dict):
                continue
            content = str(chunk.get('content') or '')[:args.max_chars]
            ck = f'{asset_id}:{i}'
            conn.execute('INSERT INTO chunks VALUES (?,?,?,?)', (ck, asset_id, chunk.get('page_number'), content))
            conn.execute('INSERT INTO text_fts VALUES (?,?,?,?)', (ck, filename, title, content))
            if content:
                totals['text_chunks'] += 1
        if image_flag and path:
            image_jobs.append((asset_id, path, filename))
        totals['assets'] += 1
    conn.commit()
    conn.close()
    # Save image index separately. No image caption or duplicate file bytes are stored.
    manifest = {'silver_source': str(silver), 'raw_root': str(root), 'model_id': args.model,
                'mode': 'image-and-keyword' if args.images else 'keyword-only'}
    (output / 'config.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.images:
        from PIL import Image, ImageOps, UnidentifiedImageError
        device = device_choice(args.device)
        processor, model = load_vision(args.model, device)
        vecs = []
        rows = []
        for i, (asset_id, path, name) in enumerate(image_jobs, 1):
            try:
                # Pillow's protection is intentional; large images are skipped, not decoded unsafely.
                with Image.open(path) as im:
                    im = ImageOps.exif_transpose(im)
                    im.thumbnail((args.max_side, args.max_side))
                    rgb = im.convert('RGB')
                    feature = image_features(processor, model, [rgb], device)[0]
                vecs.append(feature)
                rows.append({'asset_id': asset_id, 'file_name': name})
                totals['image_embedded'] += 1
            except (Exception) as exc:
                totals['image_skipped'] += 1
                if totals['image_skipped'] <= 20:
                    print(f'  SKIP {name}: {type(exc).__name__}: {str(exc)[:150]}', flush=True)
            if i % 25 == 0:
                print(f'  images attempted: {i}/{len(image_jobs)}', flush=True)
        if vecs:
            np.save(output / 'image_vectors.npy', np.stack(vecs))
            with (output / 'image_index.jsonl').open('w', encoding='utf-8') as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + '\n')
    (output / 'stats.json').write_text(json.dumps(dict(totals), indent=2), encoding='utf-8')
    print('Output:', output)
    print('Statistics:', dict(totals))
    if not args.images:
        print('Keyword index ready. To add image search, rerun with --images and a NEW --out folder.')


def rows_by_ids(db, ids):
    if not ids:
        return {}
    conn = sqlite3.connect(str(db))
    q = ','.join('?' for _ in ids)
    data = conn.execute(f'SELECT asset_id, document_id, file_name, source_path FROM assets WHERE asset_id IN ({q})', ids).fetchall()
    conn.close()
    return {r[0]: r for r in data}


def main_search(args):
    folder = args.index.resolve()
    db = folder / 'search.sqlite'
    if not db.is_file():
        raise SystemExit(f'Index not found: {db}')
    if args.mode == 'keyword':
        conn = sqlite3.connect(str(db))
        # FTS query with safely quoted words avoids untrusted MATCH syntax errors.
        words = re.findall(r'\w+', args.query, flags=re.UNICODE)
        if not words:
            raise SystemExit('Enter one or more search words')
        expression = ' OR '.join('"' + w.replace('"','') + '"' for w in words)
        rows = conn.execute('''SELECT a.file_name, a.source_path, a.document_id, c.page_number,
                            snippet(text_fts, 3, '[', ']', '...', 15) AS preview,
                            bm25(text_fts) AS score
                            FROM text_fts JOIN chunks c ON c.chunk_key=text_fts.chunk_key
                            JOIN assets a ON a.asset_id=c.asset_id
                            WHERE text_fts MATCH ? ORDER BY score LIMIT ?''', (expression,args.top)).fetchall()
        conn.close()
        for n, (name,path,doc,page,preview,score) in enumerate(rows,1):
            print(f'{n}. {name} | page={page} | document_id={doc} | BM25={score:.3f}')
            print(f'   {path}\n   {preview[:240]}')
        if not rows: print('No keyword matches. Image-only content needs --mode image.')
        return
    if args.mode == 'image':
        vec_path = folder / 'image_vectors.npy'
        json_path = folder / 'image_index.jsonl'
        if not vec_path.is_file() or not json_path.is_file():
            raise SystemExit('This index has no image embeddings. Run build --images with a new output path.')
        cfg = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
        device = device_choice(args.device)
        processor, model = load_vision(cfg['model_id'], device)
        if args.query_image:
            from PIL import Image, ImageOps
            with Image.open(args.query_image) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((2048, 2048))
                q = image_features(processor, model, [im.convert('RGB')], device)[0]
        else:
            if not args.query:
                raise SystemExit('Provide text query or --query-image path')
            q = text_feature(processor, model, args.query, device)
        vectors = np.load(vec_path, mmap_mode='r')
        if vectors.shape[1] != len(q):
            raise SystemExit(f'Embedding dimension mismatch: stored {vectors.shape[1]}, query {len(q)}')
        scores = np.asarray(vectors @ q)
        indices = np.argsort(-scores)[:args.top]
        refs = [json.loads(l) for l in json_path.open(encoding='utf-8') if l.strip()]
        selected = [refs[int(i)]['asset_id'] for i in indices]
        metadata = rows_by_ids(db, selected)
        for n,i in enumerate(indices,1):
            record = refs[int(i)]; r = metadata.get(record['asset_id'])
            print(f'{n}. similarity={scores[i]:.4f} | {r[2] if r else record["file_name"]}')
            if r: print(f'   {r[3]} | document_id={r[1]}')


def main():
    ap = argparse.ArgumentParser(description='Read-only Silver to Searchable PoC')
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build', help='Build a NEW index folder; never change source Silver / Bronze')
    b.add_argument('--silver', required=True, type=Path)
    b.add_argument('--raw-root', required=True, type=Path)
    b.add_argument('--out', required=True, type=Path)
    b.add_argument('--images', action='store_true', help='Compute SigLIP2 image vectors; downloads model on first run')
    b.add_argument('--device', choices=['auto','cpu','cuda'], default='auto')
    b.add_argument('--model', default=DEFAULT_MODEL)
    b.add_argument('--max-side', type=int, default=1536)
    b.add_argument('--max-chars', type=int, default=20000)
    s = sub.add_parser('search', help='Search existing index')
    s.add_argument('--index', required=True, type=Path)
    s.add_argument('--mode', required=True, choices=['keyword','image'])
    s.add_argument('--query', default='')
    s.add_argument('--query-image', type=Path)
    s.add_argument('--top', type=int, default=10)
    s.add_argument('--device', choices=['auto','cpu','cuda'], default='auto')
    args = ap.parse_args()
    if args.cmd == 'build': main_build(args)
    else: main_search(args)

if __name__ == '__main__':
    main()
