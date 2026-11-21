"""Create a standalone SigLIP2 preview vector index; does not change v3.
Requires search_silver.py adjacent OR --search-module directory.
"""
import argparse, json, sys
from pathlib import Path
import numpy as np

def main():
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest='command',required=True)
    b=sub.add_parser('build');b.add_argument('--manifest',type=Path,required=True);b.add_argument('--out',type=Path,required=True);b.add_argument('--search-module',type=Path,required=True);b.add_argument('--device',default='auto',choices=['auto','cpu','cuda']);b.add_argument('--model',default='google/siglip2-base-patch16-224')
    s=sub.add_parser('search');s.add_argument('--index',type=Path,required=True);s.add_argument('--search-module',type=Path,required=True);s.add_argument('--query',required=True);s.add_argument('--top',type=int,default=10);s.add_argument('--device',default='auto',choices=['auto','cpu','cuda'])
    a=p.parse_args();sys.path.insert(0,str(a.search_module.resolve()))
    try:from search_silver import load_vision,device_choice,image_features,text_feature
    except ImportError as exc: p.error(f'search_silver.py missing under --search-module: {exc}')
    from PIL import Image
    if a.command=='build':
        if a.out.exists():p.error('Output exists; use a NEW --out')
        rows=[json.loads(l) for l in a.manifest.open(encoding='utf8') if l.strip()]
        a.out.mkdir(parents=True)
        device=device_choice(a.device);proc,model=load_vision(a.model,device)
        vectors=[];refs=[]
        for r in rows:
            path=Path(r['preview_path'])
            if not path.is_file():print('MISSING PREVIEW',path);continue
            try:
                with Image.open(path) as img:vec=image_features(proc,model,[img.convert('RGB')],device)[0]
                vectors.append(vec);refs.append(r);print('INDEXED',path.name)
            except Exception as exc:print('FAILED',path.name,type(exc).__name__,exc)
        if vectors:
            np.save(a.out/'vectors.npy',np.asarray(vectors,dtype=np.float32))
            with (a.out/'refs.jsonl').open('w',encoding='utf8') as f:
                for r in refs:f.write(json.dumps(r,ensure_ascii=False)+'\n')
        (a.out/'config.json').write_text(json.dumps({'model_id':a.model,'vector_count':len(vectors)},indent=2),encoding='utf8')
        print(f'Preview index: {len(vectors)} vectors; existing v3 unchanged')
    else:
        cfg=json.loads((a.index/'config.json').read_text(encoding='utf8'))
        v=np.load(a.index/'vectors.npy')
        refs=[json.loads(l) for l in (a.index/'refs.jsonl').open(encoding='utf8') if l.strip()]
        assert len(refs)==len(v),'Index mismatch'
        device=device_choice(a.device);proc,model=load_vision(cfg['model_id'],device)
        q=text_feature(proc,model,a.query,device)
        scores=v@q
        for i in np.argsort(scores)[::-1][:a.top]:
            r=refs[int(i)]
            print(f"{float(scores[i]):.4f} | {r.get('document_id')} | {r.get('preview_path')} | page={r.get('page_number')}")
if __name__=='__main__':main()
