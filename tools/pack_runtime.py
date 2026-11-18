#!/usr/bin/env python3
"""Pack an operator-owned runtime without a duplicate operating system.

ELF dependencies are resolved without executing target-architecture code. A base
file is reused only when its content hash matches. Python source/data is imported
directly from one ZIP; native extension modules remain ordinary ELF files.
"""
import argparse
import hashlib
import json
import os
import posixpath
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import zipfile


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def base_files(archive):
    files = {}
    links = {}
    with tarfile.open(archive, 'r:gz') as outer:
        manifest = json.load(outer.extractfile('manifest.json'))
        if len(manifest) != 1:
            raise ValueError('one_base_image_required')
        for layer in manifest[0]['Layers']:
            with tarfile.open(fileobj=outer.extractfile(layer), mode='r|') as inner:
                for entry in inner:
                    path = PurePosixPath(entry.name)
                    if path.is_absolute() or '..' in path.parts:
                        raise ValueError('unsafe_base_path')
                    if entry.isfile():
                        files[path.as_posix()] = hashlib.file_digest(inner.extractfile(entry), 'sha256').hexdigest()
                        links.pop(path.as_posix(), None)
                    elif entry.issym():
                        links[path.as_posix()] = posixpath.normpath(posixpath.join(str(path.parent), entry.linkname)) if not entry.linkname.startswith('/') else entry.linkname.lstrip('/')
    for name in links:
        target, seen = name, set()
        while target in links and target not in seen:
            seen.add(target)
            target = links[target]
        if target in files:
            files[name] = files[target]
    return files


class Packer:
    def __init__(self, source, output, shared=None, readelf='readelf'):
        self.source = Path(source).resolve()
        self.output = Path(output).resolve()
        if self.output.exists():
            raise ValueError('output_already_exists')
        self.output.mkdir(parents=True)
        self.shared = shared or {}
        self.reused = {}
        self.done = set()
        self.readelf = readelf

    def owned(self, relative):
        relative = PurePosixPath(relative)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('unsafe_runtime_path')
        path = self.source / relative
        if not path.resolve().is_relative_to(self.source):
            raise ValueError('runtime_link_outside_source')
        return path

    def copy(self, relative):
        path = self.owned(relative)
        relative = Path(relative).as_posix()
        if relative in self.done:
            return
        self.done.add(relative)
        if path.is_dir():
            for child in sorted(path.iterdir()):
                self.copy(child.relative_to(self.source).as_posix())
            return
        if not path.exists():
            raise ValueError('missing_runtime_file:' + relative)
        checksum = digest(path)
        if self.shared.get(relative) == checksum:
            self.reused[relative] = checksum
            return
        target = self.output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            resolved = path.resolve().relative_to(self.source).as_posix()
            self.copy(resolved)
            # Relative links keep an operator bundle relocatable.
            target.symlink_to('/' + resolved if resolved in self.reused else os.path.relpath(self.output / resolved, target.parent))
            return
        shutil.copyfile(path, target)
        target.chmod(path.stat().st_mode & 0o777)
        with path.open('rb') as stream:
            elf = stream.read(4) == b'\x7fELF'
        if not elf:
            return
        info = subprocess.check_output([self.readelf, '-d', '-l', str(path)], text=True, stderr=subprocess.DEVNULL)
        for needed in re.findall(r'\(NEEDED\).*?\[([^\]]+)\]', info):
            found = next((p for folder in ['lib', 'usr/lib', str(Path(relative).parent)]
                          if (p := self.source / folder / needed).exists()), None)
            if found is None:
                raise ValueError('missing_runtime_library:' + needed)
            self.copy(found.relative_to(self.source).as_posix())
        interpreter = re.search(r'Requesting program interpreter:\s*([^\]]+)', info)
        if interpreter:
            self.copy(interpreter[1].lstrip('/'))

    def python(self, relative, packages=(), excludes=()):
        directory = self.owned(relative)
        match = re.fullmatch(r'python(\d+)\.(\d+)', directory.name)
        if not match:
            raise ValueError('python_stdlib_directory_required')
        destination = self.output / Path(relative).parent / ('python' + ''.join(match.groups()) + '.zip')
        destination.parent.mkdir(parents=True, exist_ok=True)
        roots = [(directory, '')] + [(Path(p).resolve(), Path(p).name + '/') for p in packages]
        names = set()
        with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for root, prefix in roots:
                for path in sorted(root.rglob('*')):
                    name = path.relative_to(root)
                    if any(p in ['__pycache__', 'lib-dynload', 'site-packages', *excludes] for p in name.parts) or path.suffix in ['.pyc', '.pyo']:
                        continue
                    if path.is_symlink():
                        raise ValueError('python_package_symlink')
                    if not path.is_file():
                        continue
                    name = prefix + name.as_posix()
                    if name in names:
                        raise ValueError('python_package_name_collision')
                    names.add(name)
                    entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    entry.external_attr = 0o100644 << 16
                    with path.open('rb') as source, archive.open(entry, 'w') as target:
                        shutil.copyfileobj(source, target, 65536)
        # Keep every native stdlib extension, including its transitive libraries.
        # Runtime coverage is not guessed from a single successful import trace.
        extensions = directory / 'lib-dynload'
        if extensions.is_dir():
            self.copy(extensions.relative_to(self.source).as_posix())

    def seal(self):
        files = {p.relative_to(self.output).as_posix(): digest(p)
                 for p in sorted(self.output.rglob('*')) if p.is_file() and not p.is_symlink()}
        size = sum(p.stat().st_size for p in self.output.rglob('*') if p.is_file() and not p.is_symlink())
        result = {'schema': 1, 'bytes': size, 'files': files, 'base_files': self.reused}
        (self.output / 'runtime-manifest.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
        return {'bytes': size, 'files': len(files), 'reused_base_files': len(self.reused)}

    def archive(self, destination):
        destination = Path(destination).resolve()
        if destination.exists() or destination.is_relative_to(self.output):
            raise ValueError('archive_must_be_new_and_outside_bundle')
        def metadata(info):
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mtime = 0
            return info
        with tarfile.open(destination, 'w') as archive:
            for path in sorted(self.output.rglob('*')):
                archive.add(path, arcname=path.relative_to(self.output).as_posix(), recursive=False, filter=metadata)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base-image', type=Path)
    parser.add_argument('--archive', type=Path, help='Sequential transport preserving links and neutral ownership')
    parser.add_argument('--executable', action='append', default=[])
    parser.add_argument('--data', action='append', default=[])
    parser.add_argument('--python-stdlib')
    parser.add_argument('--python-package', action='append', default=[])
    parser.add_argument('--python-exclude', action='append', default=[])
    args = parser.parse_args()
    packer = Packer(args.source, args.output, base_files(args.base_image) if args.base_image else {})
    try:
        for relative in args.executable + args.data:
            packer.copy(relative)
        if args.python_stdlib:
            packer.python(args.python_stdlib, args.python_package, args.python_exclude)
        result = packer.seal()
        if args.archive:
            packer.archive(args.archive)
        print(json.dumps(result, sort_keys=True))
    except BaseException:
        shutil.rmtree(packer.output)
        raise


if __name__ == '__main__':
    main()
