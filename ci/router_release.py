#!/usr/bin/env python3
"""Portable-worker release adapter over the unchanged common forge publisher.

The unprivileged build tests and packages exact Git source. The protected signer
only validates/signs those bytes. Mirrors verify the pinned RSA signature before
publishing identical canonical assets. No deployment occurs in this program.
"""
import argparse
import base64
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import zipfile
import tarfile
import hashlib

import fleetci as common

ARCHIVE = 'fleet-worker-routeros-arm64.tar.gz'
MANIFEST = 'container-manifest.json'
FIELDS = ('component', 'version_code', 'version_name', 'url', 'sha256')
COMPONENT = 'worker-routeros'
CANONICAL_BASE = 'https://github.com/hawkab/distributed-workers-routeros/releases/download/'
ROOT_FILES = {'VERSION.json', 'update-sources.example.json', 'package.json', 'README.md', 'PROXY.md', 'LICENSE', 'LICENSE.txt'}

def version(root, tag):
    try:
        value = json.loads((root / 'ci/version.json').read_bytes())
        if not common.TAG.fullmatch(tag):
            raise common.Failure('version_tag_required')
        if (set(value) != {'name', 'code'} or type(value['code']) is not int or
                value['code'] <= 0 or tag != 'v' + value['name']):
            raise common.Failure('container_tag_version_mismatch')
        return value
    except (KeyError, TypeError, ValueError, OSError):
        raise common.Failure('container_version_invalid') from None


def signature_bytes(manifest):
    return ''.join(str(manifest[key]) + '\n' for key in FIELDS).encode('utf-8')


def canonical_url(tag):
    if not common.TAG.fullmatch(tag):
        raise common.Failure('version_tag_required')
    return CANONICAL_BASE + urllib.parse.quote(tag, safe='') + '/' + ARCHIVE


def inspect_archive(root, archive_path, expected_version, commit=None, environment=None):
    """Inspect image metadata without extracting an archive or running its code."""
    try:
        with tarfile.open(archive_path, 'r:gz') as package:
            names=set()
            for entry in package.getmembers():
                p=PurePosixPath(entry.name)
                if p.is_absolute() or '..' in p.parts or entry.name in names or not (entry.isfile() or entry.isdir()):
                    raise common.Failure('container_archive_paths_invalid')
                names.add(entry.name)
            manifest=json.load(package.extractfile('manifest.json'))
            if len(manifest)!=1:raise common.Failure('single_container_required')
            image=manifest[0];config_bytes=package.extractfile(image['Config']).read();config=json.loads(config_bytes)
            labels=config['config'].get('Labels', {})
            if config['architecture']!='arm64' or config['os']!='linux':raise common.Failure('container_platform_mismatch')
            if config['config']['Entrypoint']!=['php','/opt/worker/supervise.php'] or config['config']['Cmd']!=['/config/worker.local.json']:
                raise common.Failure('container_entrypoint_mismatch')
            lock=json.loads((root/'runtime.lock.json').read_bytes())
            if labels.get('local.worker.runtime.sha256')!=lock['sha256'] or labels.get('org.opencontainers.image.version')!='v'+expected_version['name']:
                raise common.Failure('container_runtime_identity_mismatch')
            if commit and labels.get('org.opencontainers.image.revision')!=commit:raise common.Failure('container_source_identity_mismatch')
            if len(image['Layers'])!=len(config['rootfs']['diff_ids']):raise common.Failure('container_layer_count_mismatch')
            for layer, expected in zip(image['Layers'],config['rootfs']['diff_ids']):
                with package.extractfile(layer) as data:
                    digest=hashlib.file_digest(data,'sha256').hexdigest()
                if 'sha256:'+digest!=expected:raise common.Failure('container_layer_checksum_mismatch')
            return {'length':Path(archive_path).stat().st_size,'sha256':common.sha(Path(archive_path).read_bytes()),'image_id':'sha256:'+common.sha(config_bytes),'architecture':'arm64'}
    except (OSError,ValueError,KeyError,TypeError,tarfile.TarError):
        raise common.Failure('container_archive_invalid') from None


def runtime_tests(root, tag):
    common.run(['python3','-B','-m','unittest','discover','-s','tests','-v'],cwd=root)


def unsigned(root, tag, commit):
    current=version(root,tag)
    common.run(['git','diff','--exit-code',commit,'--'],cwd=root)
    runtime_tests(root,tag)
    out=root/'dist/unsigned';out.mkdir(parents=True,exist_ok=True)
    output=common.run(['python3','-B','tools/build.py','--output',str(out),'--tag',tag,'--commit',commit],cwd=root)
    print(output.decode(),end='')
    inspected=inspect_archive(root,out/ARCHIVE,current,commit=commit)
    common.save(out/'build.json',common.json_bytes({'tag':tag,'commit':commit,'version':current,**inspected}))
    return inspected


def verify_portable(root, out, config, tag, commit):
    if config['component'] != COMPONENT:
        raise common.Failure('container_component_required')
    current = version(root, tag)
    try:
        manifest = json.loads((out / MANIFEST).read_bytes())
        if set(manifest) != set(FIELDS) | {'signature_alg', 'signature'}:
            raise common.Failure('container_manifest_fields_invalid')
        if ((manifest['component'], manifest['version_code'], manifest['version_name']) !=
                (COMPONENT, current['code'], current['name']) or type(manifest['version_code']) is not int):
            raise common.Failure('container_manifest_identity')
        if manifest['signature_alg'] != 'SHA256withRSA' or manifest['url'] != canonical_url(tag):
            raise common.Failure('container_manifest_signature_or_url')
        if not common.HEX.fullmatch(manifest['sha256']) or common.sha((out / ARCHIVE).read_bytes()) != manifest['sha256']:
            raise common.Failure('container_archive_hash')
        inspect_archive(root, out / ARCHIVE, current, commit=commit)
        signature = base64.b64decode(manifest['signature'], validate=True)
        with tempfile.TemporaryDirectory(prefix='portable-signature-') as tmp:
            signature_file = Path(tmp) / 'signature.bin'
            signature_file.write_bytes(signature)
            common.run(['openssl', 'dgst', '-sha256', '-verify', root / 'assets/release-public.der',
                        '-keyform', 'DER', '-signature', signature_file], data=signature_bytes(manifest))
        return manifest
    except (OSError, ValueError, KeyError, TypeError):
        raise common.Failure('container_manifest_invalid') from None


def build(root, out, config, tag, commit):
    current = version(root, tag)
    incoming = root / 'dist/unsigned'
    try:
        metadata = json.loads((incoming / 'build.json').read_bytes())
        archive = incoming / ARCHIVE
        if ((metadata['tag'], metadata['commit'], metadata['version'], metadata['sha256'], metadata['length']) !=
                (tag, commit, current, common.sha(archive.read_bytes()), archive.stat().st_size)):
            raise common.Failure('container_unsigned_identity_mismatch')
    except (OSError, ValueError, KeyError, TypeError):
        raise common.Failure('container_unsigned_build_required') from None
    inspect_archive(root, archive, current, commit=commit)
    common.save(out / ARCHIVE, archive.read_bytes())
    manifest = {'component': COMPONENT, 'version_code': current['code'], 'version_name': current['name'],
                'url': canonical_url(tag), 'sha256': common.sha(archive.read_bytes()), 'signature_alg': 'SHA256withRSA'}
    private = Path(common.required('FLEET_SIGNING_DIRECTORY')) / 'release-signing.pem'
    signature = common.run(['openssl', 'dgst', '-sha256', '-sign', private], data=signature_bytes(manifest))
    manifest['signature'] = base64.b64encode(signature).decode()
    common.save(out / MANIFEST, common.json_bytes(manifest))
    verify_portable(root, out, config, tag, commit)
    common.finish_record(out, COMPONENT, tag, commit)
    return verify_release(root, out, config, tag, commit)


def verify_release(root, out, config, tag, commit):
    record = common.verify_record(out, COMPONENT, tag, commit)
    if {item['name'] for item in record['files']} != {ARCHIVE, MANIFEST}:
        raise common.Failure('container_canonical_asset_set')
    verify_portable(root, out, config, tag, commit)
    return record


def fetch_canonical(remote, root, out, config, tag, commit):
    record = common.fetch_canonical(remote, root, out, config, tag, commit)
    if {item['name'] for item in record['files']} != {ARCHIVE, MANIFEST}:
        raise common.Failure('container_canonical_asset_set')
    verify_portable(root, out, config, tag, commit)
    return record


def channel_documents(config, record, manifest, archive_url):
    parsed = urllib.parse.urlsplit(archive_url)
    if parsed.scheme != 'https' or parsed.hostname != 'gitflic.ru' or parsed.username:
        raise common.Failure('container_public_gitflic_archive_link_missing')
    base = 'https://gitflic.ru/project/' + common.encoded(config['gitflic_owner']) + '/' + common.encoded(config['gitflic_project']) + '/blob/raw?'
    channel = {'tag': record['tag'], 'version_code': manifest['version_code'], 'source_commit': record['commit'],
               'sha256': manifest['sha256'], 'archive_url': archive_url}
    paths = {}
    for field, name in (('manifest_url', MANIFEST), ('canonical_record_url', 'release-record.json')):
        relative = 'releases/' + record['tag'] + '/' + name
        paths[field] = relative
        channel[field] = base + urllib.parse.urlencode({'file': relative, 'commit': 'release-channel'})
    return channel, paths


def publish_channel(root, remote, record, config):
    public = common.GitFlicWeb(config['gitflic_owner'] + '/' + config['gitflic_project'])
    out = root / 'dist' / record['tag']
    fetch_canonical(public, root, out, config, record['tag'], record['commit'])
    release = public.release(record['tag'])
    if release is None:
        raise common.Pending('public_channel_release_pending')
    assets = [asset for asset in public.assets(release) if asset['name'] == ARCHIVE]
    if len(assets) != 1:
        raise common.Failure('container_public_gitflic_archive_link_missing')
    manifest = json.loads((out / MANIFEST).read_bytes())
    channel, paths = channel_documents(config, record, manifest, assets[0].get('link', ''))
    files = {relative: (out / relative.rsplit('/', 1)[1]).read_bytes() for relative in paths.values()}
    with tempfile.TemporaryDirectory(prefix='portable-channel-') as tmp:
        tmp = Path(tmp)
        home = tmp / 'gnupg'
        home.mkdir(mode=0o700)
        env = dict(os.environ, GNUPGHOME=str(home), GIT_TERMINAL_PROMPT='0')
        common.run(['gpg', '--batch', '--import', Path(common.required('FLEET_GPG_PRIVATE_KEY_FILE'))], env=env)
        keys = common.run(['gpg', '--batch', '--with-colons', '--list-secret-keys'], env=env).decode()
        fingerprints = [line.split(':')[9] for line in keys.splitlines() if line.startswith('fpr:')]
        if not fingerprints:
            raise common.Failure('ci_git_signing_key_missing')
        ask = tmp / 'askpass.py'
        common.save(ask, b'#!/usr/bin/env python3\nimport os,sys\nprint(os.environ.get("FLEET_GITFLIC_USERNAME","token") if "username" in sys.argv[1].lower() else os.environ["GITFLIC_TOKEN"])\n')
        ask.chmod(0o700)
        env['GIT_ASKPASS'] = str(ask)
        clone = tmp / 'repo'
        clone.mkdir()
        address = os.environ.get('FLEET_CHANNEL_REMOTE', 'https://gitflic.ru/project/' + config['gitflic_owner'] + '/' + config['gitflic_project'] + '.git')
        parsed = urllib.parse.urlsplit(address)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise common.Failure('channel_remote_https_without_credentials_required')
        def git(*args):
            return common.run(['git', *args], cwd=clone, env=env)
        git('init', '-q')
        git('config', 'user.name', 'DistributedWorkersReleaseBot')
        git('config', 'user.email', os.environ.get('FLEET_RELEASE_BOT_EMAIL', '14372308+hawkab@users.noreply.github.com'))
        git('config', 'user.signingkey', fingerprints[0])
        refs = git('ls-remote', '--heads', address, 'release-channel')
        if refs.strip():
            git('fetch', '--depth=1', address, 'refs/heads/release-channel')
            git('checkout', '-q', '-b', 'release-channel', 'FETCH_HEAD')
            stable = clone / 'stable.json'
            if stable.is_file():
                previous = json.loads(stable.read_bytes())
                if previous.get('version_code', 0) > channel['version_code']:
                    return
                if previous.get('version_code') == channel['version_code']:
                    if previous.get('sha256') != channel['sha256']:
                        raise common.Failure('channel_same_version_hash_conflict')
                    if previous == channel:
                        common.verify_channel_metadata(channel, paths, files)
                        return
        else:
            git('checkout', '--orphan', 'release-channel')
        for relative, data in files.items():
            target = clone / relative
            if target.exists() and target.read_bytes() != data:
                raise common.Failure('immutable_channel_metadata_conflict')
            common.save(target, data)
        common.save(clone / 'stable.json', common.json_bytes(channel))
        git('add', '--', 'stable.json', *sorted(files))
        git('commit', '-S', '-m', 'Release ' + record['tag'])
        pushed = subprocess.run(['git', 'push', address, 'HEAD:refs/heads/release-channel'], cwd=clone, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if pushed.returncode:
            error = pushed.stderr.decode(errors='replace').lower()
            if any(word in error for word in ('authentication failed', 'permission denied', 'access denied', '403', 'not allowed')):
                raise common.Failure('channel_push_permission_denied')
            if any(word in error for word in ('non-fast-forward', 'fetch first', 'could not resolve', 'connection', 'timeout', 'remote end hung up')):
                raise common.Temporary('channel_push_reconcile')
            raise common.Failure('channel_push_failed')
        common.verify_channel_metadata(channel, paths, files)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('unsigned', 'release', 'build', 'verify', 'publish'))
    parser.add_argument('--provider', choices=('gitflic', 'gitea', 'github'), default='gitflic')
    parser.add_argument('--tag', default=os.environ.get('FLEET_RELEASE_TAG', os.environ.get('CI_COMMIT_TAG', os.environ.get('GITHUB_REF_NAME', ''))))
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    config = json.loads((root / 'ci/component.json').read_bytes())
    if config['component'] != COMPONENT:
        raise common.Failure('container_component_required')
    version(root, args.tag)
    commit = common.run(['git', 'rev-parse', 'HEAD'], cwd=root).decode().strip()
    pinned = common.run(['git', 'rev-parse', args.tag + '^{commit}'], cwd=root).decode().strip()
    if commit != pinned:
        raise common.Failure('checkout_does_not_match_release_tag')
    out = root / 'dist' / args.tag
    out.mkdir(parents=True, exist_ok=True)
    if args.command == 'unsigned':
        if args.provider != 'gitflic':
            raise common.Failure('canonical_container_build_only')
        unsigned(root, args.tag, commit)
        return
    if args.command == 'verify':
        verify_release(root, out, config, args.tag, commit)
        print(json.dumps({'verified': True, 'component': COMPONENT, 'tag': args.tag}))
        return
    if args.command == 'build':
        if args.provider != 'gitflic':
            raise common.Failure('canonical_build_only')
        build(root, out, config, args.tag, commit)
        return
    remote = common.forge(args.provider, config)
    canonical = common.forge('gitflic', config, canonical=True)
    def operation():
        if args.command == 'publish':
            record = verify_release(root, out, config, args.tag, commit)
        elif args.provider != 'gitflic':
            record = fetch_canonical(canonical, root, out, config, args.tag, commit)
        else:
            existing = remote.release(args.tag)
            ready = existing and any(asset['name'] in ('release-record.json', common.METADATA_ZIP) for asset in remote.assets(existing))
            if ready:
                record = fetch_canonical(remote, root, out, config, args.tag, commit)
            elif (out / 'release-record.json').is_file():
                record = verify_release(root, out, config, args.tag, commit)
            else:
                record = build(root, out, config, args.tag, commit)
        result = common.publish(remote, out, record)
        if args.provider == 'gitflic':
            publish_channel(root, remote, record, config)
            result['channel_published'] = True
        return result
    print(json.dumps(common.retry(operation, out / 'release-state.json', once=args.once), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except common.Temporary as error:
        print('release pending: ' + str(error), file=sys.stderr)
        sys.exit(75)
    except common.Failure as error:
        print('release failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
