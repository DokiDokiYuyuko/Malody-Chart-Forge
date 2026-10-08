"""Bounded offline Beat This inference child; caller owns the shared GPU lease."""
from pathlib import Path
import hashlib
import json
import sys
import time
import uuid
import numpy as np
import soundfile as sf
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def _main_impl(request_path):
    request=json.loads(Path(request_path).read_text())
    if request.get('action')=='public_evaluation':return public_evaluation(request)
    adapter_name='beat_this_ensemble' if len(request['checkpoints'])>1 else 'beat_this_'+request['checkpoints'][0]
    import torch
    from beat_this.inference import Audio2Frames, Spect2Frames
    from beat_this.model.postprocessor import Postprocessor
    from malody_studio.workflow_log import stage, event
    from malody_studio.beat_analysis import mono_audio
    from malody_studio.resident import atomic
    if not torch.cuda.is_available():raise RuntimeError('Beat This requires CUDA; CPU fallback is disabled')
    torch.set_num_threads(4);torch.cuda.reset_peak_memory_stats()
    started=time.perf_counter();load_seconds=0;predictions=[];records=[];individual=[]
    if request.get('spectrogram'):
        spect=torch.from_numpy(np.load(request['spectrogram'])).float().to('cuda');sr=request['sample_rate'];mode='author-spectrogram'
    else:
        audio,sr=sf.read(request['audio'],dtype='float32',always_2d=True)
        if request.get('effective_end_sample') is not None:audio=audio[:request['effective_end_sample']]
        audio,mode=mono_audio(audio)
        if sr!=request['sample_rate']:raise ValueError('Audio sample rate mismatch')
        if not len(audio) or np.all(audio==0):
            result={'beat_samples':[],'downbeat_samples':[],'reasons':['silence'],
                    'provenance':{'adapter':adapter_name,'inference_skipped':'exact_zero_source','analysis_range':[0,request.get('effective_end_sample')],'mono_policy':mode},
                    'cost':{'total_seconds':time.perf_counter()-started,'load_seconds':0,'peak_allocated_vram_mb':0}}
            atomic(Path(request['output']),result);return
    for name in request['checkpoints']:
        path=ROOT/'models/beat-this'/(name+'.ckpt')
        with path.open('rb') as stream:checksum=hashlib.file_digest(stream,'sha256').hexdigest()
        deployment=json.loads((ROOT/'models/beat-this/deployment-ready.json').read_text())
        expected=next((x['sha256'] for x in deployment['checkpoints'] if x['name']==name),None)
        if checksum!=expected:raise ValueError('Published Beat This checkpoint checksum changed')
        loaded=time.perf_counter()
        with stage('beat_this.load_checkpoint',checkpoint=name,checkpoint_path=path,checkpoint_sha256=checksum,
                   device='cuda',float16=False):
            adapter=(Spect2Frames if request.get('spectrogram') else Audio2Frames)(checkpoint_path=str(path),device='cuda',float16=False)
        load_seconds+=time.perf_counter()-loaded
        with stage('beat_this.run_checkpoint',checkpoint=name,device='cuda',sample_rate=sr,
                   audio_samples=len(audio) if not request.get('spectrogram') else None,
                   spectrogram_shape=list(spect.shape) if request.get('spectrogram') else None):
            logits=adapter(spect) if request.get('spectrogram') else adapter(audio,sr)
        predictions.append(tuple(x.detach().cpu() for x in logits))
        with stage('beat_this.decode_checkpoint',checkpoint=name,decoder='minimal'):
            one_beat,one_down=Postprocessor(type='minimal')(*predictions[-1])
        individual.append({'checkpoint':name,'beat_samples':np.rint(np.asarray(one_beat)*sr).astype(int).tolist(),
                           'downbeat_samples':np.rint(np.asarray(one_down)*sr).astype(int).tolist()})
        records.append({'name':name,'sha256':checksum});del adapter;torch.cuda.empty_cache()
    # Mean logits is an experimental aggregation, not a calibrated probability.
    beat=torch.stack([x[0] for x in predictions]).mean(0)
    downbeat=torch.stack([x[1] for x in predictions]).mean(0)
    with stage('beat_this.decode_aggregated_logits',checkpoints=request['checkpoints'],
               aggregation='mean_logits',decoder='minimal'):
        beats,downbeats=Postprocessor(type='minimal')(beat,downbeat)
    result={'beat_samples':np.rint(np.asarray(beats)*sr).astype(int).tolist(),
            'downbeat_samples':np.rint(np.asarray(downbeats)*sr).astype(int).tolist(),
            'provenance':{'adapter':adapter_name,'checkpoints':records,
                          'aggregation':'mean_logits','decoder':'minimal','precision':'fp32','mono_policy':mode,'analysis_range':[0,request.get('effective_end_sample')], 'training_overlap':'GTZAN excluded by author; local recordings unknown'},
            'individual_predictions':individual,'cost':{'total_seconds':time.perf_counter()-started,'load_seconds':load_seconds,
                    'peak_allocated_vram_mb':torch.cuda.max_memory_allocated()/1024**2},'reasons':['beat_level_unverified']}
    atomic(Path(request['output']),result)
    np.savez_compressed(Path(request['output']).with_suffix('.logits.npz'),beat=beat.numpy(),downbeat=downbeat.numpy())
    event('beat_this_result','beat_this.write_result',beat_count=len(result['beat_samples']),
          downbeat_count=len(result['downbeat_samples']),cost=result.get('cost'),provenance=result.get('provenance'))


def public_evaluation(request):
    import torch
    import mir_eval
    from beat_this.inference import Spect2Frames
    from beat_this.model.postprocessor import Postprocessor
    from malody_studio.resident import atomic
    from tools.run_timing_experiments import detection_metrics
    if not torch.cuda.is_available():raise RuntimeError('Public evaluation requires CUDA')
    torch.set_num_threads(4)
    package=ROOT/'models/beat-this';decode=Postprocessor(type='minimal')
    adapters=[Spect2Frames(str(package/f'final{i}.ckpt'),device='cuda',float16=False) for i in range(3)]
    bundle=np.load(request['bundle']);output=Path(request['output']);output.mkdir(parents=True,exist_ok=True)
    records=[]
    frozen=json.loads((output/'frozen-public-protocol.json').read_text())
    for item in frozen['items']:
        name=item['name'];saved=output/(name+'.json')
        if saved.is_file():
            previous=json.loads(saved.read_text())
            if previous['status']=='completed':records.append(previous);continue
            attempts=output/'attempts';attempts.mkdir(exist_ok=True)
            atomic(attempts/(name+'-'+uuid.uuid4().hex+'.json'),previous)
        started=time.perf_counter();torch.cuda.reset_peak_memory_stats()
        row={'name':name,'split':item['split'],'label_hash':item['label_hash'],'status':'running','started':time.time()}
        atomic(saved,row)
        try:
            spect=torch.from_numpy(bundle[name+'/track'].astype(np.float32)).to('cuda')
            labels=np.loadtxt(item['labels'],ndmin=2) if item['labels'] else np.empty((0,2));reference=labels[:,0]
            ref_down=reference[labels[:,1]==1] if labels.shape[1]>1 else None
            row['downbeat_labels_available']=ref_down is not None
            frames=[adapter(spect) for adapter in adapters]
            predictions=frames+[(torch.stack([x[0] for x in frames]).mean(0),torch.stack([x[1] for x in frames]).mean(0))]
            row['methods']={}
            for method,prediction in zip(('final0','final1','final2','ensemble_mean_logits'),predictions):
                beats,down=decode(*prediction)
                scores={'beat':detection_metrics(reference,beats,1),'downbeat':detection_metrics(ref_down,down,1) if ref_down is not None else None}
                # Author-standard metrics retain metrical-level alternatives as
                # separate CML/AML diagnostics rather than hiding octave errors.
                standard=mir_eval.beat.evaluate(reference,np.asarray(beats)) if len(reference)>1 and len(beats)>1 else {}
                row['methods'][method]={'beat_seconds':np.asarray(beats).tolist(),'downbeat_seconds':np.asarray(down).tolist(),
                                        'metrics':scores,'mir_eval':standard}
            row.update(status='completed',cost={'total_seconds':time.perf_counter()-started,
                       'peak_allocated_vram_mb':torch.cuda.max_memory_allocated()/1024**2})
        except Exception as exc:row.update(status='failed',error=str(exc),error_type=type(exc).__name__)
        row['finished']=time.time();atomic(saved,row);records.append(row)
        print(json.dumps({'gtzan':name,'status':row['status'],'progress':len(records),'total':len(frozen['items'])}),flush=True)
    summary={'dataset':'GTZAN author spectrograms','training_overlap':{'BeatThis final0/1/2':'excluded by author','V32':'unknown'},
             'v32_status':'not_run_original_audio_unavailable','a0_a1_status':'not_run_original_audio_unavailable',
             'count':len(records),'failed':sum(x['status']=='failed' for x in records),'splits':{}}
    latest=json.loads((output/'frozen-public-protocol.json').read_text());groups={x['name']:x for x in latest['items']}
    summary['decision_protocol']=latest['schema'];summary['protocol_hash']=hashlib.sha256((output/'frozen-public-protocol.json').read_bytes()).hexdigest()
    for split in ('development','holdout'):
        summary['splits'][split]={}
        subset=[x for x in records if groups[x['name']]['split']==split and groups[x['name']].get('decision_eligible',True) and x['status']=='completed']
        summary['splits'][split]['recording_groups']=len(subset)
        for method in ('final0','final1','final2','ensemble_mean_logits'):
            summary['splits'][split][method]={}
            for event in ('beat','downbeat'):
                values=[x['methods'][method]['metrics'][event] for x in subset if x['methods'][method]['metrics'][event] is not None]
                summary['splits'][split][method][event]={key:float(np.mean([v[key] for v in values if v[key] is not None])) if any(v[key] is not None for v in values) else None for key in ('recall','precision','f1','p95_ms')}
            summary['splits'][split][method]['half_or_double_tracks']=sum(x['methods'][method]['metrics']['beat']['half_or_double'] for x in subset)
    summary['unavailable_downbeat_annotations']=sum(x.get('downbeat_labels_available') is False for x in records)
    atomic(output/'summary.json',summary)

def main(request_path):
    from malody_studio.workflow_log import attach_external, current_context, stage
    path=Path(request_path)
    request=json.loads(path.read_text(encoding='utf-8'))
    context=request.get('workflow_trace')
    attached=current_context()
    if attached and context and attached.get('path')==context.get('path') and attached.get('run_id')==context.get('run_id'):
        with stage('beat_this.worker_request',request_path=path,checkpoints=request.get('checkpoints'),audio=request.get('audio')):
            return _main_impl(path)
    with attach_external(context,'beat_this_cuda_worker'):
        with stage('beat_this.worker_request',request_path=path,checkpoints=request.get('checkpoints'),audio=request.get('audio')):
            return _main_impl(path)


if __name__=='__main__':main(sys.argv[1])
