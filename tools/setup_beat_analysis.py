"""Deploy isolated Beat This full checkpoints without changing app/V32 environments."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import urllib.request
import zipfile
ROOT=Path(__file__).resolve().parents[1]
SOURCE_REVISION='b95c8ab0c58c2d9fcfd40508ae8dffbc05ac4f5c'
CHECKPOINT_SHA256={
    'final0':'8c328b45f59d8dd3dff219253ff6a8d6482be57d0133a29140e2febbf8eb8331',
    'final1':'365b553f43750717c907f32fbc42910f3b264d616654583df6e44472c93ead80',
    'final2':'8f810c0a44d3a979b0372b00fa61b4e3f83a8f2a6d963c7791b9d4852cdd1abc',
}
sys.path.insert(0,str(ROOT))
from malody_studio.resident import atomic

def download(url,path):
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.is_file():
        temporary=path.with_suffix(path.suffix+'.part')
        with urllib.request.urlopen(url,timeout=120) as response,temporary.open('wb') as stream:
            while chunk:=response.read(1024*1024):stream.write(chunk)
        temporary.replace(path)
    with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    return {'url':url,'path':str(path.relative_to(ROOT)),'sha256':digest,'bytes':path.stat().st_size}

def main():
    target=ROOT/'runtime/beat-analysis-venv'
    python=target/'Scripts/python.exe'
    if not python.is_file():subprocess.run([sys.executable,'-m','venv',str(target)],check=True)
    package=ROOT/'models/beat-this';package.mkdir(parents=True,exist_ok=True)
    manifest_path=package/'deployment-manifest.json'
    if manifest_path.is_file():manifest=json.loads(manifest_path.read_text())
    else:
        revision=SOURCE_REVISION
        manifest={'repository':'https://github.com/CPJKU/beat_this','revision':revision,'license':'MIT','checkpoints':[]}
        atomic(manifest_path,manifest)
    revision=manifest['revision'];archive=ROOT/'cache/beat-this-source'/f'{revision}.zip'
    download(f'https://codeload.github.com/CPJKU/beat_this/zip/{revision}',archive)
    source=ROOT/'vendor'/f'beat_this-{revision}'
    if not source.is_dir():
        with zipfile.ZipFile(archive) as bundle:bundle.extractall(ROOT/'vendor')
    lock=ROOT/'runtime/beat-analysis-requirements-lock.txt'
    subprocess.run([str(python),'-m','pip','install','-r',str(lock)],check=True)
    subprocess.run([str(python),'-m','pip','install','--no-deps',str(source)],check=True)
    subprocess.run([str(python),'-m','pip','check'],check=True)
    for checkpoint in ('final0','final1','final2'):
        record=download(f'https://cloud.cp.jku.at/public.php/dav/files/7ik4RrBKTS273gp/{checkpoint}.ckpt',package/f'{checkpoint}.ckpt')
        if record['sha256']!=CHECKPOINT_SHA256[checkpoint]:raise RuntimeError(f'{checkpoint} 权重与本轮验证版本不一致，未发布就绪状态')
        record['name']=checkpoint;manifest['checkpoints']=[x for x in manifest['checkpoints'] if x['name']!=checkpoint]+[record]
        atomic(manifest_path,manifest)
    manifest['environment_lock_sha256']=hashlib.sha256((ROOT/'runtime/beat-analysis-requirements-lock.txt').read_bytes()).hexdigest()
    atomic(manifest_path,manifest)
    # Offline CPU loading verifies all published checkpoints before advertising readiness.
    code="from beat_this.inference import load_model; from pathlib import Path; [load_model(str(p),device='cpu') for p in Path(r'"+str(package)+"').glob('final*.ckpt')]; print('three full checkpoints loaded')"
    subprocess.run([str(python),'-B','-c',code],check=True,cwd=ROOT)
    atomic(package/'deployment-ready.json',manifest)
    print(json.dumps({'ready':True,'revision':revision,'checkpoints':[x['name'] for x in manifest['checkpoints']]}))

if __name__=='__main__':main()
