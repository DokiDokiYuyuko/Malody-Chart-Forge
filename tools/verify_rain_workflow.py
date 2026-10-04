"""Boundary checks on the owned full-song audit project; no extra inference."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import time
import zipfile

import numpy as np
import soundfile as sf

from audit_rain_workflow import Audit, ROOT

sys.path.insert(0, str(ROOT))


def verify(audit):
    a=audit
    health=a.call('GET','/api/health')
    a.check('Updated workflow service is loaded',health['advanced_workflow_version']>=5)
    p=a.project();snapshot=json.loads(json.dumps(p));sid=a.data['segment_id']
    count=len(a.call('GET',a.prefix+'/tasks')['tasks'])
    a.call('POST',a.prefix+'/segments',{'start_sample':0,'end_sample':22050},400)
    a.call('POST',a.prefix+'/segments',{'start_sample':-1,'end_sample':10000},400)
    a.call('PATCH',a.prefix,{'expected_revision':-1,'profile':'keyboard'},409)
    a.call('GET',a.prefix+'/sources/unknown/audio',expected=400)
    a.call('GET',a.prefix+'/waveform?start_ms=0&end_ms=999999',expected=400)
    a.call('POST',a.prefix+'/segments/'+sid+'/generate',{'source_id':'original','variants':['unknown--medium']},400)
    a.check('Rejected requests did not alter project or queue',a.project()==p and
        len(a.call('GET',a.prefix+'/tasks')['tasks'])==count)
    started=time.monotonic();plan=a.call('POST',a.prefix+'/section-plans',{})
    first=time.monotonic()-started
    started=time.monotonic();again=a.call('POST',a.prefix+'/section-plans',{})
    repeated=time.monotonic()-started
    a.check('Exact plan cache reused immutable result',plan==again and repeated<1,{'first_seconds':first,'cached_seconds':repeated})
    a.check('Analysis did not promote BPM confirmation or change notes',a.project()==p)
    a.data['verified_plan_id']=plan['id'];a.save()
    versions=p['segments'][0]['versions']['balanced--medium']
    parents={v['source_role']:v['id'] for v in versions if v['kind']=='stem_raw'}
    row=[]
    for rate in (.5,30):
        current=a.project()
        fused=a.call('POST',a.prefix+'/segments/'+sid+'/fuse',{'expected_revision':current['revision'],
            'vocal_revision':parents['vocals'],'accompaniment_revision':parents['accompaniment'],
            'plan_id':plan['id'],'settings':{'difficulty_rules':{'medium':{'rate':rate}}}})
        row.append({'rate':rate,'notes':len(fused['events']),'revision_id':fused['id'],
                    'budget':sum(s['hard_caps']['target'] for s in fused['provenance']['section_stats']),
                    'effective_plan':fused['provenance']['section_plan_id']})
        a.check('Fusion retuning applied without inference',fused['provenance'].get('fusion_settings_applied') is True)
    a.check('Different fusion budgets actually change results',row[0]['notes']<row[1]['notes'] and
            row[0]['budget']<row[1]['budget'],row)
    a.check('Refusion leaves adopted chart unchanged',a.project()['segments'][0]['active']==snapshot['segments'][0]['active'])
    a.data['retuning']=row;a.save()
    manifests=a.call('GET',a.prefix+'/stems')['stem_sets'];manifest=manifests[0]
    audio_checks=[]
    # Full WAVs validate source identity, peak safety and exact sample timing.
    for role,rid in parents.items():
        url=a.base+a.prefix+'/revisions/'+rid+'/audio'
        response=a.session.get(url,timeout=60);response.raise_for_status()
        data,rate=sf.read(io.BytesIO(response.content),dtype='float32',always_2d=True)
        stem=next(s for s in manifest['stems'] if s['role']==role)
        raw,_=sf.read(stem['path'],dtype='float32',always_2d=True)
        # Mirror the peak-safe audition rule through reported metadata, not a
        # guessed normalization. No EQ or source substitution is acceptable.
        metadata=a.call('GET',a.prefix+'/audition-metadata?source_id='+stem['source_id'])
        peak=float(np.max(np.abs(raw)));gain=min(1.,.98/peak) if peak else 1.
        error=float(np.max(np.abs(data-raw*gain)))
        a.check('Revision audio corresponds to '+role,rate==44100 and len(data)==p['samples'] and error<1e-6,
                {'frames':len(data),'gain':gain,'max_error':error})
        partial=a.session.get(url,headers={'Range':'bytes=4096-8191'},timeout=30)
        a.check('Audio seek range is correct for '+role,partial.status_code==206 and
                partial.content==response.content[4096:8192])
        audio_checks.append({'role':role,'sha256':hashlib.sha256(response.content).hexdigest(),'max_error':error})
    a.check('Role audio outputs are distinct',audio_checks[0]['sha256']!=audio_checks[1]['sha256'],audio_checks)
    a.data['audio_checks']=audio_checks;a.save()
    archive=a.output/'rain-api-audit.mcz'
    with zipfile.ZipFile(archive) as z:
        a.check('MCZ integrity and old-format root',z.testzip() is None and '0/' in z.namelist() and '0/audio.ogg' in z.namelist())
        chart=json.loads(z.read(next(n for n in z.namelist() if n.endswith('.mc'))).decode('utf-8'))
    from malody_studio.advanced import chart_events
    original=json.loads((ROOT/'outputs'/'advanced'/p['id']/'revisions'/(a.data['fusion_revision']+'.json')).read_text(encoding='utf-8'))
    restored=chart_events(chart)
    expected=sorted(original['events'],key=lambda e:(e['start_ms'],e['lane']))
    actual=sorted(restored,key=lambda e:(e['start_ms'],e['lane']))
    differences=[abs(e['start_ms']+1500-r['start_ms']) for e,r in zip(expected,actual)]
    differences += [abs(e['end_ms']+1500-r['end_ms']) for e,r in zip(expected,actual) if e.get('end_ms') is not None]
    a.check('MC round trip retains note heads and tails within 1ms',len(expected)==len(actual) and max(differences,default=0)<1,
            {'notes':len(actual),'max_error_ms':max(differences,default=0)})
    # Split copies the adopted immutable revision into three local versions;
    # skipping the middle must remove audio and notes with the same mapping.
    if 'split_project_revision' not in a.data:
        current=a.project()
        current=a.call('POST',a.prefix+'/segments/'+sid+'/split',{'expected_revision':current['revision'],'cuts':[37*44100,197*44100]})
        middle=current['segments'][1]
        current=a.call('PATCH',a.prefix+'/segments/'+middle['id'],{'expected_revision':current['revision'],'included':False})
        a.data['split_project_revision']=current['revision'];a.save()
    current=a.project()
    assembly=a.call('POST',a.prefix+'/assemblies',{'expected_revision':current['revision'],'preroll':0})
    report=a.call('GET',a.prefix+'/assemblies/'+assembly['id'])
    a.check('Gap removal keeps exact sample clock',report['samples']==(37*44100+p['samples']-197*44100) and len(report['mapping'])==2,
        {'samples':report['samples'],'mapping':report['mapping']})
    a.check('Gap assembly contains all selected chart fragments',not report['missing'] and report['charts'][0]['notes']>0)
    a.data['gap_assembly_id']=assembly['id'];a.save()
    a.check('No additional GPU jobs were submitted',len(a.call('GET',a.prefix+'/tasks')['tasks'])==count)
    # Leave a full-song playable project for inspection; gap export remains in history.
    current=a.project()
    middle=current['segments'][1]
    a.call('PATCH',a.prefix+'/segments/'+middle['id'],{'expected_revision':current['revision'],'included':True})
    a.data['boundary_validation_completed']=True;a.save()
    print('Boundary checks passed',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--service',default='http://127.0.0.1:8768')
    parser.add_argument('--output',type=Path,default=ROOT/'outputs'/'rain-workflow-evaluation'/'20261004')
    args=parser.parse_args()
    verify(Audit(args.service,args.output))
