import json, tempfile, unittest, zipfile
from pathlib import Path
from PIL import Image
from silver_transfer.recover_exotic import main, safe_member

class Smoke(unittest.TestCase):
 def test_safe_paths(self):
  self.assertIsNone(safe_member('../bad.txt'))
  self.assertIsNone(safe_member('C:/evil.txt'))
  self.assertIsNone(safe_member('/abs'))
  self.assertEqual(safe_member('a/b.jpg'),'a/b.jpg')
 def test_roundtrip(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);raw=root/'raw';raw.mkdir()
   Image.new('RGB',(64,32),(3,80,90)).save(raw/'shot.jfif','JPEG')
   (raw/'art.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><text x="1" y="20">Project title</text></svg>',encoding='utf8')
   import pymupdf
   pdf=pymupdf.open();p=pdf.new_page();p.insert_text((72,72),'Architectural design');pdf.save(str(raw/'art.ai'));pdf.close()
   with zipfile.ZipFile(raw/'bundle.zip','w') as z:
    z.writestr('new_project/floorplan.txt','a'),z.writestr('../evil.txt','evil')
   (raw/'broken.svg').write_text('<svg><text>',encoding='utf8')
   rows=[]
   for i,name in enumerate(['shot.jfif','art.svg','art.ai','bundle.zip','broken.svg','missing.dwg']):
    rows.append({'document_id':f'id{i}','version_id':f'v{i}','file_name':name,'extension':Path(name).suffix,'source_path':str(raw/name),'source_status':'UNSUPPORTED'})
   enrichment=root/'input.jsonl';enrichment.write_text(''.join(json.dumps(r)+'\n' for r in rows))
   out=root/'output'
   main(['--enrichment',str(enrichment),'--raw-root',str(raw),'--out',str(out),'--expand-zip'])
   data=[json.loads(l) for l in (out/'recovery_v2.jsonl').read_text(encoding='utf8').splitlines()]
   status={r['file_name']:r['result'] for r in data}
   self.assertEqual(status['shot.jfif'],'PREVIEW_READY')
   self.assertEqual(status['art.svg'],'PREVIEW_READY')
   self.assertEqual(status['art.ai'],'PREVIEW_READY')
   self.assertEqual(status['bundle.zip'],'CHILD_ASSETS_READY')
   self.assertEqual(status['missing.dwg'],'MISSING_RAW')
   text=(out/'text_chunks.jsonl').read_text()
   self.assertIn('Architectural design',text)
   children=[json.loads(l) for l in (out/'archive_children.jsonl').read_text(encoding='utf8').splitlines()]
   self.assertTrue(any(x['status']=='REJECTED_PATH' for x in children))
   self.assertTrue(any(x['status']=='EXTRACTED' for x in children))
   self.assertFalse((root/'evil.txt').exists())
   self.assertFalse((raw/'evil.txt').exists())
if __name__=='__main__':unittest.main()

