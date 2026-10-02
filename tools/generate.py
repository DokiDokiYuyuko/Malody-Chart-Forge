"""Client for the local generation service; all artifacts remain in the project."""
import argparse
import json
from pathlib import Path
import time
import httpx

def main():
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--audio', type=Path)
    source.add_argument('--reference', action='store_true')
    parser.add_argument('--title')
    parser.add_argument('--artist', default='')
    parser.add_argument('--artwork-url', default='')
    parser.add_argument('--difficulties', nargs='+', choices=['easy', 'medium', 'normal', 'hard', 'expert', 'master', 'lunatic'], default=['easy', 'medium', 'hard'])
    parser.add_argument('--steps', type=int, choices=[20, 50, 100], default=50)
    parser.add_argument('--seed', type=int, default=20261001)
    parser.add_argument('--ln-ratio', type=float, default=.15)
    parser.add_argument('--bpm', type=float)
    parser.add_argument('--engine', choices=['mug', 'v32'], default='mug')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    options = {'difficulties': json.dumps(args.difficulties), 'steps': args.steps,
               'seed': args.seed, 'ln_ratio': args.ln_ratio, 'engine': args.engine, 'artwork_url': args.artwork_url}
    if args.bpm is not None:
        options['bpm'] = args.bpm
    with httpx.Client(base_url='http://127.0.0.1:8765', timeout=60) as client:
        health = client.get('/api/health')
        health.raise_for_status()
        if not health.json()['ready']:
            raise RuntimeError('Model is not ready')
        if args.reference:
            response = client.post('/api/reference', data=options)
        else:
            options.update(title=args.title or args.audio.stem, artist=args.artist)
            with args.audio.open('rb') as audio:
                response = client.post('/api/jobs', data=options, files={'file': (args.audio.name, audio)})
        response.raise_for_status()
        job_id = response.json()['id']
        print(json.dumps({'job_id': job_id}), flush=True)
        previous = None
        while True:
            result = client.get(f'/api/jobs/{job_id}')
            result.raise_for_status()
            job = result.json()
            state = (job['status'], job['message'], job['progress'])
            if state != previous:
                print(json.dumps({'status': state[0], 'message': state[1], 'progress': state[2]}, ensure_ascii=True), flush=True)
                previous = state
            if job['status'] == 'failed':
                raise RuntimeError(job['error'])
            if job['status'] == 'completed':
                print(json.dumps({'archive': str(root / 'outputs' / job_id / 'malody-4k.mcz'),
                                  'report': str(root / 'outputs' / job_id / 'report.json')}, ensure_ascii=True), flush=True)
                return
            time.sleep(3)

if __name__ == '__main__':
    main()
