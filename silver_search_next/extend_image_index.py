"""Copy an existing SigLIP2 index and append embeddings for huge PNGs via libvips previews.
Source image, Silver JSONL and original index are never written.
Place this script beside search_silver.py from the earlier ZIP.
"""
import argparse, json, shutil, sqlite3, sys
from pathlib import Path
import numpy as np


def safe_preview(src, dst, max_side=1024):
    try:
        import pyvips
    except ImportError as e:
        raise RuntimeError('Install libvips binding: python -m pip install "pyvips[binary]"') from e
    # libvips uses demand-driven processing. It still needs RAM; failures are reported.
    im = pyvips.Image.thumbnail(str(src), max_side)
    if im.hasalpha():
        im = im.flatten(background=[255, 255, 255])
    if im.bands == 1: im = im.colourspace('srgb')
    im.jpegsave(str(dst), Q=85, strip=True)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--index',required=True,type=Path)
    p.add_argument('--out',required=True,type=Path)
    p.add_argument('--names',nargs='+',default=['600.png','600_2.png'])
    p.add_argument('--max-side',type=int,default=1024)
    p.add_argument('--device',choices=['auto','cuda','cpu'],default='auto')
    args=p.parse_args()
    if args.max_side<128 or args.max_side>4096: p.error('--max-side must be between 128 and 4096')
    source=args.index.resolve(); out=args.out.resolve()
    if not (source/'image_vectors.npy').is_file() or not (source/'image_index.jsonl').is_file(): p.error('Source image index incomplete')
    if out.exists(): p.error('Output exists. Use a NEW --out directory')
    if out == source or source in out.parents: p.error('Output cannot be inside source index')
    out.mkdir(parents=True)
    # Preserve all original index files, with SQLite backup for transactional consistency.
    for name in ['config.json','stats.json','image_index.jsonl','image_vectors.npy']:
        shutil.copy2(source/name,out/name)
    con_src=sqlite3.connect(str(source/'search.sqlite'))
    con_dst=sqlite3.connect(str(out/'search.sqlite'))
    con_src.backup(con_dst)
    con_dst.close(); con_src.close()
    (out/'previews').mkdir()
    con=sqlite3.connect(str(out/'search.sqlite'))
    ref_rows=[json.loads(line) for line in (out/'image_index.jsonl').open(encoding='utf-8') if line.strip()]
    existing={r['asset_id'] for r in ref_rows}
    wanted=[]
    for name in args.names:
        rows=con.execute('SELECT asset_id,file_name,source_path FROM assets WHERE file_name = ? OR file_name LIKE ?', (name,name+' (%')).fetchall()
        if not rows: print(f'NOT IN INDEX: {name}'); continue
        for asset_id,filename,path in rows:
            if asset_id in existing: print(f'ALREADY INDEXED: {filename}'); continue
            if not path or not Path(path).is_file(): print(f'RAW FILE MISSING: {filename}'); continue
            preview=out/'previews'/f'{asset_id}.jpg'
            try:
                safe_preview(Path(path), preview, args.max_side)
                wanted.append((asset_id,filename,preview))
                print(f'PREVIEW CREATED: {filename} -> {preview.name}')
            except Exception as ex:
                print(f'PREVIEW FAILED: {filename}: {type(ex).__name__}: {ex}')
    con.close()
    if not wanted:
        print('No previews created. Old vectors copied, unchanged.'); return
    # Import model utilities from original search_silver.py (same directory).
    try:
        from search_silver import load_vision, device_choice, image_features
        from PIL import Image
    except ImportError as ex:
        print('Put extend_image_index.py next to search_silver.py; install requirements:',ex)
        raise
    cfg=json.loads((out/'config.json').read_text(encoding='utf-8'))
    device=device_choice(args.device)
    processor,model=load_vision(cfg['model_id'],device)
    base=np.load(out/'image_vectors.npy')
    new_vectors=[];new_refs=[]
    for asset_id, filename, preview in wanted:
        try:
            with Image.open(preview) as im:
                vec=image_features(processor,model,[im.convert('RGB')],device)[0]
            if len(vec) != base.shape[1]:
                raise ValueError(f'Vector dimension mismatch: {len(vec)} != {base.shape[1]}')
            new_vectors.append(vec);new_refs.append({'asset_id':asset_id,'file_name':filename})
            print('EMBEDDED:',filename)
        except Exception as ex: print('EMBED FAILED:',filename,type(ex).__name__,str(ex)[:200])
    if new_vectors:
        # Only changing the NEW copied index; align refs to vector rows.
        merged=np.concatenate([base,np.stack(new_vectors).astype(np.float32)],axis=0)
        temp=out/'image_vectors_new.npy'
        np.save(temp,merged); temp.replace(out/'image_vectors.npy')
        with (out/'image_index.jsonl').open('a',encoding='utf-8') as fp:
            for ref in new_refs: fp.write(json.dumps(ref,ensure_ascii=False)+'\n')
        stats=json.loads((out/'stats.json').read_text(encoding='utf-8'))
        stats['image_embedded']=int(stats.get('image_embedded',len(ref_rows)))+len(new_refs)
        stats['image_skipped']=max(0,int(stats.get('image_skipped',0))-len(new_refs))
        (out/'stats.json').write_text(json.dumps(stats,indent=2),encoding='utf-8')
    print(f'INDEX READY: {out} | previous={len(ref_rows)}, added={len(new_refs)}')

if __name__=='__main__': main()
