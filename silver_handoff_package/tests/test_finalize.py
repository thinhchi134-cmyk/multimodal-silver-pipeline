import json, tempfile, unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from silver_finalize import resolve,file_maps, clean_columns, svg_extract

class TestSilver(unittest.TestCase):
    def test_resolve_windows_path_and_no_guess(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'P100').mkdir();(root/'P100'/'render.jpg').write_bytes(b'X')
            rel,by=file_maps(root)
            got,reason=resolve({'file_path':r'E:\a\03_PROJECTS\P100\render.jpg','file_name':'render.jpg'},root,rel,by)
            self.assertEqual(got,root/'P100'/'render.jpg');self.assertIsNone(reason)
    def test_duplicate_csv_headers(self):
        self.assertEqual(clean_columns(['ID','ID','','Price'],4),['ID','ID__2','column_3','Price'])
    def test_svg_text(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'s.svg';p.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10"><text>Hello</text></svg>')
            x=svg_extract(p)
            self.assertEqual(x['text_nodes'],1)
            self.assertEqual(x['text_samples'],['Hello'])
    def test_no_overwrite_contract(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);raw=root/'03_PROJECTS';raw.mkdir();s=root/'silver.jsonl'
            r={'document_id':'d','version_id':'v','file_name':'a.md','file_path':r'E:\03_PROJECTS\a.md','status':'SUCCESS','chunks':[{'content':'Hello'}]}
            (raw/'a.md').write_text('Hello');s.write_text(json.dumps(r)+'\n')
            from silver_finalize import main
            import unittest.mock as mock
            with mock.patch('sys.argv',['silver_finalize','--silver',str(s),'--raw-root',str(raw),'--out',str(root/'out')]): main()
            self.assertEqual(json.loads(s.read_text())['version_id'],'v')
            summ=json.loads((root/'out'/'quality_summary.json').read_text())
            self.assertEqual(summ['records'],1)
            self.assertEqual(summ['chunks_with_content'],1)
            with mock.patch('sys.argv',['silver_finalize','--silver',str(s),'--raw-root',str(raw),'--out',str(root/'out')]):
                with self.assertRaises(SystemExit):main()

if __name__=='__main__':unittest.main()
