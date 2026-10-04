"""Reproducible API-only audit against an already deployed, single GPU queue.

Creates an owned project from the cached complete YouTube source. Never edits
existing projects. Each response and timing is saved so the run can be resumed.
"""
import argparse
import json
from pathlib import Path
import time

import requests

ROOT = Path(__file__).resolve().parents[1]


class Audit:
    def __init__(self, base, output):
        self.base, self.output = base.rstrip('/'), output
        output.mkdir(parents=True, exist_ok=True)
        self.path = output / 'api-audit.json'
        self.data = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {
            'source_video_id': 'VuvVM5KSHnE', 'service': self.base, 'calls': [], 'checks': []}
        self.session = requests.Session()

    def save(self):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)

    def call(self, method, path, body=None, expected=200):
        began = time.monotonic()
        r = self.session.request(method, self.base + path, json=body, timeout=240)
        try:
            value = r.json()
        except ValueError:
            value = {'bytes': len(r.content), 'content_type': r.headers.get('Content-Type')}
        self.data['calls'].append({'method': method, 'path': path, 'body': body,
            'status': r.status_code, 'seconds': round(time.monotonic()-began, 3), 'response': value})
        self.save()
        if r.status_code != expected:
            raise RuntimeError(f'{method} {path}: {r.status_code}: {value}')
        return value

    def check(self, name, condition, evidence=None):
        self.data['checks'].append({'name': name, 'passed': bool(condition), 'evidence': evidence})
        self.save()
        if not condition:
            raise AssertionError(name + ': ' + str(evidence))

    @property
    def prefix(self):
        return '/api/advanced/projects/' + self.data['project_id']

    def project(self):
        return self.call('GET', self.prefix)

    def wait(self, job_id):
        previous = None
        while True:
            r = self.session.get(self.base + '/api/jobs/' + job_id, timeout=30)
            r.raise_for_status()
            job = r.json()
            state = (job['status'], job.get('progress'), job.get('message'))
            if state != previous:
                print(json.dumps({'job_id': job_id, 'state': state}, ensure_ascii=True), flush=True)
                previous = state
                self.data.setdefault('progress', []).append({'job_id':job_id, 'time': time.time(), 'state': state})
                self.save()
            if job['status'] in ('completed', 'failed', 'interrupted', 'cancelled'):
                self.data.setdefault('jobs', {})[job_id] = job
                self.save()
                self.check('Task completed without partial failures', job['status']=='completed' and not job.get('advanced_errors'),
                           {'id':job_id, 'status':job['status'], 'errors':job.get('advanced_errors'), 'error':job.get('error')})
                return job
            time.sleep(3)

    def run(self, stage):
        health = self.call('GET', '/api/health')
        self.check('GPU available', health.get('cuda_available'), health.get('gpu'))
        if 'project_id' not in self.data:
            queue = self.call('GET', '/api/queue')
            self.check('Queue idle before audit', not queue['running'] and not queue['waiting'])
            p = self.call('POST', '/api/advanced/projects/from-source', {'type':'youtube', 'id':self.data['source_video_id']})
            self.data['project_id'] = p['id']
            self.save()
            self.check('Full music imported; no pre-generated charts', p['samples']==10146528 and not p['segments'],
                       {'duration':p['duration'], 'samples':p['samples'], 'segments':len(p['segments'])})
            print('Audit project ' + p['id'], flush=True)
        if stage == 'import':
            return
        if 'separation_job' not in self.data:
            p = self.project()
            job = self.call('POST', self.prefix + '/separations', {'expected_revision':p['revision'],
                'settings':{'model':'melband_roformer_kim','overlap_count':4}})
            self.data['separation_job'] = job['id']
            self.save()
        self.wait(self.data['separation_job'])
        stems = self.call('GET', self.prefix + '/stems')['stem_sets']
        self.check('Complete Kim stem set exists', len(stems)==1)
        manifest = stems[0]
        self.data['stem_set_id'] = manifest['id']
        self.save()
        self.check('Both stems have full original clock', manifest['frame_count']==10146528 and
            sorted(x['role'] for x in manifest['stems'])==['accompaniment','vocals'] and
            all(x['frames']==10146528 and x['origin_sample']==0 for x in manifest['stems']))
        for row in manifest['stems']:
            self.call('GET', self.prefix+'/waveform?source_id='+row['source_id']+'&start_ms=14000&end_ms=18000&points=128')
            self.call('GET', self.prefix+'/audition-metadata?source_id='+row['source_id'])
        if stage == 'separation':
            return
        if 'plan_id' not in self.data:
            p = self.project()
            settings = {**p['settings'], 'fixed_seed':True, 'seed':20261004, 'section_granularity':'coarse',
                        'dynamic_enabled':True, 'engine':'v32', 'strategy':'independent'}
            p = self.call('PATCH', self.prefix, {'expected_revision':p['revision'], 'settings':settings,
                        'patterns':['balanced'], 'difficulties':['medium']})
            plan = self.call('POST', self.prefix + '/section-plans', {})
            self.data['plan_id'] = plan['id']
            self.save()
            self.check('Plan covers full original sample clock', plan['samples']==10146528 and
                        plan['sections'][0]['core'][0]==0 and plan['sections'][-1]['core'][1]==10146528)
            self.check('Analysis leaves chart/reference time unchanged', self.project()['tempo']==p['tempo'])
        if stage == 'analysis':
            return
        p = self.project()
        if not p['segments']:
            p = self.call('POST', self.prefix + '/segments', {'expected_revision':p['revision'],
                'start_sample':0, 'end_sample':p['samples'], 'name':'API audit - full song'})
        sid = p['segments'][0]['id']
        self.data['segment_id'] = sid
        self.save()
        if 'generation_job' not in self.data:
            job = self.call('POST', self.prefix + '/segments/' + sid + '/generate', {
                'expected_revision':p['revision'], 'source_id':'vocals_accompaniment',
                'stem_set_id':manifest['id'], 'variants':['balanced--medium'], 'auto_fuse':False})
            self.data['generation_job'] = job['id']
            self.save()
            print('Inference count ' + str(job['inference_count']), flush=True)
        self.wait(self.data['generation_job'])
        if stage == 'generate':
            return
        p = self.project()
        versions = p['segments'][0]['versions']['balanced--medium']
        pair = {v['source_role']:v for v in versions if v['kind']=='stem_raw'}
        self.check('Both raw roles saved independently', set(pair)=={'vocals','accompaniment'})
        if 'fusion_revision' not in self.data:
            fused = self.call('POST', self.prefix + '/segments/' + sid + '/fuse', {'expected_revision':p['revision'],
                'vocal_revision':pair['vocals']['id'], 'accompaniment_revision':pair['accompaniment']['id'],
                'plan_id':self.data['plan_id']})
            self.data['fusion_revision'] = fused['id']
            self.save()
        p = self.project()
        p = self.call('POST', self.prefix+'/segments/'+sid+'/select', {'expected_revision':p['revision'],
            'variant':'balanced--medium', 'revision_id':self.data['fusion_revision']})
        if 'assembly_id' not in self.data:
            assembly = self.call('POST', self.prefix+'/assemblies', {'expected_revision':p['revision'], 'preroll':1.5})
            self.data['assembly_id'] = assembly['id']
            self.save()
        self.call('GET', self.prefix+'/assemblies/'+self.data['assembly_id'])
        self.call('GET', self.prefix+'/assemblies/'+self.data['assembly_id']+'/charts/balanced--medium')
        r = self.session.get(self.base+self.prefix+'/assemblies/'+self.data['assembly_id']+'/download',timeout=120)
        r.raise_for_status()
        (self.output/'rain-api-audit.mcz').write_bytes(r.content)
        self.check('Download contains MCZ ZIP', r.content[:2]==b'PK', len(r.content))
        print('All API stages finished', flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--service',default='http://127.0.0.1:8768')
    parser.add_argument('--output',type=Path,default=ROOT/'outputs'/'rain-workflow-evaluation'/'20261004')
    parser.add_argument('--stage',choices=['import','separation','analysis','generate','export'],default='export')
    args=parser.parse_args()
    Audit(args.service,args.output).run(args.stage)
