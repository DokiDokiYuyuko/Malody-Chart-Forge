"""Real queued head/tail trial checks; never edit adopted chart versions."""
import io
import json
from pathlib import Path
import sys
import time

import numpy as np
import requests
import soundfile as sf

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from malody_studio.advanced import atomic, read

BASE='http://127.0.0.1:8768'
PID='55f4cb5164af47a5bb42bd3b567a6577'
OUT=ROOT/'outputs/separation-evaluation/20261004/trial-checks.json'

def api(path,payload=None):
    r=requests.get(BASE+path,timeout=90) if payload is None else requests.post(BASE+path,json=payload,timeout=90)
    r.raise_for_status();return r.json()

if __name__=='__main__':
    prefix='/api/advanced/projects/'+PID
    p=api(prefix);project_directory=ROOT/'outputs/advanced'/PID
    original,rate=sf.read(project_directory/'source.wav',dtype='float32',always_2d=True)
    record={'service':BASE,'original_source_pcm_sha256':p['source_pcm_sha256'],'runs':[]}
    for start,end in ((0,11025),(p['samples']-11025,p['samples'])):
        q=api('/api/queue')
        if q['running'] or q['waiting']:raise RuntimeError('队列繁忙，未开始边界测试')
        before=api(prefix);job=api(prefix+'/separation-trials',{'start_sample':start,'end_sample':end,
            'expected_revision':before['revision'],'settings':{'model':'melband_roformer_kim','overlap_count':4}})
        while True:
            status=api('/api/jobs/'+job['id'])
            if status['status'] in ('completed','failed','interrupted','cancelled'):break
            time.sleep(2)
        if status['status']!='completed':raise RuntimeError(status.get('error') or status['status'])
        trial=api(prefix+'/separation-trials/'+job['trial_id'])
        assert trial['core']==[start,end] and trial['frame_count']==end-start and trial['preview_only']
        assert trial['context']==[max(0,start-8*44100),min(p['samples'],end+8*44100)]
        raw=read(project_directory/'separation-trials'/trial['id']/'manifest.json')
        mix,_=sf.read(project_directory/'separation-trials'/trial['id']/'original.wav',dtype='float32',always_2d=True)
        np.testing.assert_array_equal(mix,original[start:end])
        rows=[]
        for audio in trial['audio']:
            response=requests.get(BASE+audio['audio_url'],timeout=90);response.raise_for_status()
            data,sr=sf.read(io.BytesIO(response.content),dtype='float32',always_2d=True)
            assert sr==rate==44100 and data.shape==(end-start,2) and np.isfinite(data).all()
            rows.append({'role':audio['role'],'frames':len(data),'http_status':response.status_code})
        after=api(prefix)
        assert before['segments']==after['segments'] and before['revision']==after['revision']
        record['runs'].append({'job_id':job['id'],'trial_id':trial['id'],'core':trial['core'],'context':trial['context'],
            'preview_only':True,'exact_original_crop':True,'project_unchanged':True,'audio':rows})
        atomic(OUT,record);print('Verified trial '+str(trial['core']),flush=True)
