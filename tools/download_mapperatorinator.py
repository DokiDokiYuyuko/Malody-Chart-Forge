"""Download only the pinned mania checkpoint, into the project model registry."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REPO = 'OliBomby/Mapperatorinator-v32'
DEFAULT_REVISION = '74f22583400d259bf424819e11027c17933efe54'
DEST = ROOT / 'models' / 'mapperatorinator' / 'v32-mania'

def get_json(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)

def prepare_backbone_configs():
    versions = {'openai/whisper-small': '973afd24965f72e36ca33b3055d56a652f456b4d',
                'openai/whisper-base': 'e37978b90ca9030d5170a5c07aadb050351a65bb'}
    records = []
    for repo, revision in versions.items():
        target = ROOT / 'models' / 'mapperatorinator' / 'backbone-configs' / repo.replace('/', '-') / 'config.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file():
            with urllib.request.urlopen(f'https://huggingface.co/{repo}/resolve/{revision}/config.json', timeout=60) as response:
                target.write_bytes(response.read())
        with target.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
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
    metadata = get_json(f'https://huggingface.co/api/models/{REPO}')
    revision = args.revision
    files = get_json(f'https://huggingface.co/api/models/{REPO}/tree/{revision}/' + subfolder.replace('=', '%3D'))
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
                    url = f'https://huggingface.co/{REPO}/resolve/{revision}/{item["path"]}?download=true'
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
        digest = hashlib.file_digest(target.open('rb'), 'sha256').hexdigest()
        if expected_hash and digest != expected_hash:
            raise RuntimeError(f'Checksum mismatch: {name}')
        records.append({'file': name, 'bytes': target.stat().st_size, 'sha256': digest})
        print(f'Verified {name}: {digest}', flush=True)
    manifest = {'repo': REPO, 'revision': revision, 'subfolder': subfolder, 'files': records,
                'source': f'https://huggingface.co/{REPO}', 'license': metadata.get('cardData', {}).get('license')}
    (DEST / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print('Mania checkpoint download complete.', flush=True)
    prepare_backbone_configs()

if __name__ == '__main__':
    main()
