import argparse, collections, json, sqlite3
from pathlib import Path

def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument('--index',type=Path,required=True)
    a=ap.parse_args(argv)
    con=sqlite3.connect(str(a.index/'search.sqlite'))
    rows=con.execute('SELECT file_name,status,source_path,is_image FROM assets').fetchall()
    counts=collections.defaultdict(collections.Counter)
    missing=[]
    for name,status,path,is_image in rows:
        ext=Path(name.split(' (')[0]).suffix.lower() or '(none)'
        counts[ext][status or 'UNKNOWN']+=1
        if not path or not Path(path).is_file(): missing.append(name)
    print('TOTAL ASSETS:',len(rows))
    print('TEXT CHUNKS:',con.execute("SELECT count(*) FROM chunks WHERE trim(content) <> ''").fetchone()[0])
    print('IMAGE VECTORS:',sum(1 for l in (a.index/'image_index.jsonl').open(encoding='utf-8') if l.strip()) if (a.index/'image_index.jsonl').exists() else 0)
    print('\nEXTENSION | STATUS COUNTS')
    for ext,c in sorted(counts.items(),key=lambda v:-sum(v[1].values())):
        print(f'{ext:12} {sum(c.values()):4}  {dict(c)}')
    print('\nUNRESOLVED FILES:')
    for name in missing[:30]: print(' ',name)
    con.close()


if __name__ == '__main__':
    main()
