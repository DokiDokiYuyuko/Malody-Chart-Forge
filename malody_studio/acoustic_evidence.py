"""Immutable full-source acoustic cache, independent of user arrangement settings."""
import copy
import hashlib
import json
from pathlib import Path
import numpy as np
from . import section_plan

SCHEMA = 'original-acoustic-v1'


def validate(value, source=None):
    body = {k:v for k,v in value.items() if k not in ('id','hash')}
    identity = section_plan.canonical_hash(body)
    if value.get('schema') != SCHEMA or value.get('id') != identity or value.get('hash') != identity:
        raise ValueError('原曲声学证据校验失败')
    if source and (value['source']['pcm_sha256'] != source['pcm_sha256'] or value['source']['samples'] != source['samples']):
        raise ValueError('原曲声学证据源时钟不符')
    return value


def load_or_analyze(directory, project, data=None, source=None, estimate_beats=True):
    from .advanced import atomic
    data = section_plan.pcm_audio(Path(directory)/'source.wav') if data is None else data
    pcm = hashlib.sha256(np.ascontiguousarray(data,dtype='<f4').tobytes()).hexdigest()
    if project.get('source_pcm_sha256') and pcm != project['source_pcm_sha256']:
        raise ValueError('原曲声学证据 PCM 校验失败')
    contract = {'pcm_sha256':pcm,'samples':len(data),'sample_rate':44100}
    cache_identity={'schema':SCHEMA,'source':contract}
    if not estimate_beats:cache_identity['analysis_policy']='spectral-flux-only-v1'
    key = section_plan.canonical_hash(cache_identity)
    path = Path(directory)/'acoustic-evidence'/(key+'.json')
    if path.is_file():
        try:return copy.deepcopy(validate(json.loads(path.read_text(encoding='utf-8')),contract))
        except (ValueError,KeyError,OSError):pass
    if not estimate_beats:
        legacy_key=section_plan.canonical_hash({'schema':SCHEMA,'source':contract})
        legacy=Path(directory)/'acoustic-evidence'/(legacy_key+'.json')
        if legacy.is_file():
            try:
                old=validate(json.loads(legacy.read_text(encoding='utf-8')),contract)
                body={k:copy.deepcopy(v) for k,v in old.items() if k not in ('id','hash')}
                body['beat_estimate_samples']=[]
                body['provenance']['beat_estimator']='disabled-use-shared-beat-this'
                identity=section_plan.canonical_hash(body)
                value={**body,'id':identity,'hash':identity}
                atomic(path,value)
                return copy.deepcopy(validate(value,contract))
            except (ValueError,KeyError,OSError):pass
    profile,envelope,beats,step = (section_plan.rhythm_features(data) if estimate_beats
                                 else section_plan.rhythm_features(data,estimate_beats=False))
    # Exact silence is separate from low activity: unknown quiet sound is audible.
    for row in profile:
        row['exact_silence'] = not np.any(data[row['start_sample']:row['end_sample']] != 0)
        if not row['exact_silence'] and row['active_fraction'] == 0:
            row['active_fraction'] = 1.
            row['activity_uncertain'] = True
    from scipy.signal import find_peaks
    peaks,_ = find_peaks(envelope,distance=4,prominence=.2,height=.3)
    body = {'schema':SCHEMA,'source':contract,'effective_range':[0,len(data)],'profile':profile,
            'onset_samples':[min(len(data)-1,round(int(i)*step*44100)) for i in peaks],
            'onset_strengths':[float(envelope[i]) for i in peaks],
            'beat_estimate_samples':[round(t*44100) for t in beats],
            'provenance':{'analysis':'channel-positive-spectral-flux','settings_independent':True}}
    if not estimate_beats:body['provenance']['beat_estimator']='disabled-use-shared-beat-this'
    identity = section_plan.canonical_hash(body)
    value = {**body,'id':identity,'hash':identity}
    atomic(path,value)
    return copy.deepcopy(validate(value,contract))
