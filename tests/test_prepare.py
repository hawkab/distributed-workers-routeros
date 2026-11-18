import importlib.util,json,pathlib,tempfile,unittest,zipfile
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('prepare',ROOT/'tools/prepare.py');prepare=importlib.util.module_from_spec(s);s.loader.exec_module(prepare)
class PreparationTests(unittest.TestCase):
    def test_signed_runtime_is_required(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d);(p/'manifest').write_text('{}');(p/'runtime.zip').write_bytes(b'not signed')
            with self.assertRaises((KeyError,ValueError)):prepare.prepare(p/'runtime.zip',p/'manifest',p/'out')
            self.assertFalse((p/'out').exists())
    def test_traversal_is_rejected_and_partial_context_removed(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)
            with zipfile.ZipFile(p/'runtime.zip','w') as z:z.writestr('../outside','bad')
            with patch.object(prepare,'verify',return_value={}):
                with self.assertRaisesRegex(ValueError,'runtime_archive_path_invalid'):prepare.prepare(p/'runtime.zip',p/'manifest',p/'out')
            self.assertFalse((p/'outside').exists());self.assertFalse((p/'out').exists())
    def test_duplicate_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)
            with zipfile.ZipFile(p/'runtime.zip','w') as z:
                z.writestr('a','1');z.writestr('a','2')
            with patch.object(prepare,'verify',return_value={}):
                with self.assertRaisesRegex(ValueError,'runtime_archive_path_invalid'):prepare.prepare(p/'runtime.zip',p/'manifest',p/'out')
    def test_existing_context_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d);(p/'out').mkdir();(p/'out/kept').write_text('data')
            with patch.object(prepare,'verify',return_value={}):
                with self.assertRaisesRegex(ValueError,'context_already_exists'):prepare.prepare(p/'runtime.zip',p/'manifest',p/'out')
            self.assertEqual((p/'out/kept').read_text(),'data')
if __name__=='__main__':unittest.main()
