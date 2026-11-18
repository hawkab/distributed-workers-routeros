import hashlib,importlib.util,io,json,pathlib,sys,tarfile,tempfile,unittest
ROOT=pathlib.Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'ci'))
s=importlib.util.spec_from_file_location('router_release',ROOT/'ci/router_release.py');release=importlib.util.module_from_spec(s);s.loader.exec_module(release)
class ContainerTests(unittest.TestCase):
    def image(self,path,*,architecture='arm64',commit='source',corrupt=False):
        layer=b'opaque filesystem layer';h=hashlib.sha256(layer).hexdigest();lock=json.loads((ROOT/'runtime.lock.json').read_text())
        config={'os':'linux','architecture':architecture,'config':{'Entrypoint':['php','/opt/worker/supervise.php'],'Cmd':['/config/worker.local.json'],'Labels':{'org.opencontainers.image.version':'v0.1.0','org.opencontainers.image.revision':commit,'local.worker.runtime.sha256':lock['sha256']}},'rootfs':{'diff_ids':['sha256:'+h]}}
        with tarfile.open(path,'w:gz') as t:
            for name,value in [('manifest.json',json.dumps([{'Config':'config.json','Layers':['layer.tar']}]).encode()),('config.json',json.dumps(config).encode()),('layer.tar',layer+b'changed' if corrupt else layer)]:
                i=tarfile.TarInfo(name);i.size=len(value);t.addfile(i,io.BytesIO(value))
    def test_arm64_identity_and_layer_hashes(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'image';self.image(p);r=release.inspect_archive(ROOT,p,{'name':'0.1.0'},commit='source');self.assertEqual(r['architecture'],'arm64')
    def test_wrong_architecture_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'image';self.image(p,architecture='amd64')
            with self.assertRaisesRegex(release.common.Failure,'platform_mismatch'):release.inspect_archive(ROOT,p,{'name':'0.1.0'},commit='source')
    def test_other_source_commit_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'image';self.image(p,commit='other')
            with self.assertRaisesRegex(release.common.Failure,'source_identity_mismatch'):release.inspect_archive(ROOT,p,{'name':'0.1.0'},commit='source')
    def test_changed_layer_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'image';self.image(p,corrupt=True)
            with self.assertRaisesRegex(release.common.Failure,'layer_checksum_mismatch'):release.inspect_archive(ROOT,p,{'name':'0.1.0'},commit='source')
if __name__=='__main__':unittest.main()
