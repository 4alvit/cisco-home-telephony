import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('fw',Path(__file__).parents[1]/'scripts/verify_firmware.py')
fw=importlib.util.module_from_spec(spec)
spec.loader.exec_module(fw)


class FirmwareTest(unittest.TestCase):
    def test_tampering_and_extra_files_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'firmware'
            root.mkdir()
            load='SIP45.9-4-2SR4-3S'
            files={load+'.loads':b'fixture-only', 'component.sbn':b'not real firmware'}
            for n,v in files.items():
                (root/n).write_bytes(v)
            manifest=Path(tmp)/'manifest.json'
            manifest.write_text(json.dumps({'model':'CP-7945G','reviewed':True,'load_id':load,'files':{n:hashlib.sha256(v).hexdigest() for n,v in files.items()}}))
            self.assertEqual(fw.verify(root,manifest),(2,load))
            (root/'stray.xml').write_text('x')
            with self.assertRaises(ValueError):fw.verify(root,manifest)
            (root/'stray.xml').unlink()
            (root/'component.sbn').write_text('tampered')
            with self.assertRaises(ValueError):fw.verify(root,manifest)


if __name__=='__main__':unittest.main()
