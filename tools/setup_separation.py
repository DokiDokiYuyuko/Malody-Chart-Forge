"""Download pinned public inference assets and record the isolated deployment."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'models' / 'separation' / 'demucs-4.0.1'
VERSION = 'demucs-4.0.1-adapter-v1'
WEIGHTS = {'htdemucs': ['955717e8-8726e21a.th'],
           'htdemucs_ft': ['f7e0c4bc-ba3fe64a.th', 'd12395a8-e57c48e6.th', '92cfc3b6-ef3bcb9c.th', '04573f0d-f3cf25b2.th']}


def sha(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): result.update(chunk)
    return result.hexdigest()


def fetch(url, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file(): return
    temporary = destination.with_suffix(destination.suffix + '.part')
    print('Downloading', destination.name, flush=True)
    with urllib.request.urlopen(url, timeout=120) as response, temporary.open('wb') as output:
        for chunk in iter(lambda: response.read(1024 * 1024), b''): output.write(chunk)
    temporary.replace(destination)


def bootstrap():
    directory = ROOT / 'cache' / 'separation-bootstrap'
    archive = directory / 'uv.zip'
    fetch('https://github.com/astral-sh/uv/releases/download/0.9.5/uv-x86_64-pc-windows-msvc.zip', archive)
    destination = directory / 'uv'
    if not (destination / 'uv.exe').is_file():
        with zipfile.ZipFile(archive) as z: z.extractall(destination)
    print(str(destination / 'uv.exe'))


def assets(include_ft):
    TARGET.mkdir(parents=True, exist_ok=True)
    selected = ['htdemucs'] + (['htdemucs_ft'] if include_ft else [])
    files = {}
    for model in selected:
        for filename in WEIGHTS[model]:
            target = TARGET / filename
            url = 'https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/' + filename
            fetch(url, target)
            digest = sha(target)
            if not digest.startswith(filename.split('-')[1].split('.')[0]):
                raise RuntimeError('Official weight checksum prefix does not match: ' + filename)
            files[filename] = {'bytes': target.stat().st_size, 'sha256': digest, 'url': url}
        yaml = TARGET / (model + '.yaml')
        fetch('https://raw.githubusercontent.com/facebookresearch/demucs/v4.0.1/demucs/remote/' + model + '.yaml', yaml)
        files[yaml.name] = {'bytes': yaml.stat().st_size, 'sha256': sha(yaml), 'url': 'https://raw.githubusercontent.com/facebookresearch/demucs/v4.0.1/demucs/remote/' + model + '.yaml'}
    fetch('https://raw.githubusercontent.com/facebookresearch/demucs/v4.0.1/LICENSE', TARGET / 'LICENSE')
    python = ROOT / 'runtime' / 'separation-venv' / 'Scripts' / 'python.exe'
    probe = 'import json,sys,importlib.metadata as m; import torch,torchaudio,numpy,soundfile; print(json.dumps(dict(python=sys.version,torch=torch.__version__,torchaudio=torchaudio.__version__,numpy=numpy.__version__,demucs=m.version("demucs"),soundfile=soundfile.__version__,packages={x.metadata["Name"]:x.version for x in m.distributions()})))'
    process = subprocess.run([str(python), '-c', probe], capture_output=True, text=True, check=True)
    environment = json.loads(process.stdout)
    packages = environment.pop('packages')
    lock = '--extra-index-url https://download.pytorch.org/whl/cu118\n' + '\n'.join(k.lower() + '==' + v for k, v in sorted(packages.items(), key=lambda item: item[0].lower())) + '\n'
    (ROOT / 'runtime' / 'separation-requirements-lock.txt').write_text(lock, encoding='utf-8')
    manifest = {'schema': 1, 'adapter_version': VERSION, 'code_version': 'v4.0.1',
                'code_source': 'https://github.com/facebookresearch/demucs/tree/v4.0.1',
                'license': 'MIT', 'models': {name: WEIGHTS[name] + [name + '.yaml'] for name in selected},
                'files': files, 'environment': environment, 'dependency_lock_sha256': sha(ROOT / 'runtime' / 'separation-requirements-lock.txt'),
                'bootstrap': {'uv_version': '0.9.5', 'uv_archive_sha256': sha(ROOT / 'cache' / 'separation-bootstrap' / 'uv.zip')},
                'inference_verified': False}
    previous = TARGET / 'manifest.json'
    if previous.is_file():
        old = json.loads(previous.read_text(encoding='utf-8'))
        # Installing ft later must not remove a verified default registry.
        manifest['models'] = {**old.get('models', {}), **manifest['models']}
        manifest['files'] = {**old.get('files', {}), **manifest['files']}
        if old.get('environment') == environment and old.get('dependency_lock_sha256') == manifest['dependency_lock_sha256']:
            manifest['inference_verified'] = old.get('inference_verified', False)
            if old.get('verification'): manifest['verification'] = old['verification']
            verified=old.get('verified_models')
            if isinstance(verified,dict):
                manifest['verified_models']=verified
            elif old.get('inference_verified') and isinstance(old.get('verification'),dict):
                record=old['verification'];name=record.get('model')
                if name in WEIGHTS:manifest['verified_models']={name:record}
    temporary = TARGET / 'manifest.partial.json'
    temporary.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    temporary.replace(previous)
    print(json.dumps({'ready_assets': True, 'models': list(manifest['models']), 'environment': environment}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--bootstrap', action='store_true'); parser.add_argument('--ft', action='store_true')
    args = parser.parse_args()
    bootstrap() if args.bootstrap else assets(args.ft)
