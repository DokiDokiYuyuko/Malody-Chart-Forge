"""Run fixed full-song comparisons through the existing exclusive GPU queue."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from malody_studio.advanced import atomic


def gpu_sample():
    result = subprocess.run(['nvidia-smi', '--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu',
                             '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=8)
    if result.returncode:
        raise RuntimeError('无法取得显卡实测数据：' + result.stderr)
    columns = [s.strip() for s in result.stdout.splitlines()[0].split(',')]
    return {'name': columns[0], 'driver': columns[1], 'total_mib': int(columns[2]),
            'used_mib': int(columns[3]), 'gpu_util_percent': int(columns[4]), 'monotonic': time.monotonic()}


def get(base, path):
    response = requests.get(base + path, timeout=30)
    response.raise_for_status()
    return response.json()


def run(base, projects, output):
    output.mkdir(parents=True, exist_ok=True)
    records = {'created': datetime.now(timezone.utc).isoformat(), 'service': base,
               'protocol': 'same immutable project PCM; exclusive service queue; no edits to selected chart versions',
               'gpu': gpu_sample(), 'runs': []}
    report = output / 'hardware.json'
    atomic(report, records)
    for pid in projects:
        for overlap in (2, 4):
            queue = get(base, '/api/queue')
            if queue['running'] or queue['waiting'] or queue['paused']:
                raise RuntimeError('队列不为空，停止提交基准，避免影响用户任务')
            project = get(base, '/api/advanced/projects/' + pid)
            settings = {'model': 'melband_roformer_kim', 'segment': 8, 'overlap_count': overlap,
                        'seed': 20261003}
            began = time.monotonic()
            response = requests.post(base + '/api/advanced/projects/' + pid + '/separations',
                                     json={'settings': settings, 'expected_revision': project['revision']}, timeout=120)
            response.raise_for_status()
            jid = response.json()['id']
            row = {'project_id': pid, 'title': project['title'], 'source_pcm_sha256': project['source_pcm_sha256'],
                   'frames': project['samples'], 'settings': settings, 'job_id': jid, 'samples': []}
            records['runs'].append(row)
            print(f'Queued {pid} overlap={overlap} job={jid}', flush=True)
            last = None
            while True:
                state = get(base, '/api/jobs/' + jid)
                row['samples'].append(gpu_sample())
                message = (state.get('message'), state.get('progress'))
                if message != last:
                    print(f'{jid}: {state["status"]} {message}', flush=True)
                    last = message
                if state['status'] in ('completed', 'failed', 'interrupted', 'cancelled'):
                    break
                if time.monotonic() - began > 7200:
                    raise RuntimeError('基准等候超过两小时；任务继续留在服务队列，不强行终止')
                time.sleep(3)
            row.update(status=state['status'], wall_seconds=round(time.monotonic() - began, 3),
                       error=state.get('error'), stem_set_id=state.get('stem_set_id'))
            if row['samples']:
                row['min_free_vram_mib'] = min(s['total_mib'] - s['used_mib'] for s in row['samples'])
                row['whole_gpu_peak_used_mib'] = max(s['used_mib'] for s in row['samples'])
                row['reserve_2gib'] = row['min_free_vram_mib'] >= 2048
            if row['stem_set_id']:
                manifest = json.loads((ROOT/'outputs'/'advanced'/pid/'stems'/row['stem_set_id']/'manifest.json').read_text(encoding='utf-8'))
                row['performance'] = manifest.get('performance')
                row['alignment'] = manifest.get('alignment')
                row['inference'] = manifest.get('effective_inference')
            atomic(report, records)
            if state['status'] != 'completed':
                raise RuntimeError(state.get('error') or '分离基准失败')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--service', default='http://127.0.0.1:8766')
    parser.add_argument('--projects', nargs='+', required=True)
    parser.add_argument('--output', type=Path, default=ROOT/'outputs'/'separation-evaluation'/'20261004')
    arguments = parser.parse_args()
    print(run(arguments.service.rstrip('/'), arguments.projects, arguments.output))
