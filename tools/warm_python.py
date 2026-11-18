#!/usr/bin/env python3
"""Precompile an operator-selected Python import set on the target interpreter.

All original source/data members remain unchanged. Modules outside the warm set retain
their normal source fallback; this is a startup cache, not a dependency filter.
"""
import argparse
import copy
import hashlib
import importlib
import importlib.util
import json
import marshal
from pathlib import Path
import struct
import sys
import zipfile


def warm(archive, modules):
    archive = Path(archive)
    for name in modules:
        importlib.import_module(name)
    with zipfile.ZipFile(archive) as source:
        names = set(source.namelist())
        selected = {}
        for name, module in list(sys.modules.items()):
            if module is None or not str(getattr(module, '__file__', '')).endswith('.py'):
                continue
            relative = name.replace('.', '/')
            for candidate in [relative + '.py', relative + '/__init__.py']:
                if candidate in names:
                    selected[candidate] = str(module.__file__)
        temporary = archive.with_name(archive.name + '.warm.new')
        proofs = {}
        replaced = {name[:-3] + '.pyc' for name in selected}
        try:
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as output:
                for info in source.infolist():
                    if info.filename in replaced:
                        continue
                    data = source.read(info.filename)
                    output.writestr(copy.copy(info), data)
                    if info.filename not in selected:
                        continue
                    code = compile(data, selected[info.filename], 'exec', dont_inherit=True)
                    # PEP 552 checked-hash bytecode, authenticated against the
                    # preserved source. The target interpreter supplies its magic.
                    compiled = importlib.util.MAGIC_NUMBER + struct.pack('<I', 3) + importlib.util.source_hash(data) + marshal.dumps(code)
                    entry = zipfile.ZipInfo(info.filename[:-3] + '.pyc', date_time=(1980, 1, 1, 0, 0, 0))
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    entry.external_attr = 0o100644 << 16
                    output.writestr(entry, compiled)
                    proofs[info.filename] = {'source_sha256': hashlib.sha256(data).hexdigest(), 'bytecode_sha256': hashlib.sha256(compiled).hexdigest()}
            with zipfile.ZipFile(temporary) as verified:
                for info in source.infolist():
                    if info.filename in replaced:
                        continue
                    if verified.read(info.filename) != source.read(info.filename):
                        raise ValueError('warm_cache_changed_original_member')
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    return {'python': sys.version.split()[0], 'magic': importlib.util.MAGIC_NUMBER.hex(), 'cached_modules': len(proofs), 'bytes': archive.stat().st_size, 'proofs': proofs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--module', action='append', required=True)
    args = parser.parse_args()
    print(json.dumps(warm(args.archive, args.module), sort_keys=True))


if __name__ == '__main__':
    main()
