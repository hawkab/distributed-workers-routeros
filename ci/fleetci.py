#!/usr/bin/env python3
"""Self-contained component CI. No platform credentials or signing keys in Git.

GitFlic builds canonical releases; mirrors copy the checked canonical bytes.
Retry restarts from server state, including after an uncertain POST response.
"""
import argparse
import base64
import contextlib
import gzip
import hashlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
HEX = re.compile(r"^[0-9a-f]{64}$")
ANDROID_MANIFEST = "android-manifest.json"
ANDROID_APK = "fleet-worker-android.apk"
METADATA_ZIP = "release-metadata.zip"


class Failure(Exception):
    pass


class Temporary(Failure):
    pass


class Pending(Temporary):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sanitize_output(raw, environment):
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    values = []
    def private_urls(value):
        if isinstance(value, dict):
            for child in value.values(): private_urls(child)
        elif isinstance(value, list):
            for child in value: private_urls(child)
        elif isinstance(value, str) and value.startswith(("https://", "http://")):
            values.append(value)
            host = urllib.parse.urlsplit(value).hostname
            if host: values.append(host)
    for key, value in environment.items():
        if len(value) >= 8 and re.search(r"token|password|secret|private_key|database_url|amqp_url|release_sources|release_base|deployment|gitea_.*url", key, re.I):
            values.append(value)
            private_urls(value)
            try: private_urls(json.loads(value))
            except (ValueError, TypeError): pass
    for value in sorted(set(values), key=len, reverse=True):
        text = text.replace(value, "<redacted>")
    return text


def check_go_results(output, returncode=0):
    try:
        events = [json.loads(line) for line in output.splitlines() if line.strip()]
    except (ValueError, TypeError):
        raise Failure("gateway_test_output_invalid") from None
    # go test reports package-level skip for CLI packages with no test files.
    # A named skipped integration test is materially different and must fail CI.
    if returncode or any(e.get("Action") == "fail" or (e.get("Action") == "skip" and e.get("Test")) for e in events):
        raise Failure("gateway_tests_failed_or_skipped")
    if not any(e.get("Action") == "pass" and e.get("Test") for e in events):
        raise Failure("gateway_tests_not_executed")
    return events


def encoded(value):
    return urllib.parse.quote(str(value), safe="")


def run(argv, *, cwd=None, env=None, data=None):
    # argv may contain file paths to secrets, never print it or captured errors.
    p = subprocess.run([str(x) for x in argv], cwd=cwd, env=env, input=data,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise Failure("command_failed:" + Path(str(argv[0])).name)
    return p.stdout


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    with tmp.open("wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTP:
    def __init__(self, base, token="", scheme="Bearer"):
        self.base = base.rstrip("/")
        self.token = token
        self.scheme = scheme
        if urllib.parse.urlsplit(self.base).scheme != "https":
            raise Failure("api_https_required")
        self.opener = urllib.request.build_opener(NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self.token_hosts = {urllib.parse.urlsplit(self.base).netloc}
        if urllib.parse.urlsplit(self.base).netloc == "api.github.com":
            self.token_hosts.add("uploads.github.com")

    def request(self, method, path, body=None, content_type=None, accept="application/json", missing=False):
        address = path if path.startswith("https://") else self.base + path
        seen = set()
        while True:
            u = urllib.parse.urlsplit(address)
            if u.scheme != "https" or u.username or address in seen:
                raise Failure("invalid_download_redirect")
            seen.add(address)
            headers = {"Accept": accept, "User-Agent": "fleet-component-ci/1"}
            # Do not leak API tokens to object storage/download redirects.
            if self.token and u.netloc in self.token_hosts:
                headers["Authorization"] = self.scheme + " " + self.token
            if content_type:
                headers["Content-Type"] = content_type
            req = urllib.request.Request(address, data=body, method=method, headers=headers)
            try:
                response = self.opener.open(req, timeout=120)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308) and method == "GET":
                    address = urllib.parse.urljoin(address, e.headers.get("Location", ""))
                    e.close()
                    continue
                if e.code == 404 and missing:
                    e.close()
                    return None
                code = e.code
                e.close()
                if code in (408, 425, 429) or code >= 500:
                    raise Temporary("http_" + str(code)) from None
                if code in (409, 422):
                    # A concurrent/uncertain upload may have succeeded. Reconcile first.
                    raise Temporary("http_conflict_reconcile") from None
                raise Failure("http_" + str(code)) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                raise Temporary("network_unavailable") from None
            with response:
                return response.read()

    def json(self, method, path, value=None, missing=False):
        body = json_bytes(value) if value is not None else None
        raw = self.request(method, path, body, "application/json" if body else None, missing=missing)
        if raw is None:
            return None
        try:
            return json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            raise Failure("invalid_api_json") from None


def multipart(field, path):
    boundary = "fleet-" + secrets.token_hex(24)
    name = Path(path).name
    if not NAME.fullmatch(name):
        raise Failure("invalid_asset_name")
    media_type = "application/zip" if name.endswith(".zip") else "application/octet-stream"
    prefix = ("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + field +
              "\"; filename=\"" + name + "\"\r\nContent-Type: " + media_type + "\r\n\r\n").encode()
    return prefix + Path(path).read_bytes() + ("\r\n--" + boundary + "--\r\n").encode(), \
        "multipart/form-data; boundary=" + boundary


class Forge:
    def __init__(self, provider, repository, http):
        self.provider, self.repository, self.http = provider, repository, http
        if "/" not in repository or not all(repository.rsplit("/", 1)):
            raise Failure("repository_owner_and_name_required")
        owner, name = repository.rsplit("/", 1)
        self.path = ("/project/" + encoded(owner) + "/" + encoded(name)) if provider == "gitflic" \
            else "/repos/" + "/".join(encoded(x) for x in repository.split("/"))

    def release(self, tag):
        if self.provider != "gitflic":
            return self.http.json("GET", self.path + "/releases/tags/" + encoded(tag), missing=True)
        page = 0
        while True:
            result = self.http.json("GET", self.path + "/release?size=100&page=" + str(page))
            items = result.get("_embedded", {}).get("releaseTagModelList", [])
            for item in items:
                if item.get("tagName") == tag:
                    return item
            info = result.get("page", {})
            if not items or ("totalPages" in info and page + 1 >= int(info["totalPages"])) or len(items) < 100:
                return None
            page += 1

    def assets(self, release):
        if self.provider == "gitflic":
            return release.get("attachmentFiles") or []
        result, page = [], 1
        while True:
            items = self.http.json("GET", self.path + "/releases/" + str(release["id"]) +
                "/assets?per_page=100&limit=100&page=" + str(page))
            if not isinstance(items, list):
                raise Failure("invalid_asset_list")
            result.extend(items)
            if len(items) < 100:
                return result
            page += 1

    def download(self, release, asset):
        if self.provider == "gitflic":
            address = self.path + "/release/" + release["id"] + "/file/" + asset["uuid"]
        elif self.provider == "github":
            address = self.path + "/releases/assets/" + str(asset["id"])
        else:
            address = asset["browser_download_url"]
        return self.http.request("GET", address, accept="application/octet-stream")

    def create(self, record):
        marker = "fleet-release:" + record["component"] + ":" + record["tag"] + ":" + record["commit"]
        if self.provider == "gitflic":
            value = {"title": record["tag"], "description": marker, "tagName": record["tag"],
                     "preRelease": False, "attachProjectArchive": False}
            return self.http.json("POST", self.path + "/release", value)
        value = {"tag_name": record["tag"], "target_commitish": record["commit"],
                 "name": record["tag"], "body": marker, "draft": False, "prerelease": False}
        return self.http.json("POST", self.path + "/releases", value)

    def upload(self, release, path):
        if self.provider == "gitflic":
            data, content = multipart("files", path)
            self.http.request("POST", self.path + "/release/" + release["id"] + "/file", data, content)
        elif self.provider == "gitea":
            data, content = multipart("attachment", path)
            self.http.request("POST", self.path + "/releases/" + str(release["id"]) +
                              "/assets?name=" + encoded(path.name), data, content)
        else:
            address = release["upload_url"].split("{", 1)[0] + "?name=" + encoded(path.name)
            self.http.request("POST", address, path.read_bytes(), "application/octet-stream")


class Node:
    def __init__(self, tag="", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        return " ".join("".join(x.text() if isinstance(x, Node) else x for x in self.children).split())


class HTMLTree(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(raw)
        self.close()

    def handle_starttag(self, tag, attrs):
        child = Node(tag, attrs)
        self.stack[-1].children.append(child)
        if tag not in self.VOID:
            self.stack.append(child)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


class GitFlicWeb:
    """Read-only anonymous SSR adapter. Any markup/identity drift fails closed."""
    provider = "gitflic-web"

    def __init__(self, repository, http=None):
        if not repository or not all(repository.split("/")):
            raise Failure("canonical_repository_required")
        self.path = "/project/" + "/".join(encoded(x) for x in repository.split("/"))
        self.http = http or HTTP("https://gitflic.ru")

    def html(self, path):
        raw = self.http.request("GET", path, accept="text/html")
        try:
            return HTMLTree(raw.decode("utf-8")).root
        except (UnicodeError, ValueError):
            raise Failure("gitflic_public_html_invalid") from None

    def release(self, tag):
        page = 0
        release_pattern = re.compile(re.escape(self.path) + r"/release/([0-9a-f-]{36})$")
        commit_pattern = re.compile(re.escape(self.path) + r"/commit/([0-9a-f]{40})$")
        while True:
            tree = self.html(self.path + "/release?page=" + str(page))
            nodes = list(tree.walk())
            if not any("release-page" in n.attrs.get("class", "").split() for n in nodes):
                raise Failure("gitflic_public_catalog_unrecognized")
            matching = []
            for item in (n for n in nodes if n.tag == "li"):
                children = list(item.walk())
                if not any(n.tag == "span" and n.text() == tag for n in children):
                    continue
                links = [n.attrs.get("href", "") for n in children if n.tag == "a" and release_pattern.fullmatch(n.attrs.get("href", ""))]
                commits = [commit_pattern.fullmatch(n.attrs.get("href", "")).group(1) for n in children
                           if n.tag == "a" and commit_pattern.fullmatch(n.attrs.get("href", ""))]
                if len(links) == 1 and len(commits) == 1:
                    matching.append((links[0], commits[0]))
            if len(matching) > 1:
                raise Failure("gitflic_duplicate_tag")
            if matching:
                address, commit = matching[0]
                return self.detail(address, tag, commit)
            pagers = [n for n in nodes if n.tag == "pagination"]
            if not pagers:
                return None
            if len(pagers) != 1:
                raise Failure("gitflic_pagination_ambiguous")
            pager = pagers[0].attrs.get(":pager", "")
            total = re.search(r"totalPages\s*:\s*(\d+)", pager)
            current = re.search(r"number\s*:\s*(\d+)", pager)
            if not total or not current or int(current.group(1)) != page:
                raise Failure("gitflic_pagination_did_not_advance")
            if page + 1 >= int(total.group(1)):
                return None
            page += 1

    def detail(self, address, tag, commit):
        tree = self.html(address)
        cards = [n for n in tree.walk() if "card" in n.attrs.get("class", "").split()]
        if len(cards) != 1:
            raise Failure("gitflic_release_card_unrecognized")
        nodes = list(cards[0].walk())
        if not any(n.tag == "span" and n.text() == tag for n in nodes):
            raise Failure("gitflic_release_tag_mismatch")
        commits = [n.attrs.get("href", "") for n in nodes if n.tag == "a" and
                   n.attrs.get("href", "").startswith(self.path + "/commit/")]
        if commits != [self.path + "/commit/" + commit]:
            raise Failure("gitflic_release_commit_mismatch")
        descriptions = [n.text() for n in nodes if "markdown-body" in n.attrs.get("class", "").split()]
        if len(descriptions) != 1:
            raise Failure("gitflic_release_description_missing")
        asset_pattern = re.compile(re.escape(address) + r"/([0-9a-f-]{36})/download$")
        assets = []
        for node in nodes:
            if node.tag != "a":
                continue
            match = asset_pattern.fullmatch(node.attrs.get("href", ""))
            if match:
                if not NAME.fullmatch(node.text()):
                    raise Failure("gitflic_public_asset_name_invalid")
                assets.append({"name": node.text(), "uuid": match.group(1),
                               "link": "https://gitflic.ru" + node.attrs["href"]})
        if len({x["name"] for x in assets}) != len(assets):
            raise Failure("gitflic_public_duplicate_asset")
        return {"id": address.rsplit("/", 1)[1], "tagName": tag, "commitId": commit,
                "description": descriptions[0], "attachmentFiles": assets}

    def assets(self, release):
        return release["attachmentFiles"]

    def download(self, release, asset):
        return self.http.request("GET", asset["link"], accept="application/octet-stream")


def forge(provider, config, *, canonical=False):
    env = os.environ
    if provider == "gitflic":
        owner = env.get("FLEET_GITFLIC_OWNER", config.get("gitflic_owner", ""))
        project = env.get("FLEET_GITFLIC_PROJECT", config.get("gitflic_project", config["component"]))
        if canonical:
            return GitFlicWeb(owner + "/" + project)
        return Forge(provider, owner + "/" + project,
            HTTP(env.get("FLEET_GITFLIC_API", "https://api.gitflic.ru"), env.get("GITFLIC_TOKEN", ""), "token"))
    if provider == "gitea":
        return Forge(provider, env.get("GITEA_REPOSITORY", env.get("GITHUB_REPOSITORY", "")),
            HTTP(required("GITEA_API_URL"), env.get("FLEET_GITEA_TOKEN", env.get("GITEA_TOKEN", "")), "token"))
    return Forge(provider, required("GITHUB_REPOSITORY"),
        HTTP(env.get("GITHUB_API_URL", "https://api.github.com"), env.get("GITHUB_TOKEN", "")))


def required(name):
    value = os.environ.get(name, "")
    if not value:
        raise Failure("missing_setting:" + name)
    return value


def public_apk_url(base, tag):
    base = base.replace("{tag}", encoded(tag)).rstrip("/")
    uri = urllib.parse.urlsplit(base)
    if uri.scheme != "https" or not uri.hostname or uri.username or uri.query or uri.fragment or any(c in base for c in "\r\n"):
        raise Failure("invalid_public_release_base")
    return base + "/" + ANDROID_APK


def artifact_entries(directory):
    return [{"name": p.name, "sha256": sha(p.read_bytes()), "size": p.stat().st_size}
            for p in sorted(Path(directory).iterdir()) if p.is_file() and
            p.name not in ("release-record.json", "SHA256SUMS", "release-state.json", METADATA_ZIP) and not p.name.endswith(".partial")]


def finish_record(directory, component, tag, commit):
    record = {"schema": 1, "component": component, "tag": tag, "commit": commit,
              "files": artifact_entries(directory)}
    if not record["files"]:
        raise Failure("empty_release")
    save(Path(directory) / "release-record.json", json_bytes(record))
    files = record["files"] + [{"name": "release-record.json", "sha256": sha(json_bytes(record))}]
    save(Path(directory) / "SHA256SUMS", "".join(x["sha256"] + "  " + x["name"] + "\n" for x in files).encode())
    return record


def metadata_bundle_names(record):
    names = {"SHA256SUMS", "release-record.json"}
    files = record.get("files") if isinstance(record, dict) else None
    if not isinstance(files, list) or not files:
        raise Failure("metadata_record_invalid")
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not NAME.fullmatch(item["name"]) or item["name"] in names | {METADATA_ZIP, "release-state.json"}:
            raise Failure("metadata_record_invalid")
        if item["name"].endswith(".json"):
            names.add(item["name"])
    return names


def create_metadata_bundle(directory, record):
    directory = Path(directory)
    entries = [(name, (directory / name).read_bytes(), 0o644) for name in metadata_bundle_names(record)]
    archive(directory / METADATA_ZIP, entries, 0, zip_format=True)
    return directory / METADATA_ZIP


def read_metadata_bundle(raw):
    """Read root-level metadata only; never extract an untrusted ZIP to disk."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as package:
            entries = package.infolist()
            names = [entry.filename for entry in entries]
            if len(set(names)) != len(names) or any(not NAME.fullmatch(entry.filename) or entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1 for entry in entries):
                raise Failure("metadata_archive_entries_invalid")
            values = {entry.filename: package.read(entry) for entry in entries}
        record = json.loads(values["release-record.json"])
        if set(values) != metadata_bundle_names(record):
            raise Failure("metadata_archive_members_mismatch")
        return values
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
        raise Failure("metadata_archive_invalid") from None


def verify_record(directory, component, tag, commit):
    directory = Path(directory)
    try:
        record = json.loads((directory / "release-record.json").read_bytes())
        if record["schema"] != 1 or (record["component"], record["tag"], record["commit"]) != (component, tag, commit):
            raise Failure("release_identity_mismatch")
        seen = set()
        if not record["files"]:
            raise Failure("empty_release")
        for item in record["files"]:
            name = item["name"]
            if not NAME.fullmatch(name) or name in seen or not HEX.fullmatch(item["sha256"]):
                raise Failure("invalid_release_record")
            seen.add(name)
            raw = (directory / name).read_bytes()
            if sha(raw) != item["sha256"] or len(raw) != item["size"]:
                raise Failure("artifact_checksum_mismatch")
        expected = {x["name"]: x["sha256"] for x in record["files"]}
        expected["release-record.json"] = sha((directory / "release-record.json").read_bytes())
        lines = (directory / "SHA256SUMS").read_text().splitlines()
        actual = {}
        for line in lines:
            digest, name = line.split("  ", 1)
            if not HEX.fullmatch(digest) or not NAME.fullmatch(name) or name in actual:
                raise Failure("invalid_checksum_list")
            actual[name] = digest
        if actual != expected:
            raise Failure("checksum_list_mismatch")
        return record
    except (KeyError, ValueError, TypeError, OSError):
        raise Failure("invalid_release_record") from None


def apk_tools():
    sdk = Path(os.environ.get("ANDROID_HOME", os.environ.get("ANDROID_SDK_ROOT", "")))
    tools = sdk / "build-tools" / os.environ.get("FLEET_ANDROID_BUILD_TOOLS", "36.0.0")
    return Path(os.environ.get("FLEET_APKSIGNER", str(tools / "apksigner"))), tools / "aapt"


def android_root(root):
    return root if (root / "gradlew").is_file() else root / "android"


def verify_public_apk(root, apk, environment=None):
    """Public binaries contain safe locators only, never operator configuration."""
    environment = dict(os.environ if environment is None else environment)
    safe_hosts = {"gitea.invalid", "gitflic.ru", "api.gitflic.ru", "github.com", "api.github.com"}
    template = android_root(Path(root)) / "release-sources.example.json"
    try:
        expected = json.loads(template.read_bytes())
        with zipfile.ZipFile(apk) as package:
            actual = json.loads(package.read("assets/release-sources.json"))
            if actual != expected or not isinstance(actual, list) or [x.get("id") for x in actual] != ["gitea", "gitflic", "github"]:
                raise Failure("public_apk_source_configuration_mismatch")
            fields = {"id", "type", "url", "manifest_asset", "apk_asset", "apk_url"}
            for source in actual:
                if set(source) - fields:
                    raise Failure("public_apk_unknown_source_field")
                for name in ("url", "apk_url"):
                    if name not in source: continue
                    uri = urllib.parse.urlsplit(source[name])
                    if uri.scheme != "https" or uri.hostname not in safe_hosts or uri.username or uri.fragment:
                        raise Failure("public_apk_private_source_endpoint")
                    if re.search(r"token|password|authorization|credential|secret", uri.query, re.I):
                        raise Failure("public_apk_credential_query")
            known = [value for key, value in environment.items() if len(value) >= 16 and re.search(r"token|password|secret", key, re.I)]
            for entry in package.infolist():
                if entry.is_dir(): continue
                data = package.read(entry)
                if re.search(rb"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY", data):
                    raise Failure("public_apk_private_key_material")
                for value in known:
                    if value.encode() in data or value.encode("utf-16-le") in data:
                        raise Failure("public_apk_known_credential")
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        raise Failure("public_apk_payload_invalid") from None


def verify_android(root, directory, config):
    directory, root = Path(directory), Path(root)
    try:
        m = json.loads((directory / ANDROID_MANIFEST).read_bytes())
        version = json.loads((root / "ci/version.json").read_bytes())
        fields = ("package_name", "version_code", "version_name", "url", "sha256", "apk_certificate_sha256")
        if m["signature_alg"] != "SHA256withRSA" or m["package_name"] != "local.hwak.fleetworker" or \
           m["version_code"] != version["code"] or m["version_name"] != version["name"]:
            raise Failure("android_manifest_identity")
        if not HEX.fullmatch(m["sha256"]) or not HEX.fullmatch(m["apk_certificate_sha256"]):
            raise Failure("android_manifest_hash")
        url = urllib.parse.urlsplit(m["url"])
        if url.scheme != "https" or url.username or any(c in m["url"] for c in "\r\n"):
            raise Failure("android_release_url")
        apks = list(directory.glob("*.apk"))
        if len(apks) != 1 or sha(apks[0].read_bytes()) != m["sha256"]:
            raise Failure("android_apk_hash")
        pub = android_root(root) / "app/src/main/assets/release-public.der"
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            save(d / "public.pem", run(["openssl", "pkey", "-pubin", "-inform", "DER", "-in", pub]))
            save(d / "signature", base64.b64decode(m["signature"], validate=True))
            save(d / "payload", "".join(str(m[k]) + "\n" for k in fields).encode())
            run(["openssl", "dgst", "-sha256", "-verify", d / "public.pem", "-signature", d / "signature", d / "payload"])
        verify_public_apk(root, apks[0])
        signer, aapt = apk_tools()
        certs = run([signer, "verify", "--verbose", "--print-certs", apks[0]]).decode()
        found = re.findall(r"Signer #[0-9]+ certificate SHA-256 digest: ([0-9a-fA-F]+)", certs)
        if len(found) != 1 or found[0].lower() != m["apk_certificate_sha256"] or found[0].lower() != config["android_certificate_sha256"]:
            raise Failure("android_certificate_mismatch")
        badging = run([aapt, "dump", "badging", apks[0]]).decode()
        match = re.search(r"package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'", badging)
        if not match or match.groups() != (m["package_name"], str(m["version_code"]), m["version_name"]):
            raise Failure("android_apk_identity")
    except (KeyError, ValueError, TypeError, OSError):
        raise Failure("android_verification_failed") from None


def fetch_canonical(remote, root, out, config, tag, commit):
    release = remote.release(tag)
    if release is None:
        raise Pending("canonical_release_pending")
    marker = "fleet-release:" + config["component"] + ":" + tag + ":" + commit
    if release.get("commitId") != commit or marker not in (release.get("description") or ""):
        raise Failure("canonical_release_identity_mismatch")
    assets = remote.assets(release)
    bundles = [a for a in assets if a["name"] == METADATA_ZIP]
    metadata = {}
    if bundles:
        if len(bundles) != 1:
            raise Failure("duplicate_metadata_archive")
        metadata = read_metadata_bundle(remote.download(release, bundles[0]))
        raw = metadata["release-record.json"]
    else:
        records = [a for a in assets if a["name"] == "release-record.json"]
        if not records:
            raise Pending("canonical_artifacts_pending")
        if len(records) != 1:
            raise Failure("duplicate_release_record")
        raw = remote.download(release, records[0])
    try:
        record = json.loads(raw)
        if (record["component"], record["tag"], record["commit"]) != (config["component"], tag, commit):
            raise Failure("canonical_commit_mismatch")
        items = record["files"]
        for item in items:
            name = item["name"]
            if not NAME.fullmatch(name) or not HEX.fullmatch(item["sha256"]):
                raise Failure("canonical_record_invalid")
            found = [a for a in assets if a["name"] == name]
            if len(found) > 1 or (name in metadata and found):
                raise Failure("duplicate_canonical_asset")
            if name not in metadata and not found:
                raise Pending("canonical_artifacts_pending")
            target = out / name
            if not target.is_file() or sha(target.read_bytes()) != item["sha256"]:
                data = metadata[name] if name in metadata else remote.download(release, found[0])
                if sha(data) != item["sha256"] or len(data) != item["size"]:
                    raise Failure("canonical_checksum_mismatch")
                save(target, data)
        save(out / "release-record.json", raw)
        if metadata:
            save(out / "SHA256SUMS", metadata["SHA256SUMS"])
        else:
            sums = [a for a in assets if a["name"] == "SHA256SUMS"]
            if len(sums) != 1:
                raise Pending("canonical_checksums_pending")
            save(out / "SHA256SUMS", remote.download(release, sums[0]))
        verify_record(out, config["component"], tag, commit)
        if config["component"] == "worker-android":
            verify_android(root, out, config)
        return record
    except (KeyError, ValueError, TypeError):
        raise Failure("canonical_record_invalid") from None


def publish(remote, out, record):
    release = remote.release(record["tag"])
    if release is None:
        release = remote.create(record)
    marker = "fleet-release:" + record["component"] + ":" + record["tag"] + ":" + record["commit"]
    description = release.get("description", release.get("body", "")) or ""
    if marker not in description or (release.get("commitId") and release["commitId"] != record["commit"]):
        raise Failure("existing_release_identity_conflict")
    # GitFlic SaaS rejects text/plain and application/json release attachments.
    # Publish their deterministic metadata archive last as the ready marker.
    if remote.provider == "gitflic":
        create_metadata_bundle(out, record)
        names = [x["name"] for x in record["files"] if not x["name"].endswith(".json")] + [METADATA_ZIP]
    else:
        names = [x["name"] for x in record["files"]] + ["SHA256SUMS", "release-record.json"]
    for name in names:
        local = out / name
        expected = sha(local.read_bytes())
        existing = [a for a in remote.assets(release) if a["name"] == name]
        if len(existing) > 1:
            raise Failure("duplicate_release_asset")
        if existing:
            if sha(remote.download(release, existing[0])) != expected:
                raise Failure("existing_asset_checksum_conflict:" + name)
            continue
        remote.upload(release, local)
        # Re-fetch to reconcile even APIs whose upload response is only an ACK.
        release = remote.release(record["tag"])
        existing = [a for a in remote.assets(release) if a["name"] == name]
        if not existing:
            raise Temporary("asset_not_indexed_yet")
        if len(existing) != 1 or sha(remote.download(release, existing[0])) != expected:
            raise Failure("uploaded_asset_checksum_mismatch:" + name)
    return {"component": record["component"], "tag": record["tag"], "commit": record["commit"],
            "provider": remote.provider, "published": True, "assets": len(names)}


def archive(path, entries, epoch, *, zip_format=False):
    entries = sorted(entries, key=lambda x: x[0])
    if zip_format:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for name, data, mode in entries:
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.external_attr = (stat.S_IFREG | mode) << 16
                z.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    else:
        with open(path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz, \
             tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
            for name, data, mode in entries:
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), mode, epoch
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(data))


def amqp_application_ready(uri):
    """AMQP login + virtual-host open, rather than Erlang VM ping readiness."""
    address = urllib.parse.urlsplit(uri)
    def short(value):
        value = value.encode()
        if len(value) > 255:
            raise Failure("test_amqp_short_string_invalid")
        return bytes([len(value)]) + value
    def exact(connection, count):
        chunks = bytearray()
        while len(chunks) < count:
            part = connection.recv(count - len(chunks))
            if not part:
                raise OSError("amqp_startup_connection_closed")
            chunks.extend(part)
        return bytes(chunks)
    def method(connection):
        kind, channel, length = struct.unpack(">BHI", exact(connection, 7))
        payload = exact(connection, length)
        end = exact(connection, 1)
        if kind != 1 or channel != 0 or end != b"\xce" or len(payload) < 4:
            raise Failure("test_amqp_protocol_invalid")
        return struct.unpack(">HH", payload[:4]), payload[4:]
    def send(connection, number, arguments=b""):
        payload = struct.pack(">HH", 10, number) + arguments
        connection.sendall(struct.pack(">BHI", 1, 0, len(payload)) + payload + b"\xce")
    host = address.hostname
    port = address.port or (5671 if address.scheme == "amqps" else 5672)
    with socket.create_connection((host, port), timeout=10) as raw:
        connection = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if address.scheme == "amqps" else raw
        try:
            connection.sendall(b"AMQP\x00\x00\x09\x01")
            number, data = method(connection)
            if number != (10, 10):
                raise OSError("amqp_application_not_ready")
            auth = b"\0" + urllib.parse.unquote(address.username or "guest").encode() + b"\0" + urllib.parse.unquote(address.password or "guest").encode()
            capabilities = short("authentication_failure_close") + b"t\x01"
            properties = short("capabilities") + b"F" + struct.pack(">I", len(capabilities)) + capabilities
            send(connection, 11, struct.pack(">I", len(properties)) + properties + short("PLAIN") + struct.pack(">I", len(auth)) + auth + short("en_US"))
            number, data = method(connection)
            if number == (10, 50):
                if len(data) >= 2 and struct.unpack(">H", data[:2])[0] in (403, 530):
                    raise Failure("test_amqp_authentication_rejected")
                raise OSError("amqp_application_not_ready")
            if number != (10, 30) or len(data) != 8:
                raise OSError("amqp_application_not_ready")
            channels, frame_max, _ = struct.unpack(">HIH", data)
            send(connection, 31, struct.pack(">HIH", channels, frame_max, 0))
            virtual_host = urllib.parse.unquote(address.path[1:]) if address.path not in ("", "/") else "/"
            send(connection, 40, short(virtual_host) + short("") + b"\0")
            number, data = method(connection)
            if number == (10, 50):
                if len(data) >= 2 and struct.unpack(">H", data[:2])[0] in (403, 530):
                    raise Failure("test_amqp_virtual_host_rejected")
                raise OSError("amqp_application_not_ready")
            if number != (10, 41):
                raise OSError("amqp_application_not_ready")
            send(connection, 50, struct.pack(">H", 200) + short("CI readiness") + struct.pack(">HH", 0, 0))
            method(connection)
        finally:
            if connection is not raw:
                connection.close()


def wait_amqp_application(uri, running=lambda: True):
    while True:
        if not running():
            raise Failure("test_service_stopped")
        try:
            amqp_application_ready(uri)
            return
        except OSError:
            time.sleep(1)  # Startup progress has no arbitrary attempt/time cap.


@contextlib.contextmanager
def test_services():
    if os.environ.get("REGISTRY_TEST_DATABASE_URL") and os.environ.get("FLEET_TEST_AMQP_URL"):
        for setting in ("REGISTRY_TEST_DATABASE_URL", "FLEET_TEST_AMQP_URL"):
            uri = urllib.parse.urlsplit(os.environ[setting])
            port = uri.port or (5432 if setting.startswith("REGISTRY") else 5672)
            while True:
                try:
                    with socket.create_connection((uri.hostname, port), timeout=10):
                        break
                except socket.gaierror:
                    raise Failure("test_service_dns_unavailable") from None
                except OSError:
                    time.sleep(1)
        wait_amqp_application(os.environ["FLEET_TEST_AMQP_URL"])
        yield os.environ.copy()
        return
    prefix = "fleet-ci-" + secrets.token_hex(6)
    password = secrets.token_hex(24)
    names = [prefix + "-pg", prefix + "-mq"]
    try:
        run(["docker", "run", "-d", "--name", names[0], "-e", "POSTGRES_PASSWORD=" + password,
             "-p", "127.0.0.1::5432", os.environ.get("FLEET_TEST_PG_IMAGE", "postgres:17")])
        run(["docker", "run", "-d", "--name", names[1], "-e", "RABBITMQ_DEFAULT_USER=fleetci",
             "-e", "RABBITMQ_DEFAULT_PASS=" + password, "-p", "127.0.0.1::5672",
             os.environ.get("FLEET_TEST_MQ_IMAGE", "rabbitmq:4.2.9")])
        for name, command in ((names[0], ["pg_isready", "-U", "postgres"]),
                              (names[1], ["rabbitmq-diagnostics", "-q", "ping"])):
            while True:
                status = run(["docker", "inspect", "-f", "{{.State.Running}}", name]).strip()
                if status != b"true":
                    raise Failure("test_service_stopped")
                p = subprocess.run(["docker", "exec", name] + command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if p.returncode == 0:
                    break
                time.sleep(1)
        pg = run(["docker", "port", names[0], "5432/tcp"]).decode().strip().rsplit(":", 1)[1]
        mq = run(["docker", "port", names[1], "5672/tcp"]).decode().strip().rsplit(":", 1)[1]
        env = os.environ.copy()
        env["REGISTRY_TEST_DATABASE_URL"] = "postgres://postgres:" + password + "@127.0.0.1:" + pg + "/postgres?sslmode=disable"
        env["FLEET_TEST_AMQP_URL"] = "amqp://fleetci:" + password + "@127.0.0.1:" + mq + "/"
        wait_amqp_application(env["FLEET_TEST_AMQP_URL"], running=lambda: run(["docker", "inspect", "-f", "{{.State.Running}}", names[1]]).strip() == b"true")
        yield env
    finally:
        for name in names:
            subprocess.run(["docker", "rm", "-f", "-v", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def tracked(root):
    names = run(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    result = []
    for name in names:
        if not name or name.startswith((".git/", ".local/", "dist/", "ci/", ".github/", ".gitea/")) or name == "gitflic-ci.yaml":
            continue
        p = root / name
        if p.is_symlink():
            raise Failure("package_symlink_requires_explicit_policy")
        if p.is_file():
            result.append((name, p.read_bytes(), 0o755 if p.stat().st_mode & 0o111 else 0o644))
    return result


def android_unsigned(root, tag, commit):
    directory = android_root(root)
    version = json.loads((root / "ci/version.json").read_bytes())
    if tag != "v" + version["name"] or not isinstance(version["code"], int) or version["code"] <= 0:
        raise Failure("android_tag_version_mismatch")
    build_env = os.environ.copy()
    for private_name in ("FLEET_RELEASE_SOURCES_JSON", "GITFLIC_TOKEN", "GITEA_TOKEN", "GITHUB_TOKEN", "FLEET_GITEA_TOKEN", "CI_JOB_TOKEN", "FLEET_GPG_PRIVATE_KEY_FILE", "FLEET_SIGNING_DIRECTORY", "GNUPGHOME"):
        build_env.pop(private_name, None)
    run([directory / "gradlew", "--no-daemon", ":app:assembleRelease", ":app:testReleaseUnitTest", ":app:lintRelease", ":app:verifyPublicApk",
         "-PworkerVersionCode=" + str(version["code"]), "-PworkerVersionName=" + version["name"]], cwd=directory, env=build_env)
    target = root / "dist/unsigned"
    apk = (directory / "app/build/outputs/apk/release/app-release-unsigned.apk").read_bytes()
    save(target / "worker-unsigned.apk", apk)
    save(target / "build.json", json_bytes({"tag": tag, "commit": commit, "version": version,
                                           "sha256": sha(apk), "size": len(apk)}))


def channel_documents(config, record, manifest, apk_url):
    """Versioned public Git files keep Android JSON outside release MIME limits."""
    base = "https://gitflic.ru/project/" + encoded(config["gitflic_owner"]) + "/" + encoded(config["gitflic_project"]) + "/blob/raw?"
    names = {"manifest_url": ANDROID_MANIFEST, "canonical_record_url": "release-record.json"}
    document = {"tag": record["tag"], "version_code": manifest["version_code"], "source_commit": record["commit"],
                "sha256": manifest["sha256"], "apk_url": apk_url}
    paths = {}
    for field, name in names.items():
        relative = "releases/" + record["tag"] + "/" + name
        paths[field] = relative
        document[field] = base + urllib.parse.urlencode({"file": relative, "commit": "release-channel"})
    return document, paths


def verify_channel_metadata(channel, paths, files):
    public = HTTP("https://gitflic.ru")
    for field, relative in paths.items():
        data = public.request("GET", channel[field], accept="application/octet-stream", missing=True)
        if data is None:
            raise Temporary("channel_metadata_not_indexed_yet")
        if data != files[relative]:
            raise Failure("anonymous_channel_metadata_checksum")


def publish_channel(root, remote, record, config):
    """Atomic signed channel promotion, after anonymous bundle/APK verification."""
    public = GitFlicWeb(config["gitflic_owner"] + "/" + config["gitflic_project"])
    out = root / "dist" / record["tag"]
    fetch_canonical(public, root, out, config, record["tag"], record["commit"])
    release = public.release(record["tag"])
    if release is None:
        raise Pending("public_channel_release_pending")
    apk_name = next(x["name"] for x in record["files"] if x["name"].endswith(".apk"))
    assets = [asset for asset in public.assets(release) if asset["name"] == apk_name]
    if len(assets) != 1 or not assets[0].get("link", "").startswith("https://gitflic.ru/"):
        raise Failure("public_gitflic_apk_link_missing")
    manifest = json.loads((out / ANDROID_MANIFEST).read_bytes())
    channel, paths = channel_documents(config, record, manifest, assets[0]["link"])
    files = {relative: (out / relative.rsplit("/", 1)[1]).read_bytes() for relative in paths.values()}
    private = Path(required("FLEET_GPG_PRIVATE_KEY_FILE"))
    with tempfile.TemporaryDirectory(prefix="fleet-channel-") as d:
        d = Path(d)
        home = d / "gnupg"
        home.mkdir(mode=0o700)
        env = dict(os.environ, GNUPGHOME=str(home))
        run(["gpg", "--batch", "--import", private], env=env)
        keys = run(["gpg", "--batch", "--with-colons", "--list-secret-keys"], env=env).decode()
        fingerprints = [x.split(":")[9] for x in keys.splitlines() if x.startswith("fpr:")]
        if not fingerprints:
            raise Failure("ci_git_signing_key_missing")
        ask = d / "askpass.py"
        save(ask, b'#!/usr/bin/env python3\nimport os,sys\nprint(os.environ.get("FLEET_GITFLIC_USERNAME","token") if "username" in sys.argv[1].lower() else os.environ["GITFLIC_TOKEN"])\n')
        ask.chmod(0o700)
        env.update(GIT_ASKPASS=str(ask), GIT_TERMINAL_PROMPT="0")
        clone = d / "repo"
        clone.mkdir()
        address = os.environ.get("FLEET_CHANNEL_REMOTE", "https://gitflic.ru/project/" + config["gitflic_owner"] + "/" + config["gitflic_project"] + ".git")
        if urllib.parse.urlsplit(address).username:
            raise Failure("channel_remote_must_not_embed_credentials")
        run(["git", "init", "-q"], cwd=clone, env=env)
        run(["git", "config", "user.name", "DistributedWorkersReleaseBot"], cwd=clone, env=env)
        run(["git", "config", "user.email", os.environ.get("FLEET_RELEASE_BOT_EMAIL", "14372308+hawkab@users.noreply.github.com")], cwd=clone, env=env)
        run(["git", "config", "user.signingkey", fingerprints[0]], cwd=clone, env=env)
        refs = run(["git", "ls-remote", "--heads", address, "release-channel"], cwd=clone, env=env)
        if refs.strip():
            run(["git", "fetch", "--depth=1", address, "refs/heads/release-channel"], cwd=clone, env=env)
            run(["git", "checkout", "-q", "-b", "release-channel", "FETCH_HEAD"], cwd=clone, env=env)
            stable = clone / "stable.json"
            if stable.is_file():
                previous = json.loads(stable.read_bytes())
                if previous.get("version_code", 0) > channel["version_code"]:
                    return
                if previous.get("version_code") == channel["version_code"]:
                    if previous.get("sha256") != channel["sha256"]:
                        raise Failure("channel_same_version_hash_conflict")
                    if previous == channel:
                        verify_channel_metadata(channel, paths, files)
                        return
        else:
            run(["git", "checkout", "--orphan", "release-channel"], cwd=clone, env=env)
        for relative, data in files.items():
            target = clone / relative
            if target.exists() and target.read_bytes() != data:
                raise Failure("immutable_channel_metadata_conflict")
            save(target, data)
        save(clone / "stable.json", json_bytes(channel))
        run(["git", "add", "--", "stable.json"] + sorted(files), cwd=clone, env=env)
        run(["git", "commit", "-S", "-m", "Release " + record["tag"]], cwd=clone, env=env)
        pushed = subprocess.run(["git", "push", address, "HEAD:refs/heads/release-channel"],
                                cwd=clone, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if pushed.returncode:
            error = pushed.stderr.decode(errors="replace").lower()
            if any(word in error for word in ("authentication failed", "permission denied", "access denied", "403", "not allowed")):
                raise Failure("channel_push_permission_denied")
            if any(word in error for word in ("non-fast-forward", "fetch first", "could not resolve", "connection", "timeout", "remote end hung up")):
                raise Temporary("channel_push_reconcile")
            raise Failure("channel_push_failed")
        verify_channel_metadata(channel, paths, files)


def build(root, out, config, tag, commit):
    component = config["component"]
    epoch = int(run(["git", "show", "-s", "--format=%ct", commit], cwd=root))
    if component == "gateway":
        directory = root if (root / "go.mod").is_file() else root / "gateway"
        with test_services() as env:
            process = subprocess.run(["go", "test", "-race", "-json", "./..."], cwd=directory, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output = sanitize_output(process.stdout, env)
            stderr = sanitize_output(process.stderr, env)
            save(root / "dist" / ("test-results-" + tag + ".jsonl"), output.encode())
            save(root / "dist" / ("test-stderr-" + tag + ".log"), stderr.encode())
            try:
                events = check_go_results(output, process.returncode)
            except Failure:
                # Keep the complete sanitized evidence in artifacts even on failure.
                print(output, flush=True)
                print(stderr, file=sys.stderr, flush=True)
                raise
            passed = sum(e.get("Action") == "pass" and bool(e.get("Test")) for e in events)
            empty_packages = sum(e.get("Action") == "skip" and not e.get("Test") for e in events)
            print("Go tests: " + str(passed) + " passed; packages without tests: " + str(empty_packages), flush=True)
        for goos, goarch in (("linux", "amd64"), ("linux", "arm64"), ("windows", "amd64")):
            with tempfile.TemporaryDirectory() as d:
                entries = []
                for command in ("worker-gateway", "fleet-relay", "fleetctl"):
                    name = command + (".exe" if goos == "windows" else "")
                    env = dict(os.environ, CGO_ENABLED="0", GOOS=goos, GOARCH=goarch)
                    run(["go", "build", "-trimpath", "-buildvcs=false", "-ldflags=-s -w", "-o", Path(d) / name, "./cmd/" + command], cwd=directory, env=env)
                    entries.append((name, (Path(d) / name).read_bytes(), 0o755))
                zip_format = goos == "windows"
                archive(out / (component + "-" + tag + "-" + goos + "-" + goarch + (".zip" if zip_format else ".tar.gz")), entries, epoch, zip_format=zip_format)
    elif component == "worker-android":
        directory = android_root(root)
        version = json.loads((root / "ci/version.json").read_bytes())
        if tag != "v" + version["name"] or not isinstance(version["code"], int) or version["code"] <= 0:
            raise Failure("android_tag_version_mismatch")
        unsigned = root / "dist/unsigned/worker-unsigned.apk"
        metadata = json.loads((root / "dist/unsigned/build.json").read_bytes())
        if (metadata["tag"], metadata["commit"], metadata["version"], metadata["sha256"]) != (tag, commit, version, sha(unsigned.read_bytes())):
            raise Failure("unsigned_build_identity_mismatch")
        private = Path(os.environ.get("FLEET_SIGNING_DIRECTORY", str(Path.home() / ".local/share/fleet-worker-signing")))
        signer, aapt = apk_tools()
        badging = run([aapt, "dump", "badging", unsigned]).decode()
        match = re.search(r"package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'", badging)
        if not match or match.groups() != ("local.hwak.fleetworker", str(version["code"]), version["name"]):
            raise Failure("unsigned_apk_identity_mismatch")
        verify_public_apk(root, unsigned)
        apk = out / ANDROID_APK
        run([signer, "sign", "--v4-signing-enabled", "false", "--ks", private / "apk-release.jks", "--ks-key-alias", "fleet-worker", "--ks-pass",
             "file:" + str(private / "apk-password"), "--out", apk,
             unsigned])
        canonical_base = "https://github.com/hawkab/distributed-workers-android/releases/download/{tag}"
        configured_base = os.environ.get("FLEET_PUBLIC_RELEASE_BASE", canonical_base)
        if configured_base != canonical_base:
            raise Failure("canonical_public_release_base_must_match_public_forge")
        manifest = {"package_name": "local.hwak.fleetworker", "version_code": version["code"], "version_name": version["name"],
                    "url": public_apk_url(canonical_base, tag), "sha256": sha(apk.read_bytes()),
                    "apk_certificate_sha256": config["android_certificate_sha256"], "signature_alg": "SHA256withRSA"}
        fields = ("package_name", "version_code", "version_name", "url", "sha256", "apk_certificate_sha256")
        signature = run(["openssl", "dgst", "-sha256", "-sign", private / "release-signing.pem"],
                        data="".join(str(manifest[k]) + "\n" for k in fields).encode())
        manifest["signature"] = base64.b64encode(signature).decode()
        save(out / ANDROID_MANIFEST, json_bytes(manifest))
        verify_android(root, out, config)
    else:
        entries = tracked(root)
        if component == "protocol":
            schemas = [(name, data) for name, data, _ in entries if name.endswith(".schema.json")]
            if not schemas:
                raise Failure("protocol_schemas_missing")
            for _, data in schemas:
                schema = json.loads(data)
                if not isinstance(schema, dict) or not schema.get("$schema"):
                    raise Failure("invalid_protocol_schema")
        for p in root.rglob("*.py"):
            if not any(part in (".git", ".local", "ci", "dist", "__pycache__") for part in p.relative_to(root).parts):
                compile(p.read_bytes(), str(p.relative_to(root)), "exec")
        if not entries:
            raise Failure("empty_component")
        archive(out / (component + "-" + tag + ".tar.gz"), entries, epoch)
    return finish_record(out, component, tag, commit)


def retry(operation, state_path, *, once=False):
    delay = 2
    while True:
        try:
            result = operation()
            save(state_path, json_bytes({"state": "complete", "result": result}))
            return result
        except Temporary as e:
            save(state_path, json_bytes({"state": "pending", "reason": str(e)}))
            print("release pending: " + str(e), flush=True)
            if once:
                raise
            # No attempt exhaustion or invented job deadline. Native CI cancellation
            # remains resumable: next dispatch reconciles the forge state.
            time.sleep(delay)
            delay = min(60, delay * 2)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=("release", "build", "android-unsigned", "publish", "verify"))
    p.add_argument("--provider", choices=("gitflic", "gitea", "github"), default="gitflic")
    p.add_argument("--tag", default=os.environ.get("FLEET_RELEASE_TAG", os.environ.get("CI_COMMIT_TAG", os.environ.get("GITHUB_REF_NAME", ""))))
    p.add_argument("--once", action="store_true", help="Return pending status for a manual resumable check")
    args = p.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    config = json.loads((root / "ci/component.json").read_bytes())
    if not TAG.fullmatch(args.tag):
        raise Failure("version_tag_required")
    commit = run(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
    tag_commit = run(["git", "rev-parse", args.tag + "^{commit}"], cwd=root).decode().strip()
    if commit != tag_commit:
        raise Failure("checkout_does_not_match_release_tag")
    out = root / "dist" / args.tag
    out.mkdir(parents=True, exist_ok=True)
    if args.command == "android-unsigned":
        if args.provider != "gitflic" or config["component"] != "worker-android":
            raise Failure("canonical_android_build_only")
        android_unsigned(root, args.tag, commit)
        return
    if args.command == "verify":
        result = verify_record(out, config["component"], args.tag, commit)
        if config["component"] == "worker-android":
            verify_android(root, out, config)
        print(json.dumps({"verified": True, "component": result["component"], "tag": args.tag}))
        return
    if args.command == "build":
        if args.provider != "gitflic":
            raise Failure("canonical_build_only")
        build(root, out, config, args.tag, commit)
        return
    remote = forge(args.provider, config)
    canonical = forge("gitflic", config, canonical=True)

    def operation():
        if args.command == "publish":
            record = verify_record(out, config["component"], args.tag, commit)
            if config["component"] == "worker-android":
                verify_android(root, out, config)
        elif args.provider != "gitflic":
            record = fetch_canonical(canonical, root, out, config, args.tag, commit)
        else:
            existing = remote.release(args.tag)
            ready = existing and any(x["name"] in ("release-record.json", METADATA_ZIP) for x in remote.assets(existing))
            if ready:
                record = fetch_canonical(remote, root, out, config, args.tag, commit)
            elif (out / "release-record.json").is_file():
                record = verify_record(out, config["component"], args.tag, commit)
                if config["component"] == "worker-android":
                    verify_android(root, out, config)
            else:
                record = build(root, out, config, args.tag, commit)
        result = publish(remote, out, record)
        if args.provider == "gitflic" and config["component"] == "worker-android":
            publish_channel(root, remote, record, config)
            result["channel_published"] = True
        return result

    print(json.dumps(retry(operation, out / "release-state.json", once=args.once), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Temporary as e:
        print("release pending: " + str(e), file=sys.stderr)
        sys.exit(75)
    except Failure as e:
        print("release failed: " + str(e), file=sys.stderr)
        sys.exit(1)
