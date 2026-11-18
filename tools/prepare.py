#!/usr/bin/env python3
"""Verify a signed portable runtime and prepare an explicit, secret-free context."""
import argparse, base64, hashlib, json, pathlib, shutil, stat, subprocess, tempfile, zipfile
ROOT = pathlib.Path(__file__).resolve().parents[1]
FIELDS = ('component', 'version_code', 'version_name', 'url', 'sha256')
def verify(archive, manifest, key=ROOT / 'assets/release-public.der'):
    m = json.loads(pathlib.Path(manifest).read_text())
    lock = json.loads((ROOT / 'runtime.lock.json').read_text())
    if (m['component'], m['version_name'], m['version_code'], m['sha256']) != (lock['component'], lock['version'], lock['version_code'], lock['sha256']):
        raise ValueError('runtime_lock_mismatch')
    if hashlib.sha256(pathlib.Path(archive).read_bytes()).hexdigest() != m['sha256']:
        raise ValueError('runtime_hash_mismatch')
    with tempfile.TemporaryDirectory() as folder:
        d = pathlib.Path(folder)
        (d / 'payload').write_bytes(''.join(str(m[k]) + '\n' for k in FIELDS).encode())
        (d / 'signature').write_bytes(base64.b64decode(m['signature'], validate=True))
        pem = subprocess.run(['openssl', 'pkey', '-pubin', '-inform', 'DER', '-in', str(key)], capture_output=True, check=True).stdout
        (d / 'public.pem').write_bytes(pem)
        subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(d / 'public.pem'), '-signature', str(d / 'signature'), str(d / 'payload')], check=True, capture_output=True)
    return m

def prepare(archive, manifest, target):
    m = verify(archive, manifest)
    target = pathlib.Path(target)
    if target.exists(): raise ValueError('context_already_exists')
    target.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive) as z:
            names = set()
            for e in z.infolist():
                p = pathlib.PurePosixPath(e.filename)
                mode = e.external_attr >> 16
                if p.is_absolute() or '..' in p.parts or '\\' in e.filename or e.filename in names or stat.S_ISLNK(mode):
                    raise ValueError('runtime_archive_path_invalid')
                names.add(e.filename)
                if e.is_dir(): continue
                # Keep both adapters so the signed updater's runtime identity stays intact.
                dest = target / e.filename
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(e)); dest.chmod(0o755 if mode & 0o111 else 0o644)
        v = json.loads((target / 'VERSION.json').read_text())
        if v != {'code': m['version_code'], 'name': m['version_name']}: raise ValueError('runtime_version_mismatch')
        if (target / 'assets/release-public.der').read_bytes() != (ROOT / 'assets/release-public.der').read_bytes(): raise ValueError('runtime_signing_key_mismatch')
    except BaseException:
        shutil.rmtree(target); raise
    return {'runtime_sha256': m['sha256'], 'runtime_version': v, 'unpacked_bytes': sum(p.stat().st_size for p in target.rglob('*') if p.is_file())}
if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--archive',type=pathlib.Path,required=True);p.add_argument('--manifest',type=pathlib.Path,required=True);p.add_argument('--target',type=pathlib.Path,default=ROOT/'.build/sdk');a=p.parse_args()
    print(json.dumps(prepare(a.archive,a.manifest,a.target)))
