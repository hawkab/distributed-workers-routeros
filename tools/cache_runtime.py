#!/usr/bin/env python3
"""Stage only an authenticated public runtime in the ignored CI image cache."""
import argparse,json,pathlib,shutil
from prepare import ROOT,verify
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--archive',type=pathlib.Path,required=True);p.add_argument('--manifest',type=pathlib.Path,required=True);a=p.parse_args()
m=verify(a.archive,a.manifest);target=ROOT/'.build/runtime-cache'/m['version_name'];target.mkdir(parents=True,exist_ok=True)
for source,name in [(a.archive,'fleet-worker-portable.zip'),(a.manifest,'portable-manifest.json')]:
    dest=target/name
    if dest.exists() and dest.read_bytes()!=source.read_bytes():raise ValueError('authenticated_cache_entry_conflict')
    shutil.copyfile(source,dest)
print(json.dumps({'cached':True,'version':m['version_name'],'sha256':m['sha256']}))
