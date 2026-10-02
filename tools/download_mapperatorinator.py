"""Download only the pinned mania checkpoint, into the project model registry."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import time
import urllib.request
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
REPO = 'OliBomby/Mapperatorinator-v32'
DEFAULT_REVISION = '74f22583400d259bf424819e11027c17933efe54'
DEST = ROOT / 'models' / 'mapperatorinator' / 'v32-mania'
HF_ENDPOINT = os.environ.get('HF_ENDPOINT', 'https://huggingface.co').rstrip('/')
PINNED_SHA256 = {
    'config.json': '38486bbba93ba1f8b2701ade96d9b356d4e5d57e5b6fdd4ec118c0f03af198db',
    'generation_config.json': '10b901beeb3c982c16c53cd5b37a4f9b3d4674fc70cbc3e64c7b20fdb0433f51',
    'tokenizer.json': '737148191316146ba76b6b7b0c59646baff6002ef834734dd113a8eb1e5036da',
    'mania:model.safetensors': '3d5d1c2a01ad462bcc99bafaaf44174bc47be099724574b17cd68016be5a863f',
    'timing:model.safetensors': 'a79fd39a72f2ae814b397f8b4c991fac0b5faa9fbaca3f8f0df91289b23b9707',
    'timing:README.md': '63ff96745dfd3d32d130def31e37d4a5c013412a673aacb7d102171e4a3aa1dc',
}
BACKBONE_SHA256 = {
    'openai/whisper-small': 'e6a2b489da1b5aed65a8eb8d1e7466fa867ad5643a8bc138ba708bd56b2875c4',
    'openai/whisper-base': 'a153c53883a6799b6f056b4a8d1a515c9926d03994682ba88a7618d7da0c1',
}

def get_json(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def public_endpoint(endpoint):
    parts = urlsplit(endpoint)
    # Keep the mirror host for reproducibility, but never persist credentials,
    # tokens, or query strings supplied in an endpoint URL.
    host = parts.netloc.rsplit('@', 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path.rstrip('/'), '', ''))

def prepare_backbone_configs():
    versions = {'openai/whisper-small': '973afd24965f72e36ca33b3055d56a652f456b4d',
                'openai/whisper-base': 'e37978b90ca9030d5170a5c07aadb050351a65bb'}
    records = []
    for repo, revision in versions.items():
        target = ROOT / 'models' / 'mapperatorinator' / 'backbone-configs' / repo.replace('/', '-') / 'config.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file():
            with urllib.request.urlopen(f'{HF_ENDPOINT}/{repo}/resolve/{revision}/config.json', timeout=60) as response:
                target.write_bytes(response.read())
        with target.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != BACKBONE_SHA256[repo]:
            raise RuntimeError(f'Backbone config checksum mismatch: {repo}')
        records.append({'repo': repo, 'revision': revision, 'file': str(target.relative_to(ROOT)),
                        'bytes': target.stat().st_size, 'sha256': digest})
    (ROOT / 'models' / 'mapperatorinator' / 'backbone-configs.json').write_text(json.dumps(records, indent=2), encoding='utf-8')

def main():
    global DEST
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', action='store_true', help='Download the companion timing model')
    parser.add_argument('--revision', default=DEFAULT_REVISION, help='Pinned Hugging Face revision')
    args = parser.parse_args()
    subfolder = '' if args.base else 'gamemode=3'
    if args.base:
        DEST = ROOT / 'models' / 'mapperatorinator' / 'v32-timing'
    DEST.mkdir(parents=True, exist_ok=True)
    (ROOT / 'models' / 'mapperatorinator' / 'deployment-ready.json').unlink(missing_ok=True)
    metadata = get_json(f'{HF_ENDPOINT}/api/models/{REPO}')
    revision = args.revision
    files = get_json(f'{HF_ENDPOINT}/api/models/{REPO}/tree/{revision}/' + subfolder.replace('=', '%3D'))
    records = []
    for item in files:
        if item['type'] != 'file' or Path(item['path']).name not in ('config.json', 'generation_config.json', 'model.safetensors', 'tokenizer.json', 'README.md'):
            continue
        name = Path(item['path']).name
        target = DEST / name
        partial = target.with_suffix(target.suffix + '.part')
        expected_hash = item.get('lfs', {}).get('oid')
        if not target.exists() or target.stat().st_size != item['size']:
            for attempt in range(5):
                try:
                    url = f'{HF_ENDPOINT}/{REPO}/resolve/{revision}/{item["path"]}?download=true'
                    offset = partial.stat().st_size if partial.exists() else 0
                    request = urllib.request.Request(url, headers={'Range': f'bytes={offset}-'} if offset else {})
                    with urllib.request.urlopen(request, timeout=90) as response:
                        append = offset and response.status == 206
                        count = offset if append else 0
                        last = time.monotonic()
                        with partial.open('ab' if append else 'wb') as output:
                            while chunk := response.read(4 * 1024 * 1024):
                                output.write(chunk)
                                count += len(chunk)
                                if time.monotonic() - last > 15:
                                    print(f'{name}: {count}/{item["size"]} bytes', flush=True)
                                    last = time.monotonic()
                    if partial.stat().st_size != item['size']:
                        raise RuntimeError('Incomplete download')
                    partial.replace(target)
                    break
                except Exception as exc:
                    print(f'{name}: retry {attempt + 1}: {exc}', flush=True)
                    if attempt == 4:
                        raise
                    time.sleep(3)
        with target.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if expected_hash and digest != expected_hash:
            target.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)
            raise RuntimeError(f'Checksum mismatch: {name}')
        if revision == DEFAULT_REVISION:
            pinned_key = ('timing:' if args.base else 'mania:') + name if name in ('model.safetensors', 'README.md') else name
            expected_pinned = PINNED_SHA256.get(pinned_key)
            if expected_pinned and digest != expected_pinned:
                target.unlink(missing_ok=True)
                partial.unlink(missing_ok=True)
                raise RuntimeError(f'Pinned checksum mismatch: {name}')
        records.append({'file': name, 'bytes': target.stat().st_size, 'sha256': digest})
        print(f'Verified {name}: {digest}', flush=True)
    manifest = {'repo': REPO, 'revision': revision, 'subfolder': subfolder, 'files': records,
                'source': f'{public_endpoint(HF_ENDPOINT)}/{REPO}', 'license': metadata.get('cardData', {}).get('license')}
    (DEST / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print('Mania checkpoint download complete.', flush=True)
    prepare_backbone_configs()

if __name__ == '__main__':
    main()
