import importlib.util
import pathlib
import sys
import tempfile
import unittest
import zipfile
import zipimport

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('warm_python', ROOT / 'tools/warm_python.py')
warm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(warm)


class PythonWarmTests(unittest.TestCase):
    def test_original_sources_and_data_survive_insertion_of_bytecode(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = pathlib.Path(tmp) / 'runtime.zip'
            files = {'warm_fixture.py': b'def answer():\n    return 42\n', 'other.py': b'value=7\n', 'data.json': b'{"value":9}'}
            with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as z:
                for name, data in files.items():
                    z.writestr(name, data)
            sys.path.insert(0, str(archive))
            try:
                result = warm.warm(archive, ['warm_fixture'])
                self.assertEqual(result['cached_modules'], 1)
                warm.warm(archive, ['warm_fixture'])
                with zipfile.ZipFile(archive) as z:
                    self.assertEqual(len(z.namelist()), len(set(z.namelist())))
                    for name, data in files.items():
                        self.assertEqual(z.read(name), data)
                    self.assertIn('warm_fixture.pyc', z.namelist())
                    self.assertNotIn('other.pyc', z.namelist())
            finally:
                sys.path.remove(str(archive))
                sys.modules.pop('warm_fixture', None)
                zipimport._zip_directory_cache.pop(str(archive), None)
            loader = zipimport.zipimporter(str(archive))
            code = loader.get_code('warm_fixture')
            self.assertEqual(code.co_filename, str(archive) + '/warm_fixture.py')
            self.assertIn('return 42', loader.get_source('warm_fixture'))
            namespace = {}
            exec(code, namespace)
            self.assertEqual(namespace['answer'](), 42)


if __name__ == '__main__':
    unittest.main()
