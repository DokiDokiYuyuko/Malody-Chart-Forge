"""Queued response-curve trials. Every attempt keeps its original audio identity."""
import json
import hashlib
from pathlib import Path
import numpy as np
import soundfile as sf
from .advanced import SR, atomic
from .section_plan import canonical_hash


def write_experimental_reference(path, reference):
    """Serialize an explicit experiment clock with all upstream-required fields.

    This writes a file only; experimental parent clocks remain unqualified.
    """
    text=('osu file format v14\n\n[General]\nAudioFilename: density-input.wav\nMode: 3\n\n'
          '[Metadata]\nTitle: Frozen timing experiment\nArtist: Local\nCreator: Startrail\nVersion: Timing\n\n'
          '[Difficulty]\nCircleSize:4\nOverallDifficulty:8\nHPDrainRate:5\nApproachRate:9\n'
          'SliderMultiplier:1.4\nSliderTickRate:1\n\n[TimingPoints]\n')
    text+='\n'.join(f'{t},{60000/bpm},4,1,0,100,1,0' for t,bpm in reference['points'])+'\n\n[HitObjects]\n'
    path=Path(path);path.write_text(text,encoding='utf-8')
    return path


def run(source, directory, options, progress):
    from .mapperatorinator import generate
    from .beat_analysis import mono_audio
    snapshot=options['_advanced'];trial=snapshot['trial'];directory=Path(directory)
    src=Path(trial['source']['path']);info=sf.info(src)
    if info.samplerate!=SR or info.frames!=snapshot['project']['samples']:
        raise ValueError('校准原曲采样时钟已变化')
    data,rate=sf.read(src,dtype='float32',always_2d=True)
    from .separation import pcm_hash
    source_pcm_sha=pcm_hash(data)
    if source_pcm_sha!=trial['source']['pcm_sha']:raise ValueError('校准 PCM 身份不符')
    import librosa
    from .audio_bounds import content_end
    source_stop=min(len(data),content_end(snapshot['project']))
    if source_stop<trial['bounds'][1]:
        raise ValueError('实验范围超出高级生成的有效 PCM 边界')
    mono,mono_policy=mono_audio(data[:source_stop])
    y=librosa.resample(mono,orig_sr=rate,target_sr=22050)
    wave=directory/'density-input.wav';sf.write(wave,y,22050,subtype='FLOAT')
    windows=trial.get('windows') or [trial['bounds']]
    from .advanced_generation import _v32_time_bounds
    presets=[{'key':'condition-'+str(c).replace('.','_')+'__window-'+str(index),
              'label':'校准 '+str(c)+' / '+str(index+1),'sr':c,
              'seed':trial['seed'],'start_time':_v32_time_bounds(bounds)[0],
              'end_time':_v32_time_bounds(bounds)[1]}
             for c in trial['conditions'] for index,bounds in enumerate(windows)]
    local={**snapshot['settings'],'engine':'v32','title':snapshot['project']['title'],
           'artist':snapshot['project']['artist'],'difficulties':['expert'],
           'pattern':trial.get('pattern','balanced'),'_advanced_presets':presets,
           'seed':trial['seed'],'experimental_parameter_snapshot':True}
    if trial.get('reference'):
        path=write_experimental_reference(directory/'frozen-experimental-timing.osu',trial['reference'])
        local['timing_reference']=str(path)
    elif trial.get('timing_reference'):local['timing_reference']=trial['timing_reference']
    from .paths import ROOT
    adapter_files=['malody_studio/density_trials.py','malody_studio/mapperatorinator.py',
                   'malody_studio/density_timing_fallback.py',
                   'malody_studio/audio_bounds.py','malody_studio/beat_analysis.py',
                   'tools/mapperatorinator_worker.py','vendor/Mapperatorinator/inference.py']
    adapter={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in adapter_files}
    input_file_sha=hashlib.sha256(wave.read_bytes()).hexdigest()
    with src.open('rb') as stream:
        source_file_sha=hashlib.file_digest(stream,'sha256').hexdigest()
    prepared_pcm_sha=pcm_hash(y)
    timing_fallback_identity=None
    fallback_artifact=trial.get('timing_fallback_artifact')
    if fallback_artifact is not None:
        if local.get('timing_reference'):
            raise ValueError('精确模型计时回退不能与首轮计时参考同时使用')
        from .density_timing_fallback import stage_artifact
        fallback_path,timing_fallback_identity=stage_artifact(
            Path(__file__).resolve().parents[1],fallback_artifact,
            project_id=snapshot['project']['id'],source=trial['source'],
            source_file_sha=source_file_sha,source_pcm_sha=source_pcm_sha,
            source_rate=rate,source_frames=len(data),prepared_pcm_sha=prepared_pcm_sha,
            prepared_wav_sha=input_file_sha,directory=directory)
        local['timing_fallback_reference']=str(fallback_path)
        local['timing_fallback_identity']=timing_fallback_identity
    atomic(directory/'trial-execution.json',{'adapter_hashes':adapter,'input_file_sha256':input_file_sha,
        'input_preparation_policy':'advanced-source-prefix-v1',
        'time_bounds_policy':'advanced-integer-ms-v1',
        'input_pcm_identity':{
            'source':{'source_id':trial['source'].get('source_id'),'source_role':trial['source'].get('source_role'),
                      'file_sha256':source_file_sha,'pcm_sha256':source_pcm_sha,'sample_rate':rate,'frames':len(data)},
            'prepared':{'source_stop_sample_exclusive':source_stop,'tail_policy':snapshot['project'].get('tail_trim'),
                        'mono_policy':mono_policy,'sample_rate':22050,'channels':1,'subtype':'FLOAT',
                        'frames':len(y),'pcm_sha256':prepared_pcm_sha,'wav_sha256':input_file_sha}},
        'trial':trial,'presets':presets,'reference_used':bool(local.get('timing_reference')),
        'timing_fallback_identity':timing_fallback_identity})
    charts,metadata=generate(wave,directory,local,progress)
    observations=[]
    for condition in trial['conditions']:
        owned=[];window_results=[]
        for index,bounds in enumerate(windows):
            preset=next(p for p in presets if p['sr']==condition and p['key'].endswith('__window-'+str(index)))
            parsed=charts.get(preset['key']);notes=(parsed[0] if isinstance(parsed,tuple) else parsed) if parsed else []
            a,b=_v32_time_bounds(bounds);part=[n for n in notes if a<=n.start<b];owned.extend(part)
            window_results.append({'key':preset['key'],'bounds':bounds,'heads':len(part),
                'status':'completed' if parsed else 'failed','error':metadata.get('rejected_charts',{}).get(preset['key'])})
        observations.append({'engine':'v32','source_role':trial['source']['source_role'],
            'recording_id':snapshot['project']['source_pcm_sha256'],'project_id':snapshot['project']['id'],
            'split':trial['split'],'range':trial['bounds'],'condition':condition,'heads':len(owned),
            'active_seconds':trial['active_seconds'],'achieved_rate':len(owned)/max(1e-9,trial['active_seconds']),
            'seed':trial['seed'],'pattern':trial.get('pattern','balanced'),
            'holds':sum(n.end is not None for n in owned),'windows':window_results,
            'reference_mode':trial.get('reference_mode','project'),
            'timing_fallback_identity':timing_fallback_identity,
            'status':'completed' if all(r['status']=='completed' for r in window_results) else 'partial' if owned else 'failed'})
    result={'observations':observations,'trial':trial,'metadata':metadata,'adapter_hashes':adapter,
            'timing_fallback_identity':timing_fallback_identity}
    atomic(directory/'density-trial.json',result)
    return result
