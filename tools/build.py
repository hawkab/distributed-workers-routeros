#!/usr/bin/env python3
"""Assemble an ARM64 image from signature-verified packages; no remote build host."""
import argparse,gzip,hashlib,importlib.util,io,json,os,pathlib,shutil,subprocess,tarfile,tempfile,urllib.request
ROOT=pathlib.Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('prepare',ROOT/'tools/prepare.py');prepare=importlib.util.module_from_spec(s);s.loader.exec_module(prepare)
PACKAGES=['musl','busybox','alpine-baselayout','alpine-keys','ca-certificates','php84','php84-curl','php84-openssl','php84-phar','php84-zlib','php84-pcntl','php84-posix']
def canonical(v):return json.dumps(v,sort_keys=True,separators=(',',':')).encode()
def add_bytes(t,name,data,mode=0o644):
    info=tarfile.TarInfo(name);info.size=len(data);info.mode=mode;info.mtime=0;t.addfile(info,io.BytesIO(data))
def build(out,tag,commit):
    out=pathlib.Path(out);out.mkdir(parents=True,exist_ok=True);lock=json.loads((ROOT/'runtime.lock.json').read_text())
    with tempfile.TemporaryDirectory(prefix='worker-image-') as folder:
        work=pathlib.Path(folder);root=work/'root';root.mkdir();(root/'etc/apk').mkdir(parents=True)
        shutil.copytree('/usr/share/apk/keys/aarch64',root/'etc/apk/keys');shutil.copyfile('/etc/apk/repositories',root/'etc/apk/repositories')
        # The builder's official Alpine keys authenticate every foreign package.
        # Package scripts cannot run ARM code on the builder; all runtime setup
        # is explicit below and has the same purpose as container/Dockerfile.
        p=subprocess.run(['apk','--root',str(root),'--arch','aarch64','--initdb','--keys-dir','/usr/share/apk/keys/aarch64','--repositories-file','/etc/apk/repositories','add','--no-cache','--no-scripts',*PACKAGES],capture_output=True)
        (out/'package-install.log').write_bytes(p.stdout+p.stderr)
        if p.returncode:raise RuntimeError('verified_package_install_failed')
        for applet in subprocess.check_output(['busybox','--list-full'],text=True).splitlines():
            link=root/applet.lstrip('/')
            if not link.exists() and not link.is_symlink():
                link.parent.mkdir(parents=True,exist_ok=True);link.symlink_to('/bin/busybox')
        base='https://github.com/hawkab/distributed-workers-portable/releases/download/v'+lock['version']+'/'
        http=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        cache=pathlib.Path(os.environ.get('FLEET_RUNTIME_CACHE','/opt/fleet-runtime-cache'))/lock['version']
        for name in ['fleet-worker-portable.zip','portable-manifest.json']:
            if (cache/name).is_file():shutil.copyfile(cache/name,work/name)
            else:
                with http.open(base+name,timeout=30) as r,(work/name).open('wb') as w:shutil.copyfileobj(r,w,65536)
        prepare.prepare(work/'fleet-worker-portable.zip',work/'portable-manifest.json',root/'opt/fleet')
        for name in ['config','state','programs','opt/worker','usr/local/bin','etc/ssl/certs','proc','sys','dev']:(root/name).mkdir(parents=True,exist_ok=True)
        (root/'usr/local/bin/php').symlink_to('/usr/bin/php84')
        for name in ['supervise.php','tick.php']:shutil.copyfile(ROOT/'container'/name,root/'opt/worker'/name)
        with (root/'etc/passwd').open('a') as f:f.write('worker:x:10001:10001:worker:/state:/sbin/nologin\n')
        with (root/'etc/group').open('a') as f:f.write('worker:x:10001:\n')
        (root/'etc/php84/conf.d/90-worker.ini').write_text('memory_limit=-1\nmax_execution_time=0\nphar.readonly=1\n')
        certs=sorted((root/'usr/share/ca-certificates').rglob('*.crt'));assert certs
        (root/'etc/ssl/certs/ca-certificates.crt').write_bytes(b'\n'.join(p.read_bytes() for p in certs))
        # Execute the exact platform-independent process supervisor on the native
        # builder; the resulting ARM64 image is tested on supported hardware.
        env=dict(os.environ,WORKER_SUPERVISOR=str(ROOT/'container/supervise.php'))
        probe=subprocess.run(['php',str(ROOT/'tests/supervisor.php')],env=env,capture_output=True)
        if probe.returncode or probe.stdout.count(b'ok:')!=4:raise RuntimeError('container_process_tests_failed')
        print(probe.stdout.decode(),end='')
        layer=work/'layer.tar'
        def metadata(info):
            info.uid=10001 if info.name in ['config','state','programs'] else 0;info.gid=info.uid;info.uname='';info.gname='';info.mtime=0
            if info.name in ['config','state','programs']:info.mode=0o700
            return info
        with tarfile.open(layer,'w',format=tarfile.PAX_FORMAT) as t:
            for item in sorted(root.rglob('*')):t.add(item,arcname=item.relative_to(root).as_posix(),recursive=False,filter=metadata)
        with layer.open('rb') as f:diff=hashlib.file_digest(f,'sha256').hexdigest()
        config={'created':'1970-01-01T00:00:00Z','architecture':'arm64','os':'linux','config':{'User':'10001:10001','Env':['PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'],'Entrypoint':['php','/opt/worker/supervise.php'],'Cmd':['/config/worker.local.json'],'WorkingDir':'/state','StopSignal':'SIGTERM','Labels':{'org.opencontainers.image.revision':commit,'org.opencontainers.image.version':tag,'local.worker.runtime.sha256':lock['sha256']}},'rootfs':{'type':'layers','diff_ids':['sha256:'+diff]},'history':[{'created':'1970-01-01T00:00:00Z','created_by':'signature-verified package assembly'}]}
        raw=canonical(config);image_id=hashlib.sha256(raw).hexdigest();manifest=[{'Config':image_id+'.json','RepoTags':['fleet-worker-routeros:'+tag.lstrip('v')],'Layers':['layer.tar']}]
        target=out/'fleet-worker-routeros-arm64.tar.gz'
        with target.open('wb') as dest,gzip.GzipFile(fileobj=dest,mode='wb',filename='',mtime=0) as packed,tarfile.open(fileobj=packed,mode='w|',format=tarfile.PAX_FORMAT) as t:
            add_bytes(t,'manifest.json',canonical(manifest));add_bytes(t,image_id+'.json',raw);t.add(layer,arcname='layer.tar',filter=lambda i:metadata(i))
        with target.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
        return {'image_id':'sha256:'+image_id,'architecture':'arm64','sha256':digest,'length':target.stat().st_size,'image_bytes':sum(p.stat().st_size for p in root.rglob('*') if p.is_file() and not p.is_symlink()),'runtime_sha256':lock['sha256']}
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=pathlib.Path,required=True);p.add_argument('--tag',required=True);p.add_argument('--commit',required=True);a=p.parse_args();print(json.dumps(build(a.output,a.tag,a.commit)))
