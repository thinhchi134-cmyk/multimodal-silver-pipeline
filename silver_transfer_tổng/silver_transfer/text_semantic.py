"""Create/search a multilingual text embedding index from existing search.sqlite chunks.
No writes to original search.sqlite, Silver JSONL or raw files.
"""
import argparse, json, sqlite3
from pathlib import Path
import numpy as np

MODEL='intfloat/multilingual-e5-small'

def open_model(name,device):
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(name,device=device)

def build(args):
    src=args.index/'search.sqlite'; dest=args.out
    if not src.is_file(): raise SystemExit(f'Not found: {src}')
    if dest.exists(): raise SystemExit(f'Output already exists: {dest}')
    conn=sqlite3.connect(str(src))
    rows=conn.execute('''SELECT c.chunk_key,a.document_id,a.file_name,a.source_path,c.page_number,c.content
                         FROM chunks c JOIN assets a ON c.asset_id=a.asset_id
                         WHERE trim(c.content) <> '' ORDER BY c.chunk_key''').fetchall()
    conn.close()
    if not rows: raise SystemExit('No nonempty text chunks in index')
    print('Text chunks to index:',len(rows))
    texts=['passage: '+str(r[-1])[:args.max_chars] for r in rows]
    model=open_model(args.model,args.device)
    vec=model.encode(texts,batch_size=args.batch,normalize_embeddings=True,convert_to_numpy=True,show_progress_bar=True)
    dest.mkdir(parents=True)
    np.save(dest/'text_vectors.npy',np.asarray(vec,dtype='float32'))
    with (dest/'text_index.jsonl').open('w',encoding='utf-8') as f:
        for r in rows:
            obj=dict(zip(['chunk_key','document_id','file_name','source_path','page_number','content'],r))
            f.write(json.dumps(obj,ensure_ascii=False)+'\n')
    (dest/'config.json').write_text(json.dumps({'model':args.model,'source_index':str(args.index.resolve())},indent=2),encoding='utf-8')
    print('Saved:',dest)

def search(args):
    folder=args.index
    cfg=json.loads((folder/'config.json').read_text(encoding='utf-8'))
    model=open_model(cfg['model'],args.device)
    query=model.encode(['query: '+args.query],normalize_embeddings=True,convert_to_numpy=True)[0]
    vectors=np.load(folder/'text_vectors.npy',mmap_mode='r')
    scores=np.asarray(vectors @ query)
    refs=[json.loads(l) for l in (folder/'text_index.jsonl').open(encoding='utf-8') if l.strip()]
    if len(refs)!=len(scores): raise RuntimeError('Vector and metadata count mismatch')
    for rank,i in enumerate(np.argsort(-scores)[:args.top],1):
        r=refs[int(i)]
        print(f'{rank}. similarity={scores[i]:.4f} | {r["file_name"]} | page={r["page_number"]}')
        print('   ',r['source_path'])
        print('   ',r['content'][:220].replace('\n',' '))

def main(argv=None):
    p=argparse.ArgumentParser(); s=p.add_subparsers(dest='cmd',required=True)
    b=s.add_parser('build');b.add_argument('--index',type=Path,required=True);b.add_argument('--out',type=Path,required=True)
    b.add_argument('--model',default=MODEL);b.add_argument('--device',default='cpu');b.add_argument('--batch',type=int,default=16);b.add_argument('--max-chars',type=int,default=2500)
    r=s.add_parser('search');r.add_argument('--index',type=Path,required=True);r.add_argument('--query',required=True);r.add_argument('--device',default='cpu');r.add_argument('--top',type=int,default=10)
    a=p.parse_args(argv);build(a) if a.cmd=='build' else search(a)


if __name__ == '__main__':
    main()
