import importlib.util
import pathlib
import tempfile
import tarfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('pack_runtime', ROOT / 'tools/pack_runtime.py')
pack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pack)


class RuntimePackingTests(unittest.TestCase):
    def test_archive_preserves_links_with_target_neutral_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'source/lib').mkdir(parents=True)
            (root / 'source/lib/library.so.1').write_bytes(b'library')
            (root / 'source/lib/library.so').symlink_to('library.so.1')
            p = pack.Packer(root / 'source', root / 'output')
            p.copy('lib/library.so')
            p.seal()
            p.archive(root / 'runtime.tar')
            with tarfile.open(root / 'runtime.tar') as archive:
                self.assertTrue(archive.getmember('lib/library.so').issym())
                self.assertEqual(archive.getmember('lib/library.so').linkname, 'library.so.1')
                self.assertTrue(all(m.uid == 0 and m.gid == 0 for m in archive.getmembers()))
            with self.assertRaisesRegex(ValueError, 'outside_bundle'):
                p.archive(root / 'output/self.tar')

    def test_same_name_different_content_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'source/lib').mkdir(parents=True)
            (root / 'source/lib/value').write_bytes(b'actual')
            p = pack.Packer(root / 'source', root / 'output', {'lib/value': '0' * 64})
            p.copy('lib/value')
            self.assertEqual((root / 'output/lib/value').read_bytes(), b'actual')

    def test_exact_base_file_is_reused_and_bound_in_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'source').mkdir()
            f = root / 'source/value'
            f.write_bytes(b'actual')
            p = pack.Packer(f.parent, root / 'output', {'value': pack.digest(f)})
            p.copy('value')
            self.assertFalse((root / 'output/value').exists())
            self.assertEqual(p.reused, {'value': pack.digest(f)})

    def test_missing_transitive_dependency_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'source/bin').mkdir(parents=True)
            (root / 'source/bin/program').write_bytes(b'\x7fELFfixture')
            p = pack.Packer(root / 'source', root / 'output')
            with patch.object(pack.subprocess, 'check_output', return_value='(NEEDED) Shared library: [missing.so]'):
                with self.assertRaisesRegex(ValueError, 'missing_runtime_library'):
                    p.copy('bin/program')

    def test_outside_source_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'source').mkdir()
            (root / 'source/outside').symlink_to(root)
            p = pack.Packer(root / 'source', root / 'output')
            with self.assertRaisesRegex(ValueError, 'runtime_link_outside_source'):
                p.copy('outside')

    def test_python_keeps_source_data_and_packages_without_bytecode_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            library = root / 'source/usr/lib/python3.12'
            (library / '__pycache__').mkdir(parents=True)
            (library / 'hello.py').write_text('value = 42\n')
            (library / '__pycache__/hello.pyc').write_bytes(b'unneeded')
            (library / 'data.json').write_text('{}')
            package = root / 'package'
            package.mkdir()
            (package / '__init__.py').write_text('value = 7\n')
            p = pack.Packer(root / 'source', root / 'output')
            p.python('usr/lib/python3.12', [package])
            with zipfile.ZipFile(root / 'output/usr/lib/python312.zip') as z:
                self.assertEqual(set(z.namelist()), {'hello.py', 'data.json', 'package/__init__.py'})


if __name__ == '__main__':
    unittest.main()
