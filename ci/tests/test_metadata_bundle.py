import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse
import zipfile
from test_fleetci import MemoryForge

spec = importlib.util.spec_from_file_location('bundle_ci', Path(__file__).resolve().parents[1] / 'fleetci.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class CanonicalForge(MemoryForge):
    provider = 'gitflic'


class MetadataBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'canonical'
        self.source.mkdir()
        (self.source / 'payload.bin').write_bytes(b'unchanged executable bytes')
        (self.source / 'signed-manifest.json').write_bytes(b'{"signed":"same manifest bytes"}\n')
        self.record = ci.finish_record(self.source, 'gateway', 'v1.0.0', 'a' * 40)
        self.remote = CanonicalForge()

    def publish(self):
        return ci.publish(self.remote, self.source, self.record)

    def replace_bundle(self, entries):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as z:
            for name, value in entries:
                z.writestr(name, value)
        self.remote.data[ci.METADATA_ZIP] = output.getvalue()

    def test_supported_archive_is_last_and_retry_is_idempotent(self):
        self.publish()
        self.assertEqual(list(self.remote.data), ['payload.bin', ci.METADATA_ZIP])
        first = self.remote.data[ci.METADATA_ZIP]
        count = self.remote.uploads
        self.publish()
        self.assertEqual(self.remote.uploads, count)
        self.assertEqual(first, self.remote.data[ci.METADATA_ZIP])
        repeated = ci.finish_record(self.source, 'gateway', 'v1.0.0', 'a' * 40)
        self.assertEqual(repeated, self.record)

    def test_mirror_unpacks_exact_metadata_and_copies_original_payload_bytes(self):
        self.publish()
        out = self.root / 'mirror'
        got = ci.fetch_canonical(self.remote, self.root, out, {'component': 'gateway'}, 'v1.0.0', 'a' * 40)
        self.assertEqual(got, self.record)
        for name in ('payload.bin', 'signed-manifest.json', 'SHA256SUMS', 'release-record.json'):
            self.assertEqual((out / name).read_bytes(), (self.source / name).read_bytes())
        mirror = MemoryForge()
        ci.publish(mirror, out, got)
        self.assertEqual(mirror.data['payload.bin'], self.remote.data['payload.bin'])
        self.assertEqual(list(mirror.data)[-1], 'release-record.json')

    def test_modified_signed_metadata_cannot_be_published(self):
        self.publish()
        entries = ci.read_metadata_bundle(self.remote.data[ci.METADATA_ZIP])
        entries['signed-manifest.json'] = b'{"signed":"tampered"}'
        self.replace_bundle(entries.items())
        out = self.root / 'rejected'
        with self.assertRaisesRegex(ci.Failure, 'canonical_checksum_mismatch'):
            ci.fetch_canonical(self.remote, self.root, out, {'component': 'gateway'}, 'v1.0.0', 'a' * 40)
        self.assertFalse((out / 'release-record.json').exists())

    def test_ready_archive_does_not_hide_a_missing_binary(self):
        self.publish()
        del self.remote.data['payload.bin']
        with self.assertRaises(ci.Pending):
            ci.fetch_canonical(self.remote, self.root, self.root / 'missing', {'component': 'gateway'}, 'v1.0.0', 'a' * 40)

    def test_duplicate_traversal_symlink_and_undeclared_members_are_rejected(self):
        self.publish()
        entries = list(ci.read_metadata_bundle(self.remote.data[ci.METADATA_ZIP]).items())
        for extra in [('release-record.json', b'{}'), ('../escape.json', b'{}'), ('undeclared.json', b'{}')]:
            with self.subTest(member=extra[0]):
                self.replace_bundle(entries + [extra])
                with self.assertRaises(ci.Failure):
                    ci.read_metadata_bundle(self.remote.data[ci.METADATA_ZIP])
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as z:
            for name, value in entries:
                z.writestr(name, value)
            link = zipfile.ZipInfo('link.json')
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(link, b'/private')
        with self.assertRaises(ci.Failure):
            ci.read_metadata_bundle(output.getvalue())

    def test_wrong_source_commit_is_rejected_before_metadata_is_saved(self):
        self.publish()
        out = self.root / 'wrong-commit'
        with self.assertRaisesRegex(ci.Failure, 'canonical_release_identity_mismatch'):
            ci.fetch_canonical(self.remote, self.root, out, {'component': 'gateway'}, 'v1.0.0', 'b' * 40)
        self.assertFalse(out.exists())

    def test_corrupt_archive_is_refused(self):
        with self.assertRaisesRegex(ci.Failure, 'metadata_archive_invalid'):
            ci.read_metadata_bundle(b'not a ZIP')


class PublicChannelMetadataTests(unittest.TestCase):
    def documents(self):
        return ci.channel_documents({'gitflic_owner': 'distributed-workers', 'gitflic_project': 'worker-android'},
            {'tag': 'v1.0.0', 'commit': 'a' * 40}, {'version_code': 7, 'sha256': 'b' * 64},
            'https://gitflic.ru/project/distributed-workers/worker-android/release/example/apk/download')

    def test_json_locators_are_immutable_versioned_public_git_files(self):
        channel, paths = self.documents()
        self.assertEqual(channel['version_code'], 7)
        for key, path in paths.items():
            uri = urllib.parse.urlsplit(channel[key])
            self.assertEqual(uri.hostname, 'gitflic.ru')
            self.assertEqual(urllib.parse.parse_qs(uri.query), {'file': [path], 'commit': ['release-channel']})
            self.assertTrue(path.startswith('releases/v1.0.0/'))
            self.assertNotIn('/api/', uri.path)

    def test_nondefault_raw_ref_uses_live_gitflic_commit_selector_contract(self):
        # The public GitFlic frontend's raw links use commit=. Live probes proved
        # branch= ignored: main/default lacks these files and returns 404.
        channel, paths = self.documents()
        files = {path: path.encode() for path in paths.values()}
        class GitFlicRaw:
            def __init__(self, base):
                pass
            def request(self, method, address, **kwargs):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(address).query)
                if query.get('commit', ['main'])[0] != 'release-channel':
                    return None
                return files.get(query['file'][0])
        with patch.object(ci, 'HTTP', GitFlicRaw):
            ci.verify_channel_metadata(channel, paths, files)
            wrong = {key: value.replace('&commit=', '&branch=') if isinstance(value, str) else value
                     for key, value in channel.items()}
            with self.assertRaisesRegex(ci.Temporary, 'channel_metadata_not_indexed_yet'):
                ci.verify_channel_metadata(wrong, paths, files)

    def test_raw_public_files_must_match_and_pending_indexing_is_resumable(self):
        channel, paths = self.documents()
        files = {path: path.encode() for path in paths.values()}
        class Public:
            def __init__(self, base):
                self.base = base
            def request(self, method, address, **kwargs):
                path = urllib.parse.parse_qs(urllib.parse.urlsplit(address).query)['file'][0]
                return files[path]
        with patch.object(ci, 'HTTP', Public):
            ci.verify_channel_metadata(channel, paths, files)
        with patch.object(Public, 'request', return_value=None), patch.object(ci, 'HTTP', Public):
            with self.assertRaises(ci.Temporary):
                ci.verify_channel_metadata(channel, paths, files)
        with patch.object(Public, 'request', return_value=b'changed'), patch.object(ci, 'HTTP', Public):
            with self.assertRaisesRegex(ci.Failure, 'anonymous_channel_metadata_checksum'):
                ci.verify_channel_metadata(channel, paths, files)
