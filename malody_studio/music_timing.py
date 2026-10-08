"""Immutable shared music evidence on the original complete PCM sample clock."""
from __future__ import annotations
import copy
import hashlib
import json
import math
import threading
from pathlib import Path
import numpy as np
import soundfile as sf
from . import beat_analysis
from .paths import ROOT

SCHEMA='music-evidence-v1'
TIMING_SCHEMA='timing-map-v1'
VERSION='shared-original-clock-4'
_lock=threading.RLock()
SELECTION=ROOT/'models/beat-this/selection.json'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()


def sealed(value):
    body={k:v for k,v in value.items() if k not in ('id','hash')}
    return {**body,'id':digest(body),'hash':digest(body)}


def source_contract(directory,project):
    path=Path(directory)/'source.wav'
    data,sr=sf.read(path,dtype='float32',always_2d=True)
    if sr!=project.get('sample_rate',44100) or len(data)!=project['samples']:
        raise ValueError('音乐证据原曲采样时钟与项目不一致')
    if not np.isfinite(data).all():raise ValueError('原曲含无效采样')
    pcm=hashlib.sha256(data.astype('<f4').tobytes()).hexdigest()
    if project.get('source_pcm_sha256') and pcm!=project['source_pcm_sha256']:
        raise ValueError('音乐证据原曲 PCM 校验失败')
    if project.get('source_sha256'):
        with path.open('rb') as stream:file_hash=hashlib.file_digest(stream,'sha256').hexdigest()
        if file_hash!=project['source_sha256']:raise ValueError('原曲文件校验失败')
    active=np.flatnonzero(np.any(data!=0,axis=1))
    last=int(active[-1])+1 if len(active) else 0
    end=last if len(data)-last>=sr else len(data)
    # Tail policy is delivery metadata; evidence always describes acoustic content.
    return data,{'pcm_sha256':pcm,'sample_rate':sr,'samples':len(data),'channels':data.shape[1],'effective_end_sample':end}


def timing_map(raw,source,adapter=None):
    stop=source['effective_end_sample'];sr=source['sample_rate']
    beats=sorted(set(int(x) for x in raw.get('beat_samples',[]) if 0<=x<stop))
    down=sorted(set(int(x) for x in raw.get('downbeat_samples',[]) if 0<=x<stop))
    fit=beat_analysis.robust_fit(beats,sr)
    # Meter only when multiple consecutive bar intervals agree on integer beat count.
    aligned=[int(np.argmin(np.abs(np.asarray(beats)-sample))) for sample in down] if beats else []
    aligned=[index for sample,index in zip(down,aligned) if abs(beats[index]-sample)<=sr*.025]
    counts=np.diff(aligned)
    meter=int(np.median(counts)) if len(counts)>=3 and 2<=np.median(counts)<=12 and np.mean(counts==np.median(counts))>=.9 else None
    observed_down=down[:]
    if fit.get('eligible') and aligned:
        # Project the measured downbeat indices through the multi-beat fit.
        down=[int(round(fit['phase_sample']+fit['indices'][index]*fit['period_samples'])) for index in aligned]
        down=[x for x in down if 0<=x<stop]
    phase=down[0] if meter is not None and down else None
    beat_eligible=len(beats)>=8 and fit.get('inlier_fraction',0)>=.9
    reasons=list(raw.get('reasons',[]))
    if meter is None:reasons.append('meter_or_bar_phase_uncertain')
    if not beat_eligible:reasons.append('unstable_or_insufficient_beats')
    # Detection remains a hypothesis: an approved selection policy can permit
    # stable measured meter/phase, but librosa scalar-only estimates cannot.
    v32=bool(beat_eligible and meter and phase is not None and fit.get('eligible'))
    points=[{'sample':phase,'bpm':fit['bpm'],'meter':meter,'confirmed':False}] if v32 else []
    local=beat_analysis.segmented_fit(beats,sr)
    if not v32 and meter and phase is not None:
        # Conservative piecewise map: every fitted region needs a measured bar
        # anchor; adjacent grids must agree at the next measured bar within
        # tolerance. Missing regions remain rejected, never extrapolated.
        pieces=[]
        for region in local:
            anchors=[x for x in down if region['range'][0]<=x<region['range'][1]]
            if not region.get('eligible') or not anchors:pieces=[];break
            observed=np.asarray(region['beat_samples']);index=int(np.argmin(abs(observed-anchors[0])))
            anchor=int(round(region['phase_sample']+region['indices'][index]*region['period_samples']))
            pieces.append({'sample':anchor,'bpm':region['bpm'],'meter':meter,'confirmed':False})
            # Project only observed downbeats to this supported local grid.
            for observed_anchor in anchors:
                fitted_index=int(np.argmin(abs(observed-observed_anchor)))
                projected=int(round(region['phase_sample']+region['indices'][fitted_index]*region['period_samples']))
                down.append(projected)
        continuity=[]
        for previous,current in zip(pieces,pieces[1:]):
            period=60*sr/previous['bpm'];cycles=(current['sample']-previous['sample'])/period
            continuity.append(abs(cycles-round(cycles))*period*1000/sr)
        if pieces and max(continuity,default=0)<=25:
            points=pieces;v32=True;beat_eligible=True
            down=sorted(set(x for x in down if 0<=x<stop));phase=points[0]['sample']
    return sealed({'schema':TIMING_SCHEMA,'source':copy.deepcopy(source),'beat_samples':beats,'downbeat_samples':down,'observed_downbeat_samples':observed_down,
        'meter':meter,'phase_sample':phase,'tempo_points':points,'fit':fit,
        'local_fits':local,'uncertain':not v32,
        'eligibility':{'beat':bool(beat_eligible),'phase':phase is not None,'meter':meter is not None,'v32':v32},
        'reasons':sorted(set(reasons)),'provenance':{**raw.get('provenance',{}),'phase_method':'multi-beat-index-fit' if fit.get('eligible') else 'piecewise-observed-bars',**({'adapter':adapter} if adapter else {})}})


def validate_timing(value,source=None):
    if value.get('schema')!=TIMING_SCHEMA or value!=sealed(value):raise ValueError('TimingMap 校验失败')
    if source is not None and value['source']!=source:raise ValueError('TimingMap 原曲时钟不匹配')
    contract=value['source'];sr=contract['sample_rate'];stop=contract['effective_end_sample']
    if type(sr)!=int or sr<=0 or type(contract['samples'])!=int or type(stop)!=int or not 0<=stop<=contract['samples']:
        raise ValueError('原曲采样范围无效')
    if value.get('meter') is not None and (type(value['meter'])!=int or not 2<=value['meter']<=12):raise ValueError('拍号证据无效')
    if value.get('phase_sample') is not None and (type(value['phase_sample'])!=int or not 0<=value['phase_sample']<stop):raise ValueError('小节相位无效')
    if set(value['eligibility'])!={'beat','phase','meter','v32'} or any(type(x)!=bool for x in value['eligibility'].values()):raise ValueError('节拍资格字段无效')
    for key in ('beat_samples','downbeat_samples'):
        points=value[key]
        if any(type(x)!=int or not 0<=x<stop for x in points) or points!=sorted(set(points)):raise ValueError('节拍采样点无效')
    if value['eligibility']['v32']:
        if not all(value['eligibility'].values()) or value['phase_sample'] not in value['downbeat_samples'] or not value['meter'] or not value['tempo_points'] or len(value['beat_samples'])<8 or len(value['downbeat_samples'])<4:raise ValueError('缺少真实小节相位证据')
    points=value['tempo_points']
    if [x['sample'] for x in points]!=sorted(set(x['sample'] for x in points)):raise ValueError('节拍锚点顺序无效')
    for point in points:
        if type(point['sample'])!=int or point['sample'] not in value['downbeat_samples'] or not math.isfinite(point['bpm']) or point['bpm']<=0 or point['meter']!=value['meter']:
            raise ValueError('节拍锚点与相位证据不一致')
    return value


def validate_evidence(value,source=None):
    if value.get('schema')!=SCHEMA or value!=sealed(value):raise ValueError('MusicEvidence 校验失败')
    if source is not None and value['source']!=source:raise ValueError('MusicEvidence 原曲时钟不匹配')
    for candidate in value['candidates']:validate_timing(candidate,value['source'])
    if value.get('acoustic'):
        from .acoustic_evidence import validate
        validate(value['acoustic'],value['source'])
    if not value.get('selected_timing_id') or value['selected_timing_id'] not in {x['id'] for x in value['candidates']}:raise ValueError('选择的节拍版本缺失')
    return value


def selected_policy():
    try:
        config=json.loads(SELECTION.read_text(encoding='utf-8'))
        detector_approved=config.get('detector_approved',config.get('accepted')) is True
        if detector_approved and config.get('adapter') in ('beat_this_final0','beat_this_final1','beat_this_final2','beat_this_ensemble'):
            config={**config,'detector_approved':True,'strong_reference_approved':config.get('strong_reference_approved') is True}
            deployment=beat_analysis.DEPLOYMENT
            if deployment.is_file():config={**config,'deployment_manifest_hash':hashlib.sha256(deployment.read_bytes()).hexdigest()}
            return config
    except (OSError,ValueError):pass
    raise RuntimeError('Beat This 默认分析配置不可用；请检查已有部署，不会退回 librosa')


def load_or_analyze(project_directory,project,progress=None,frozen_policy=None):
    from .resident import atomic
    from .workflow_log import stage, event, file_identity
    with _lock:
        with stage('music_timing.read_and_verify_pcm', source=file_identity(Path(project_directory)/'source.wav',hash_file=True),
                   expected_pcm_sha=project.get('source_pcm_sha256')):
            data,source=source_contract(project_directory,project)
        from . import acoustic_evidence
        policy=selected_policy() if frozen_policy is None else copy.deepcopy(frozen_policy)
        if not isinstance(policy,dict) or policy.get('adapter') not in ('librosa','beat_this_final0','beat_this_final1','beat_this_final2','beat_this_ensemble'):
            raise ValueError('冻结的音乐检测策略无效，请重新分析')
        with stage('music_timing.extract_acoustic_activity', estimate_beats=not policy['adapter'].startswith('beat_this'),
                   sample_rate=source.get('sample_rate'),samples=source.get('samples'),policy=policy):
            acoustic=acoustic_evidence.load_or_analyze(project_directory,project,data=data,source=source,
                                                     estimate_beats=not policy['adapter'].startswith('beat_this'))
        if policy['adapter'].startswith('beat_this'):
            deployment=beat_analysis.DEPLOYMENT
            expected=policy.get('deployment_manifest_hash')
            if not expected or not deployment.is_file() or hashlib.sha256(deployment.read_bytes()).hexdigest()!=expected:
                raise ValueError('音乐检测部署版本已改变，请重新分析')
        cache_identity={'version':VERSION,'source':source,'policy':policy}
        legacy_key=digest(cache_identity)
        key=digest({**cache_identity,'detector_execution':'beat-this-primary-v1'}) if policy['adapter'].startswith('beat_this') else legacy_key
        path=ROOT/'cache/music-evidence'/(key+'.json')
        # Good historic Beat This evidence remains reusable. A historic fallback
        # is preserved at its old path, and a new detector result gets a new key.
        legacy_path=ROOT/'cache/music-evidence'/(legacy_key+'.json')
        with stage('music_timing.probe_evidence_cache', cache_key=key, candidates=[path,legacy_path],
                   detector_execution='beat-this-primary-v1' if policy['adapter'].startswith('beat_this') else 'legacy'):
            for cached_path in dict.fromkeys((path,legacy_path)):
                if not cached_path.is_file():continue
                try:
                    cached=validate_evidence(json.loads(cached_path.read_text(encoding='utf-8')),source)
                    selected=select_timing(cached)
                    if cached.get('policy')!=policy or cached.get('version')!=VERSION:continue
                    if (not policy['adapter'].startswith('beat_this') or
                            selected['provenance'].get('adapter')==policy['adapter']):
                        if policy['adapter'].startswith('beat_this'):
                            # Preserve the old cache byte-for-byte. Reuse its real
                            # detector observations with the current activity-only
                            # evidence, excluding historic librosa beat candidates.
                            body={k:copy.deepcopy(v) for k,v in cached.items() if k not in ('id','hash')}
                            body['acoustic']=acoustic
                            body['candidates']=[row for row in body['candidates']
                                                if row['provenance'].get('adapter')!='librosa']
                            body['analysis']={**body.get('analysis',{}),'detector_execution':'beat-this-primary-v1'}
                            cached=sealed(body)
                            validate_evidence(cached,source)
                            atomic(path,cached)
                        event('cache_hit','music_timing.probe_evidence_cache',cache_path=cached_path,evidence_id=cached['id'],
                              selected_timing_id=cached.get('selected_timing_id'))
                        return copy.deepcopy(cached)
                except (OSError,ValueError,KeyError):pass
        if progress:progress('分析共享音乐节拍证据',10)
        candidates=[]
        failures=[]
        if policy['adapter'].startswith('beat_this'):
            checkpoints=('final0','final1','final2') if policy['adapter']=='beat_this_ensemble' else (policy['adapter'].removeprefix('beat_this_'),)
            # Beat This is the selected detector. A failed inference is an
            # explicit failure, never a successful librosa fallback.
            raw=beat_analysis.beat_this(Path(project_directory)/'source.wav',source['sample_rate'],checkpoints,progress,effective_end_sample=source['effective_end_sample'])
            with stage('music_timing.decode_beat_this_observations', beat_count=len(raw.get('beat_samples',[])),
                       downbeat_count=len(raw.get('downbeat_samples',[])), checkpoints=checkpoints):
                candidates.append(timing_map(raw,source))
            if policy.get('independent_v32_review'):
                review=beat_analysis.v32_timing(Path(project_directory)/'source.wav',source['sample_rate'],policy.get('v32_mode')=='super',progress,effective_end_sample=source['effective_end_sample'])
                candidates.append(timing_map(review,source))
        else:
            with stage('music_timing.librosa_beat_estimate', sample_rate=source['sample_rate'],
                       effective_samples=source['effective_end_sample']):
                raw=beat_analysis.librosa_beats(data[:source['effective_end_sample']],source['sample_rate'])
                candidates.append(timing_map(raw,source))
        body={'schema':SCHEMA,'version':VERSION,'source':source,'policy':policy,'candidates':candidates,'acoustic':acoustic,
              'analysis':{'failures':failures,'original_shared_pcm':True,'ground_truth':False}}
        if policy['adapter'].startswith('beat_this'):
            body['analysis']['detector_execution']='beat-this-primary-v1'
        with stage('music_timing.choose_candidate', candidates=[{'id':row['id'],'adapter':row['provenance'].get('adapter'),
                     'eligibility':row.get('eligibility')} for row in candidates]):
            selected=_choose_candidate(body)
        if selected['id'] not in {x['id'] for x in candidates}:candidates.append(selected)
        body['selected_timing_id']=selected['id']
        evidence=sealed(body)
        with stage('music_timing.validate_and_cache_evidence', evidence_id=evidence['id'],cache_path=path,
                   beat_count=len(selected.get('beat_samples',[])),timing_uncertain=selected.get('uncertain')):
            validate_evidence(evidence,source);atomic(path,evidence)
        event('evidence_result','music_timing.completed',evidence_id=evidence['id'],selected_timing=selected,
              candidates=[{'id':row['id'],'provenance':row.get('provenance'),'eligibility':row.get('eligibility'),
                           'beat_count':len(row.get('beat_samples',[]))} for row in candidates],cache_path=path)
        if progress:progress('音乐节拍证据已保存',100)
        return copy.deepcopy(evidence)


def select_timing(evidence):
    validate_evidence(evidence)
    identity=evidence['selected_timing_id']
    selected=next(x for x in evidence['candidates'] if x['id']==identity)
    return copy.deepcopy(selected)


def _choose_candidate(evidence):
    # Explicit selection is independent of difficulty, titles and model notes.
    candidates=evidence['candidates']
    if not candidates:raise ValueError('没有节拍分析结果')
    selected=next((x for x in candidates if x['provenance'].get('adapter','').startswith('beat_this')),candidates[0])
    if evidence['policy'].get('independent_v32_review'):
        review=next((x for x in candidates if x['provenance'].get('adapter','').startswith('v32')),None)
        agree=False
        if review and selected['beat_samples'] and review['beat_samples']:
            samples=np.asarray(review['beat_samples']);tol=evidence['source']['sample_rate']*.025
            matches=sum(float(np.min(np.abs(samples-beat)))<=tol for beat in selected['beat_samples'])
            agree=matches/max(len(samples),len(selected['beat_samples']))>=.9 and review['meter']==selected['meter']
        if not agree:
            selected=copy.deepcopy(selected);selected['eligibility']['v32']=False;selected['uncertain']=True
            selected['reasons']=sorted(set(selected['reasons']+['independent_v32_disagreement_or_unavailable']))
            selected=sealed(selected)
    if evidence['policy'].get('strong_reference_approved') is not True:
        selected=copy.deepcopy(selected);selected['eligibility']['v32']=False;selected['uncertain']=True
        selected['reasons']=sorted(set(selected['reasons']+['strong_reference_not_approved']))
        selected=sealed(selected)
    return copy.deepcopy(selected)
