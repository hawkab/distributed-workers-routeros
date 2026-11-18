import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve()
MODULE = HERE.parents[1] / "fleetci.py"
if not MODULE.exists():
    MODULE = HERE.parents[1] / "_shared/fleetci.py"
spec = importlib.util.spec_from_file_location("fleetci", MODULE)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class MemoryForge:
    provider = "memory"

    def __init__(self):
        self.saved = None
        self.data = {}
        self.uploads = 0
        self.uncertain_once = False

    def release(self, tag):
        return self.saved

    def create(self, record):
        self.saved = {"id": "r", "commitId": record["commit"], "description":
            "fleet-release:" + record["component"] + ":" + record["tag"] + ":" + record["commit"]}
        return self.saved

    def assets(self, release):
        return [{"name": x} for x in self.data]

    def upload(self, release, path):
        self.uploads += 1
        self.data[path.name] = path.read_bytes()
        if self.uncertain_once:
            self.uncertain_once = False
            raise ci.Temporary("network_response_lost")

    def download(self, release, asset):
        return self.data[asset["name"]]


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "gateway-v1.0.0.tar.gz").write_bytes(b"real artifact bytes")
        self.record = ci.finish_record(self.root, "gateway", "v1.0.0", "a" * 40)

    def test_publish_retry_is_idempotent_and_record_last(self):
        remote = MemoryForge()
        first = ci.publish(remote, self.root, self.record)
        uploads = remote.uploads
        self.assertEqual(list(remote.data)[-1], "release-record.json")
        self.assertEqual(ci.publish(remote, self.root, self.record), first)
        self.assertEqual(remote.uploads, uploads)

    def test_existing_changed_asset_is_never_overwritten(self):
        remote = MemoryForge()
        remote.create(self.record)
        remote.data["gateway-v1.0.0.tar.gz"] = b"different existing bytes"
        with self.assertRaisesRegex(ci.Failure, "existing_asset_checksum_conflict"):
            ci.publish(remote, self.root, self.record)
        self.assertEqual(remote.uploads, 0)
        self.assertEqual(remote.data["gateway-v1.0.0.tar.gz"], b"different existing bytes")

    def test_uncertain_upload_reconciles_before_retry(self):
        remote = MemoryForge()
        remote.uncertain_once = True
        with self.assertRaises(ci.Temporary):
            ci.publish(remote, self.root, self.record)
        ci.publish(remote, self.root, self.record)
        self.assertEqual(remote.uploads, len(remote.data))

    def test_other_source_commit_conflicts_before_upload(self):
        remote = MemoryForge()
        remote.create(dict(self.record, commit="b" * 40))
        with self.assertRaisesRegex(ci.Failure, "existing_release_identity_conflict"):
            ci.publish(remote, self.root, self.record)
        self.assertEqual(remote.uploads, 0)

    def test_changed_checksum_file_is_rejected(self):
        (self.root / "SHA256SUMS").write_text("0" * 64 + "  gateway-v1.0.0.tar.gz\n")
        with self.assertRaisesRegex(ci.Failure, "checksum_list_mismatch"):
            ci.verify_record(self.root, "gateway", "v1.0.0", "a" * 40)

    def test_local_checksum_and_traversal_are_rejected(self):
        (self.root / "gateway-v1.0.0.tar.gz").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ci.Failure, "artifact_checksum_mismatch"):
            ci.verify_record(self.root, "gateway", "v1.0.0", "a" * 40)
        record = dict(self.record, files=[dict(self.record["files"][0], name="../secret")])
        (self.root / "release-record.json").write_text(json.dumps(record))
        with self.assertRaisesRegex(ci.Failure, "invalid_release_record"):
            ci.verify_record(self.root, "gateway", "v1.0.0", "a" * 40)

    def test_pending_has_no_short_attempt_exhaustion_and_state_is_resumable(self):
        calls = []
        def work():
            calls.append(True)
            if len(calls) <= 30:
                raise ci.Pending("canonical_release_pending")
            return {"published": True}
        with patch.object(ci.time, "sleep"):
            self.assertTrue(ci.retry(work, self.root / "state.json")["published"])
        self.assertEqual(len(calls), 31)
        self.assertEqual(json.loads((self.root / "state.json").read_text())["state"], "complete")
        with self.assertRaises(ci.Pending):
            ci.retry(lambda: (_ for _ in ()).throw(ci.Pending("pending")), self.root / "state.json", once=True)
        self.assertEqual(json.loads((self.root / "state.json").read_text())["state"], "pending")

    def test_canonical_tampering_is_not_attached_to_mirror(self):
        remote = MemoryForge()
        ci.publish(remote, self.root, self.record)
        remote.data["gateway-v1.0.0.tar.gz"] = b"tampered canonical bytes"
        target = self.root / "mirror"
        target.mkdir()
        with self.assertRaisesRegex(ci.Failure, "canonical_checksum_mismatch"):
            ci.fetch_canonical(remote, self.root, target, {"component": "gateway"}, "v1.0.0", "a" * 40)
        self.assertFalse((target / "release-record.json").exists())

    def test_archives_have_reproducible_bytes(self):
        entries = [("b", b"two", 0o644), ("a", b"one", 0o755)]
        for zip_format, suffix in ((False, ".tar.gz"), (True, ".zip")):
            a, b = self.root / ("a" + suffix), self.root / ("b" + suffix)
            ci.archive(a, entries, 1234567890, zip_format=zip_format)
            ci.archive(b, list(reversed(entries)), 1234567890, zip_format=zip_format)
            self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_tokens_only_go_to_matching_api_and_official_upload_host(self):
        client = ci.HTTP("https://api.github.com", "never-log-this")
        self.assertEqual(client.token_hosts, {"api.github.com", "uploads.github.com"})
        self.assertNotIn("example.invalid", client.token_hosts)

    def test_public_release_base_expands_tag_without_legacy_gateway_suffix(self):
        base="https://example.invalid/git/team/worker/releases/download/{tag}"
        self.assertEqual(ci.public_apk_url(base,"v0.2.0"),"https://example.invalid/git/team/worker/releases/download/v0.2.0/fleet-worker-android.apk")
        with self.assertRaises(ci.Failure):
            ci.public_apk_url("https://user:token@example.invalid/releases/{tag}","v0.2.0")

    def test_auth_failure_is_not_retryable_and_redirect_does_not_leak_token(self):
        client=ci.HTTP("https://api.github.com","publisher-secret")
        class Forbidden:
            def open(self,*a,**kw):
                raise ci.urllib.error.HTTPError("https://api.github.com",403,"Forbidden",{},None)
        client.opener=Forbidden()
        with self.assertRaises(ci.Failure) as result:
            client.request("GET","/repos/owner/repo")
        self.assertNotIsInstance(result.exception,ci.Temporary)
        calls=[]
        class Redirect:
            def open(self,req,**kw):
                calls.append(req)
                if len(calls)==1:
                    raise ci.urllib.error.HTTPError(req.full_url,302,"Found",{"Location":"https://cdn.example.invalid/artifact"},None)
                return ci.io.BytesIO(b"bytes")
        client.opener=Redirect()
        self.assertEqual(client.request("GET","/asset"),b"bytes")
        self.assertEqual(calls[0].get_header("Authorization"),"Bearer publisher-secret")
        self.assertIsNone(calls[1].get_header("Authorization"))

    def test_anonymous_canonical_rejects_wrong_tag_commit_marker(self):
        remote = MemoryForge()
        ci.publish(remote, self.root, self.record)
        remote.saved["description"] = "fleet-release:gateway:v9.0.0:" + "a" * 40
        with self.assertRaisesRegex(ci.Failure, "canonical_release_identity_mismatch"):
            ci.fetch_canonical(remote, self.root, self.root / "mirror", {"component":"gateway"}, "v1.0.0", "a" * 40)


class PublicHTMLTests(unittest.TestCase):
    def setUp(self):
        self.catalog = (HERE.parent / "fixtures/gitflic-public-releases.html").read_bytes()
        self.detail = (HERE.parent / "fixtures/gitflic-public-release.html").read_bytes()
        self.requested = []
        self.page_filter = lambda page, data: data
        parent = self
        class Transport:
            def request(self, method, address, **kwargs):
                parent.requested.append(address)
                if "/release?page=" in address:
                    page = int(address.rsplit("=", 1)[1])
                    return parent.page_filter(page, parent.catalog)
                return parent.detail
        self.web = ci.GitFlicWeb("notyfile/notyfile-releases", Transport())

    def test_real_public_html_contains_exact_tag_commit_and_download_asset(self):
        release = self.web.release("v1.0.0-rc53")
        self.assertEqual(release["id"], "75f84094-25e4-4845-9388-59c662b217f5")
        self.assertEqual(release["commitId"], "76c6a0ce6896f5a521d552775fa9acdcd1614803")
        self.assertEqual(release["attachmentFiles"][0]["name"], "notyfile-1.0.0-rc53.apk")
        self.assertTrue(release["attachmentFiles"][0]["link"].endswith("/5db80e01-523c-4d5d-a846-9ef7645580b5/download"))

    def test_similar_tag_is_not_an_exact_match(self):
        self.assertIsNone(self.web.release("v1.0.0-rc5"))

    def test_detail_changed_tag_or_commit_is_rejected(self):
        self.detail = self.detail.replace(b"v1.0.0-rc53", b"v1.0.0-rc99")
        with self.assertRaisesRegex(ci.Failure,"gitflic_release_tag_mismatch"):
            self.web.release("v1.0.0-rc53")

    def test_pagination_follows_observed_total_without_page_cap(self):
        def pages(page, data):
            data = data.replace(b"totalPages: 1", b"totalPages: 3").replace(b"number: 0", ("number: " + str(page)).encode())
            return data.replace(b"v1.0.0-rc53", b"v1.0.0-rc600") if page < 2 else data
        self.page_filter = pages
        self.assertEqual(self.web.release("v1.0.0-rc53")["tagName"], "v1.0.0-rc53")
        self.assertEqual([a for a in self.requested if "?page=" in a], ["/project/notyfile/notyfile-releases/release?page="+str(x) for x in (0,1,2)])

    def test_nonadvancing_pagination_and_login_markup_fail_closed(self):
        self.page_filter = lambda page, data: data.replace(b"totalPages: 1", b"totalPages: 2")
        with self.assertRaisesRegex(ci.Failure,"pagination_did_not_advance"):
            self.web.release("v7.7.7")
        self.catalog = b"<html><form>Login required</form></html>"
        with self.assertRaisesRegex(ci.Failure,"public_catalog_unrecognized"):
            self.web.release("v7.7.7")

    def test_public_adapter_cannot_inherit_publisher_credentials(self):
        with patch.dict(ci.os.environ,{"GITFLIC_TOKEN":"do-not-copy-publisher-token"}):
            web = ci.forge("gitflic",{"component":"gateway","gitflic_owner":"distributed-workers","gitflic_project":"gateway"},canonical=True)
        self.assertIsInstance(web,ci.GitFlicWeb)
        self.assertEqual(web.http.token,"")


if __name__ == "__main__":
    unittest.main()
