"""Audio-clock beat adapters. No difficulty, editor tempo or note snapping inputs."""
from __future__ import annotations
import json
import subprocess
import time
import uuid
from pathlib import Path
import numpy as np
from .paths import ROOT

PYTHON = ROOT / 'runtime/beat-analysis-venv/Scripts/python.exe'
DEPLOYMENT = ROOT / 'models/beat-this/deployment-ready.json'


def mono_audio(data):
    """Preserve timing and avoid cancelling audible antiphase stereo channels."""
    data = np.asarray(data, dtype=np.float32)
    if not len(data):return np.empty(0,dtype=np.float32),'empty'
    if data.ndim == 1:return data, 'mono'
    mean = data.mean(axis=1)
    powers = np.mean(data.astype(np.float64)**2, axis=0)
    if float(np.mean(mean.astype(np.float64)**2)) < float(powers.max(initial=0))*0.05:
        return data[:, int(np.argmax(powers))], 'strongest-channel-antiphase'
    return mean, 'channel-mean'


def robust_fit(beat_samples, sample_rate, tolerance_ms=25):
    """Fit multiple integer beats using Huber IRLS, preserving missing beat indices.

    This diagnoses a constant-tempo hypothesis, never certifies musical beat level.
    """
    beats = np.asarray(beat_samples, dtype=np.float64)
    if len(beats)<4:return {'eligible':False,'reason':'insufficient_beats'}
    gaps = np.diff(beats)
    period = float(np.median(gaps))
    if period <= 0:return {'eligible':False,'reason':'invalid_intervals'}
    indices = np.r_[0, np.cumsum(np.maximum(1, np.rint(gaps/period)).astype(int))]
    x = np.column_stack((np.ones(len(indices)),indices))
    weights = np.ones(len(beats))
    for _ in range(12):
        fit = np.linalg.lstsq(x*weights[:,None]**.5, beats*weights**.5, rcond=None)[0]
        residual = beats-x@fit
        scale=max(sample_rate*.002,1.4826*float(np.median(np.abs(residual-np.median(residual)))))
        weights=np.minimum(1,1.345*scale/np.maximum(np.abs(residual),1))
    residual_ms=(beats-x@fit)*1000/sample_rate
    p95=float(np.quantile(np.abs(residual_ms),.95))
    return {'eligible':bool(fit[1]>0 and p95<=tolerance_ms), 'bpm':float(60*sample_rate/fit[1]),
            'period_samples':float(fit[1]),'phase_sample':int(round(fit[0])),
            'p95_residual_ms':p95,'beat_count':len(beats),'indices':indices.tolist(),
            'residuals_ms':residual_ms.tolist(),'inlier_fraction':float(np.mean(np.abs(residual_ms)<=tolerance_ms))}


def segmented_fit(beat_samples, sample_rate, window_seconds=None):
    """Residual-driven change-point fit, never fixed-window tempo redlines.

    Adjacent regimes share their transition beat. At least eight beats support
    each fit; an extra regime must explain a material residual reduction and a
    meaningful period change. All observations remain available to the caller.
    """
    beats=np.asarray(beat_samples,dtype=np.int64)
    minimum=8
    def partition(values,depth=0):
        fit=robust_fit(values,sample_rate)
        if len(values)<minimum*2 or fit.get('eligible') or depth>=8:
            return [{'range':[int(values[0]),int(values[-1])+1], 'beat_samples':values.tolist(),**fit}] if len(values) else []
        original=float(np.mean(np.abs(fit.get('residuals_ms',[0]))));best=None
        # Median gap contrast cheaply screens candidates before robust fitting.
        gaps=np.diff(values)
        for index in range(minimum-1,len(values)-minimum+1):
            before=float(np.median(gaps[max(0,index-8):index]));after=float(np.median(gaps[index:index+8]))
            if abs(after-before)/max(before,after,1)<.015:continue
            left=robust_fit(values[:index+1],sample_rate);right=robust_fit(values[index:],sample_rate)
            score=(sum(abs(x) for x in left.get('residuals_ms',[]))+sum(abs(x) for x in right.get('residuals_ms',[])))/(len(values)+1)
            if original-score<5 or score>original*.6:continue
            if best is None or score<best[0]:best=(score,index)
        if best is None:return [{'range':[int(values[0]),int(values[-1])+1],'beat_samples':values.tolist(),**fit}]
        return partition(values[:best[1]+1],depth+1)+partition(values[best[1]:],depth+1)
    return partition(beats)


def fitted_prediction(raw,sample_rate):
    """Experimental grid projection with raw observations preserved for audit."""
    import copy
    result=copy.deepcopy(raw);fit=robust_fit(raw.get('beat_samples',[]),sample_rate)
    result['observed_beat_samples']=raw.get('beat_samples',[])[:]
    result['observed_downbeat_samples']=raw.get('downbeat_samples',[])[:]
    result['fit']=fit
    if fit.get('eligible'):
        observed=np.asarray(raw['beat_samples']);indices=np.asarray(fit['indices'])
        prediction=np.rint(fit['phase_sample']+indices*fit['period_samples']).astype(int)
        result['beat_samples']=prediction.tolist()
        result['downbeat_samples']=[int(prediction[np.argmin(np.abs(observed-down))]) for down in raw.get('downbeat_samples',[])]
    result.setdefault('provenance',{})['projection']='multi-beat-huber-constant-only' if fit.get('eligible') else 'rejected_keep_observations'
    return result


def librosa_beats(data, sample_rate):
    import librosa
    mono,mode=mono_audio(data)
    if not len(mono) or np.all(mono==0):
        return {'beat_samples':[],'downbeat_samples':[],'provenance':{'adapter':'librosa','mono_policy':mode},'reasons':['silence']}
    y=librosa.resample(mono,orig_sr=sample_rate,target_sr=22050)
    onset=librosa.onset.onset_strength(y=y,sr=22050,hop_length=256)
    bpm,frames=librosa.beat.beat_track(onset_envelope=onset,sr=22050,hop_length=256,trim=False)
    beats=np.rint(librosa.frames_to_time(frames,sr=22050,hop_length=256)*sample_rate).astype(int)
    return {'beat_samples':beats[beats<len(data)].tolist(),'downbeat_samples':[],
            'provenance':{'adapter':'librosa','version':librosa.__version__,'hop_length':256,'analysis_sr':22050,'mono_policy':mode},
            'estimated_bpm':float(np.asarray(bpm).reshape(-1)[0]),'reasons':['beat_level_unverified','no_downbeat_detector']}


def beat_this(audio_path, sample_rate, checkpoints=('final0',), progress=None, effective_end_sample=None):
    """Run one bounded child process under the shared application's GPU lease."""
    from .resident import external_gpu, atomic
    from .workflow_log import stage, file_identity, event, current_context
    if not PYTHON.is_file() or not DEPLOYMENT.is_file():raise RuntimeError('Beat This 隔离环境或完整权重尚未部署')
    directory=ROOT/'cache/beat-analysis-runs'/uuid.uuid4().hex
    directory.mkdir(parents=True)
    request={'audio':str(Path(audio_path).resolve()),'sample_rate':sample_rate,'checkpoints':list(checkpoints),
             'output':str(directory/'result.json'),'effective_end_sample':effective_end_sample,
             'workflow_trace':current_context()}
    atomic(directory/'request.json',request)
    wait_started=time.perf_counter()
    with stage('music.beat_this_gpu_analysis', audio=file_identity(audio_path), sample_rate=sample_rate,
               checkpoints=checkpoints, deployment=file_identity(DEPLOYMENT,hash_file=True),
               isolated_python=PYTHON, worker=ROOT/'tools'/'beat_analysis_worker.py'):
        with external_gpu(progress):
            queue_seconds=time.perf_counter()-wait_started
            worker_started=time.perf_counter()
            with (directory/'worker.log').open('w',encoding='utf-8') as log:
                try:
                    subprocess.run([str(PYTHON),'-u',str(ROOT/'tools/beat_analysis_worker.py'),str(directory/'request.json')],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=1800,check=True)
                except subprocess.CalledProcessError as exc:
                    log.flush()
                    detail=(directory/'worker.log').read_text(encoding='utf-8',errors='replace').splitlines()
                    event('child_process_failure','music.beat_this_gpu_analysis',returncode=exc.returncode,
                          worker_log=directory/'worker.log',log_tail=detail[-500:])
                    raise RuntimeError('Beat This 推理失败：'+(detail[-1] if detail else str(exc))+'；日志 '+str(directory/'worker.log')) from exc
            worker_seconds=time.perf_counter()-worker_started
    result=json.loads((directory/'result.json').read_text(encoding='utf-8'))
    result.setdefault('cost',{}).update(queue_wait_seconds=queue_seconds,worker_process_seconds=worker_seconds)
    event('model_result','music.beat_this_gpu_analysis',provenance=result.get('provenance'),cost=result.get('cost'),
          beat_count=len(result.get('beat_samples',[])),downbeat_count=len(result.get('downbeat_samples',[])),
          worker_log=file_identity(directory/'worker.log'))
    return result


def v32_timing(audio_path, sample_rate, super_timing=False, progress=None, seed=20261005, effective_end_sample=None):
    from .resident import call, atomic
    directory=ROOT/'cache/timing-v32-runs'/uuid.uuid4().hex
    directory.mkdir(parents=True)
    request={'action':'timing_only','audio':str(Path(audio_path).resolve()),'output':str(directory),
             'sample_rate':sample_rate,'seed':seed,'super_timing':super_timing,'timer_iterations':20,'effective_end_sample':effective_end_sample}
    atomic(directory/'request.json',request)
    return call('v32',{'request_path':str(directory/'request.json')},progress)
