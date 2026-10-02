"""Resumable, range-checked downloader for large public model assets."""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import time
import urllib.request
import subprocess

def download(url, destination, workers=12, sha256=None, use_curl=False):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if use_curl:
        headers = subprocess.check_output(['curl.exe', '-sSIL', '--fail', '--max-time', '60', url], text=True)
        import re
        size = int(re.findall(r'(?im)^content-length:\s*(\d+)', headers)[-1])
        direct = url
    else:
        with urllib.request.urlopen(urllib.request.Request(url, method='HEAD'), timeout=60) as r:
            size = int(r.headers['Content-Length'])
            direct = r.url
    chunk_size = 16 * 1024 * 1024
    parts = destination.parent / (destination.name + '.parts')
    parts.mkdir(exist_ok=True)
    def fetch(index):
        start = index * chunk_size
        end = min(size - 1, start + chunk_size - 1)
        part = parts / str(index)
        expected = end - start + 1
        if part.exists() and part.stat().st_size == expected:
            return expected
        for attempt in range(4):
            try:
                if use_curl:
                    header_file = parts / f'{index}.headers'
                    subprocess.run(['curl.exe', '-sSL', '--fail', '--max-time', '180', '--range', f'{start}-{end}', '-D', str(header_file), '-o', str(part), direct], check=True)
                    if f'bytes {start}-{end}/' not in header_file.read_text():
                        raise RuntimeError('Server did not honor byte range')
                    if part.stat().st_size != expected:
                        raise RuntimeError('Incomplete range')
                    return expected
                req = urllib.request.Request(direct, headers={'Range': f'bytes={start}-{end}'})
                with urllib.request.urlopen(req, timeout=120) as r:
                    if r.status != 206 or not r.headers.get('Content-Range', '').startswith(f'bytes {start}-{end}/'):
                        raise RuntimeError('Server did not honor requested byte range')
                    with part.open('wb') as f:
                        while data := r.read(1024 * 1024):
                            f.write(data)
                if part.stat().st_size != expected:
                    raise RuntimeError('Incomplete range')
                return expected
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))
    count = (size + chunk_size - 1) // chunk_size
    complete = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for task in concurrent.futures.as_completed([pool.submit(fetch, i) for i in range(count)]):
            complete += task.result()
            print(json.dumps({'downloaded_mb': round(complete / 1048576), 'total_mb': round(size / 1048576)}), flush=True)
    temporary = destination.with_suffix(destination.suffix + '.assembling')
    digest = hashlib.sha256()
    with temporary.open('wb') as target:
        for i in range(count):
            with (parts / str(i)).open('rb') as source:
                while data := source.read(4 * 1024 * 1024):
                    digest.update(data)
                    target.write(data)
    actual = digest.hexdigest()
    if sha256 and actual != sha256:
        raise RuntimeError(f'SHA256 mismatch: {actual}')
    temporary.replace(destination)
    print(json.dumps({'path': str(destination), 'bytes': size, 'sha256': actual}), flush=True)

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('url')
    p.add_argument('destination')
    p.add_argument('--workers', type=int, default=12)
    p.add_argument('--sha256')
    p.add_argument('--curl', action='store_true')
    a = p.parse_args()
    download(a.url, a.destination, a.workers, a.sha256, a.curl)
