"""Frozen, resumable beat/timing comparisons; failed attempts are immutable data."""
from __future__ import annotations
import argparse
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
import numpy as np
import soundfile as sf
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from malody_studio import beat_analysis as ba
from malody_studio.music_timing import source_contract,timing_map,digest
from malody_studio.resident import atomic,external_gpu
HOME=ROOT/'work/timing-experiments'
METHODS=('A0','A1','B0','B1','C0','C1','D0','D1')
COMPARISON_REVISION='timing-comparison-2-99pct'

def freeze():
    path=HOME/'frozen-protocol.json'
    if path.is_file():return json.loads(path.read_text(encoding='utf-8'))
    HOME.mkdir(parents=True,exist_ok=True);unique={}
    for directory in sorted((ROOT/'outputs/advanced').iterdir()):
        project_path=directory/'project.json'
        if not project_path.is_file():continue
        project=json.loads(project_path.read_text(encoding='utf-8'))
        if project['samples']<44100*5 or project.get('title','').startswith('分离制谱对照'):continue
        data,source=source_contract(directory,project);key=source['pcm_sha256']
        if key in unique:continue
        unique[key]={'project_id':project['id'],'project_revision':project['revision'],'title':project['title'],
                     'directory':str(directory),'audio':str(directory/'source.wav'),'source':source,
                     'split':'holdout' if int(key[:8],16)%3==0 else 'development','labels':None}
    code_paths=['malody_studio/audio.py','malody_studio/adaptive_difficulty.py','malody_studio/section_plan.py','malody_studio/music_timing.py','malody_studio/beat_analysis.py',
                'tools/mapperatorinator_worker.py','tools/run_timing_experiments.py','tools/beat_analysis_worker.py','tools/setup_beat_analysis.py',
                'requirements-lock.txt','runtime/mapperatorinator-requirements-lock.txt']
    baseline={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in code_paths if (ROOT/p).is_file()}
    protocol={'schema':'timing-experiment-protocol-v1','created':time.time(),'items':list(unique.values()),
        'methods':list(METHODS),'split_rule':'PCM sha256 first 32 bits modulo 3; same recording never crosses sets',
        'selection_priority':['false_acceptance_at_matched_coverage','octave_errors','loss_of_lock','p95_and_drift','continuity','cost'],
        'controlled_gate':{'p95_ms':25,'extra_drift_over_30_seconds_ms':20,'minimum_recall':.99},
        'real_label_policy':'No accuracy claims on unlabelled local audio; V32 training overlap unknown',
        'baseline':baseline,'public':'GTZAN author annotations v1.0 and author spectrograms; final0-2 exclude GTZAN',
        'super':{'iterations':20,'seed':20261005,'super_timing_fast_loop':True,'precision':'bf16'},
        'adoption':'No selection accepted until public and controlled evidence independently reviewed'}
    atomic(path,protocol)
    return protocol

def detection_metrics(reference,prediction,sr,tolerance_ms=70):
    """Maximum-cardinality monotone one-to-one matching; missed beats stay visible."""
    ref=np.asarray(reference,dtype=float);pred=np.asarray(prediction,dtype=float)
    i=j=0;matches=[];tolerance=tolerance_ms*sr/1000
    while i<len(ref) and j<len(pred):
        delta=pred[j]-ref[i]
        if abs(delta)<=tolerance:matches.append((i,j,delta));i+=1;j+=1
        elif delta < -tolerance:j+=1
        else:i+=1
    count=len(matches);recall=count/len(ref) if len(ref) else float(not len(pred))
    precision=count/len(pred) if len(pred) else float(not len(ref))
    errors=np.asarray([x[2] for x in matches])*1000/sr
    nearest_errors=np.asarray([float(pred[np.argmin(np.abs(pred-point))]-point)*1000/sr for point in ref]) if len(pred) else np.array([])
    all_distances=np.abs(nearest_errors)
    drift=None
    if count>=3:
        seconds=np.asarray([ref[x[0]] for x in matches])/sr
        drift=float(np.polyfit(seconds,errors,1)[0]*30) if np.ptp(seconds)>0 else None
    ratio=float(np.median(np.diff(ref))/np.median(np.diff(pred))) if len(ref)>2 and len(pred)>2 else None
    return {'recall':recall,'precision':precision,'f1':2*recall*precision/max(recall+precision,1e-12),
            'matched':count,'missed':len(ref)-count,'extra':len(pred)-count,
            'p95_ms':float(np.quantile(all_distances,.95)) if len(all_distances) else None,
            'matched_p95_ms':float(np.quantile(np.abs(errors),.95)) if count else None,
            'nearest_median_absolute_ms':float(np.median(all_distances)) if len(all_distances) else None,
            'nearest_signed_offset_ms':float(np.median(nearest_errors)) if len(nearest_errors) else None,
            'nearest_drift_over_30s_ms':float(np.polyfit(ref/sr,nearest_errors,1)[0]*30) if len(nearest_errors)>=3 and np.ptp(ref)>0 else None,
            'p95_population':'every_reference_beat_nearest_prediction; no_predictions_unbounded_reported_null',
            'signed_offset_ms':float(np.median(errors)) if count else None,'drift_over_30s_ms':drift,
            'predicted_beat_rate_ratio':ratio,'half_or_double':bool(ratio is not None and (abs(ratio-.5)<.05 or abs(ratio-2)<.1))}

def synthetic_items():
    directory=HOME/'synthetic';directory.mkdir(exist_ok=True);sr=44100;items=[]
    specifications=[('offset_120bpm',np.arange(.137,36,.5),'mono'),
                    ('step_120_150',np.r_[np.arange(.137,18,.5),np.arange(18.137,36,.4)],'mono'),
                    ('antiphase_120',np.arange(.137,36,.5),'antiphase'),
                    ('quiet_nonzero',np.arange(.137,36,.5),'quiet'),
                    ('pickup_120',np.arange(.387,36,.5),'pickup'),
                    ('meter3_120',np.arange(.137,36,.5),'meter3'),
                    ('weak_drums',np.arange(.137,36,.5),'weakdrums'),
                    ('middle_silence',np.arange(.137,36,.5),'middle_silence'),
                    ('half_competition',np.arange(.137,36,.5),'half'),
                    ('constant_accent_change',np.arange(.137,36,.5),'accents'),
                    ('zero_tail',np.arange(.137,28,.5),'tail'),
                    ('silence',np.array([]),'mono')]
    for name,seconds,mode in specifications:
        audio=np.zeros(sr*36,np.float32);samples=np.rint(seconds*sr).astype(int)
        pulse=np.exp(-np.arange(round(sr*.045))/(sr*.006))*np.sin(np.arange(round(sr*.045))*2*np.pi*1300/sr)
        for index,sample in enumerate(samples):
            gain=.15 if mode=='pickup' and index<2 else .08 if mode=='half' and index%2 else .3 if mode=='accents' and index>35 else 1.
            audio[sample:sample+len(pulse)]+=gain*pulse[:len(audio)-sample].astype(np.float32)
        if mode=='weakdrums':audio=audio*.02+.04*np.sin(np.arange(len(audio))*2*np.pi*220/sr).astype(np.float32)
        keep=np.ones(len(samples),bool)
        if mode=='middle_silence':audio[sr*12:sr*16]=0;keep=(samples<sr*12)|(samples>=sr*16)
        if mode=='quiet':audio*=1e-8
        if mode=='antiphase':audio=np.column_stack((audio,-audio))
        path=directory/(name+'.wav')
        if not path.is_file():sf.write(path,audio,sr,subtype='FLOAT')
        full=audio[:,None] if audio.ndim==1 else audio;active=np.flatnonzero(np.any(full!=0,axis=1))
        last=int(active[-1])+1 if len(active) else 0;end=last if len(audio)-last>=sr else len(audio)
        source={'pcm_sha256':hashlib.sha256(full.astype('<f4').tobytes()).hexdigest(),'sample_rate':sr,'samples':len(audio),'channels':full.shape[1],'effective_end_sample':end}
        meter=3 if mode=='meter3' else 4;down=samples[np.arange(len(samples))%meter==(2 if mode=='pickup' else 0)]
        if mode=='middle_silence':down=down[(down<sr*12)|(down>=sr*16)]
        items.append({'id':name,'audio':str(path),'source':source,'split':'controlled','labels':{'beat_samples':samples[keep].tolist(),'downbeat_samples':down.tolist()},'meter':meter,'ambiguous_beat_level':mode=='half','label_note':'Deterministic pulse clock; tests preprocessing/engineering, not natural music accuracy'})
    atomic(directory/'ground-truth.json',items);return items

def run_method(method,item):
    source=item['source'];sr=source['sample_rate'];data,_=sf.read(item['audio'],dtype='float32',always_2d=True)
    if hashlib.sha256(data.astype('<f4').tobytes()).hexdigest()!=source['pcm_sha256'] or len(data)!=source['samples']:
        raise ValueError('Frozen experimental PCM input changed')
    data=data[:source['effective_end_sample']]
    if method in ('A0','A1'):
        raw=ba.librosa_beats(data,sr)
        if method=='A1':raw=ba.fitted_prediction(raw,sr)
    elif method in ('B0','B1','D0','D1'):
        raw=ba.beat_this(item['audio'],sr,('final0',) if method=='B0' else ('final0','final1','final2'),effective_end_sample=source['effective_end_sample'])
        if method in ('D0','D1'):
            raw=ba.fitted_prediction(raw,sr)
            if method=='D1':
                check=ba.v32_timing(item['audio'],sr,False,effective_end_sample=source['effective_end_sample'])
                raw['independent_review']=check
                raw['agreement']=detection_metrics(raw['beat_samples'],check['beat_samples'],sr,25)
                if raw['agreement']['f1']<.9:raw['reasons'].append('independent_v32_disagreement')
    elif method in ('C0','C1'):raw=ba.v32_timing(item['audio'],sr,method=='C1',effective_end_sample=source['effective_end_sample'])
    else:raise ValueError(method)
    timing=timing_map(raw,source)
    if 'independent_v32_disagreement' in raw.get('reasons',[]):
        timing['eligibility']['v32']=False
        from malody_studio.music_timing import sealed
        timing['uncertain']=True;timing=sealed(timing)
    return raw,timing

def existing_success(item_id,method):
    for path in (HOME/'runs').glob('*.json'):
        row=json.loads(path.read_text(encoding='utf-8'))
        if row.get('item_id')==item_id and row.get('method')==method and row['status']=='completed' and row.get('comparison_revision')==COMPARISON_REVISION:return True
    return False

def run(items,methods):
    (HOME/'runs').mkdir(parents=True,exist_ok=True)
    for item in items:
        identity=item.get('id',item['source']['pcm_sha256'])
        for method in methods:
            if existing_success(identity,method):continue
            row={'run_id':uuid.uuid4().hex,'item_id':identity,'source':item['source'],'split':item['split'],'method':method,
                 'started':time.time(),'status':'running','ground_truth':bool(item.get('labels')),'comparison_revision':COMPARISON_REVISION}
            row['implementation_hash']=hashlib.sha256(Path(__file__).read_bytes()+Path(ba.__file__).read_bytes()).hexdigest()
            path=HOME/'runs'/(row['run_id']+'.json');atomic(path,row);started=time.perf_counter()
            try:
                raw,timing=run_method(method,item);row.update(raw=raw,timing=timing,status='completed')
                if item.get('labels'):
                    row['metrics']={key:detection_metrics(item['labels'][key],raw[key],item['source']['sample_rate']) for key in ('beat_samples','downbeat_samples')}
                    m=row['metrics']['beat_samples']
                    row['controlled_gate']=bool(m['recall']>=.99 and m['precision']>=.99 and m['p95_ms'] is not None and m['p95_ms']<=25 and m['drift_over_30s_ms'] is not None and abs(m['drift_over_30s_ms'])<=20 and not m['half_or_double'])
            except Exception as exc:row.update(status='failed',error_type=type(exc).__name__,error=str(exc))
            row.update(elapsed_seconds=time.perf_counter()-started,finished=time.time());atomic(path,row)
            print(json.dumps({'item':identity,'method':method,'status':row['status'],'seconds':row['elapsed_seconds'],'error':row.get('error')},ensure_ascii=False),flush=True)
    summarize()

def summarize():
    rows=[json.loads(x.read_text(encoding='utf-8')) for x in (HOME/'runs').glob('*.json')]
    output={'schema':'timing-experiment-summary-v1','created':time.time(),'run_count':len(rows),'methods':{}}
    for method in METHODS:
        records=[r for r in rows if r['method']==method and r.get('comparison_revision')==COMPARISON_REVISION]
        completed=[r for r in records if r['status']=='completed']
        output['methods'][method]={'attempts':len(records),'completed':len(completed),'failed':len([r for r in records if r['status']=='failed']),
            'coverage':sum(r['timing']['eligibility']['v32'] for r in completed)/len(records) if records else None,
            'elapsed_seconds':sum(r.get('elapsed_seconds',0) for r in records),
            'metrics_claim':'Local unlabelled recordings: no accuracy measured',
            'controlled_pass':sum(r.get('controlled_gate',False) for r in completed),
            'labelled_runs':sum(r['ground_truth'] for r in completed)}
    atomic(HOME/'summary.json',output)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--freeze',action='store_true');parser.add_argument('--freeze-public',action='store_true');parser.add_argument('--audit-public',action='store_true');parser.add_argument('--local',action='store_true');parser.add_argument('--synthetic',action='store_true');parser.add_argument('--public',action='store_true');parser.add_argument('--methods',nargs='+',choices=METHODS,default=list(METHODS));parser.add_argument('--limit',type=int)
    parser.add_argument('--audit-controlled',action='store_true')
    parser.add_argument('--audit-selected',action='store_true')
    parser.add_argument('--downstream-ab',action='store_true')
    args=parser.parse_args();protocol=freeze();items=[]
    if args.synthetic:items.extend(synthetic_items())
    if args.local:items.extend(protocol['items'])
    if args.freeze_public:freeze_public()
    if args.public:run_public()
    if args.audit_public:audit_public()
    if args.audit_selected:audit_selected()
    if args.downstream_ab:downstream_ab()
    if args.audit_controlled:audit_controlled()
    run(items[:args.limit] if args.limit else items,args.methods)
    print(json.dumps({'unique_original_recordings':len(protocol['items']),'frozen_protocol':str(HOME/'frozen-protocol.json')}))

def freeze_public():
    annotations=ROOT/'data/beat-this-annotations-v1/gtzan/annotations/beats'
    output=HOME/'public-gtzan';output.mkdir(parents=True,exist_ok=True)
    frozen=output/'frozen-public-protocol.json'
    bundle=ROOT/'data/beat-this-public/gtzan.npz'
    if not frozen.is_file() or not json.loads(frozen.read_text()).get('song_group_metadata'):
        if frozen.is_file():
            backup=output/'frozen-public-before-song-groups.json'
            if not backup.is_file():backup.write_bytes(frozen.read_bytes())
        metadata=ROOT/'data/gtzan-recording-metadata';index={};group_members={}
        for line in (metadata/'index.txt').read_text(encoding='utf-8').splitlines():
            fields=[x.strip() for x in line.split(':::')]
            if len(fields)<3 or fields[0].startswith('#'):continue
            name='gtzan_'+fields[0].removesuffix('.wav').replace('.','_')
            known=bool(fields[1] and fields[2] and '?' not in fields[1]+fields[2] and not any(x in (fields[1]+' '+fields[2]).lower() for x in ('unknown','unidentified')))
            group=(fields[1].casefold()+' / '+fields[2].casefold()) if known else None
            index[name]={'artist':fields[1],'song':fields[2],'group':group}
        partitions={}
        for partition in ('train_filtered','valid_filtered','test_filtered'):
            for line in (metadata/(partition+'.txt')).read_text().splitlines():
                name='gtzan_'+Path(line).name.removesuffix('.wav').replace('.','_');partitions[name]='holdout' if partition=='test_filtered' else 'development'
        bundle_names=[x.removesuffix('/track') for x in np.load(bundle).files]
        for name in bundle_names:
            group=index.get(name,{}).get('group')
            if group and name in partitions:group_members.setdefault(group,[]).append(name)
        group_split={group:'holdout' if any(partitions[name]=='holdout' for name in names) else 'development' for group,names in group_members.items()}
        items=[]
        for name in sorted(bundle_names):
            path=annotations/(name+'.beats');checksum=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            group=index.get(name,{}).get('group');included=bool(group and name in partitions and path.is_file())
            representative=included and name==min(group_members[group])
            items.append({'name':name,'labels':str(path) if path.is_file() else None,'label_hash':checksum,
                          'split':group_split[group] if included else 'excluded','song_group':group,
                          'decision_eligible':representative,'recording_metadata':index.get(name,{})})
        with bundle.open('rb') as stream:checksum=hashlib.file_digest(stream,'sha256').hexdigest()
        metadata_hash={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [metadata/'index.txt',metadata/'train_filtered.txt',metadata/'valid_filtered.txt',metadata/'test_filtered.txt']}
        atomic(frozen,{'schema':'frozen-public-timing-v2','items':items,'bundle_sha256':checksum,
            'annotation_revision':'c3c47fd37d3074d9f8119f18bbf460f909609f22','spectrogram_source':'https://zenodo.org/records/13922116',
            'config':{'models':['final0','final1','final2'],'aggregation':'mean_logits','decoder':'minimal','precision':'fp32'},
            'song_group_metadata':{'repository':'https://github.com/boblsturm/GTZAN','revision':'fedc781026cf1a20034830f8071c98d93a422016','file_sha256':metadata_hash,
                                   'split_rule':'author fault-filtered partitions; same artist/song entirely holdout if any test clip, otherwise development; one representative per group; unknown identity excluded from selection'},
            'revision_reason':'Original-song grouping fixed before observing aggregate scores; all 1000 predictions retained',
            'limitations':'Spectrogram-only evaluation; local audio and V32 unknown training overlap'})
    return output,bundle

def run_public():
    output,bundle=freeze_public()
    atomic(output/'request.json',{'action':'public_evaluation','bundle':str(bundle),'output':str(output)})
    with external_gpu():
        with (output/'worker.log').open('a',encoding='utf-8') as log:
            subprocess.run([str(ba.PYTHON),'-u',str(ROOT/'tools/beat_analysis_worker.py'),str(output/'request.json')],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=7200)


def grid_predictions(timing,stop_sample):
    sr=timing['source']['sample_rate'];beats=[];down=[];points=timing['tempo_points']
    for i,point in enumerate(points):
        start=0 if i==0 else point['sample'];end=points[i+1]['sample'] if i+1<len(points) else stop_sample
        period=60*sr/point['bpm'];anchor=point['sample']
        first=int(np.ceil((start-anchor)/period));last=int(np.ceil((end-anchor)/period))
        for n in range(first,last):
            sample=round(anchor+n*period)
            if 0<=sample<stop_sample:
                beats.append(sample)
                if n%point['meter']==0:down.append(sample)
    return sorted(set(beats)),sorted(set(down))


def audit_public():
    """Score actual proposed grids against labels; no model-consensus ground truth."""
    output=HOME/'public-gtzan';frozen=json.loads((output/'frozen-public-protocol.json').read_text())
    scores=[];methods=('final0','final1','final2','ensemble_mean_logits');sr=22050
    unavailable_downbeat=[]
    for item in frozen['items']:
        if not item.get('decision_eligible') or not item['labels']:continue
        row=json.loads((output/(item['name']+'.json')).read_text())
        if row['status']!='completed':continue
        labels=np.loadtxt(item['labels'],ndmin=2);ref=np.rint(labels[:,0]*sr).astype(int)
        if labels.shape[1]<2:
            unavailable_downbeat.append(item['name']);continue  # beat-only labels cannot certify meter/phase
        ref_down=ref[labels[:,1]==1]
        source={'sample_rate':sr,'samples':round(30.02*sr),'effective_end_sample':round(30.02*sr)}
        for method in methods:
            prediction=row['methods'][method]
            raw={'beat_samples':np.rint(np.asarray(prediction['beat_seconds'])*sr).astype(int).tolist(),
                 'downbeat_samples':np.rint(np.asarray(prediction['downbeat_seconds'])*sr).astype(int).tolist(),
                 'provenance':{'adapter':method,'input':'author_spectrogram'}}
            timing=timing_map(raw,source)
            grid,down=grid_predictions(timing,source['samples'])
            # Restrict to the labelled active span; silence/no-annotation spans
            # cannot be treated as musical beat ground truth.
            if len(ref):grid=[x for x in grid if ref[0]-sr*.07<=x<=ref[-1]+sr*.07]
            if len(ref_down):down=[x for x in down if ref_down[0]-sr*.07<=x<=ref_down[-1]+sr*.07]
            beat_metric=detection_metrics(ref,grid,sr);down_metric=detection_metrics(ref_down,down,sr)
            correct=all(m['recall']>=.99 and m['precision']>=.99 and m['p95_ms'] is not None and m['p95_ms']<=25 for m in (beat_metric,down_metric))
            reference_fit=ba.robust_fit(ref,sr)
            down_indices=np.flatnonzero(labels[:,1]==1);bar_lengths=np.diff(down_indices)
            reference_meter=int(np.median(bar_lengths)) if len(bar_lengths)>2 and np.mean(bar_lengths==np.median(bar_lengths))>=.9 else None
            tempo_deviations=[]
            for index,point in enumerate(timing['tempo_points']):
                stop=timing['tempo_points'][index+1]['sample'] if index+1<len(timing['tempo_points']) else source['samples']
                local_ref=ref[(ref>=point['sample'])&(ref<stop)];local_fit=ba.robust_fit(local_ref,sr)
                if len(local_ref)>=8 and local_fit.get('p95_residual_ms',1e6)<=70:
                    tempo_deviations.append(abs(point['bpm']/local_fit['bpm']-1)*100)
            natural={'octave_error':beat_metric['half_or_double'],
                     'meter_error':bool(reference_meter is not None and timing['meter'] is not None and timing['meter']!=reference_meter),
                     'gross_bar_phase_error':bool(down_metric['nearest_median_absolute_ms'] is not None and down_metric['nearest_median_absolute_ms']>70),
                     'tempo_error_over_half_percent':bool(tempo_deviations and max(tempo_deviations)>.5),
                     'maximum_tempo_deviation_percent':max(tempo_deviations) if tempo_deviations else None,
                     'reference_annotation_constant_fit_p95_ms':reference_fit.get('p95_residual_ms'),
                     'phase_median_ms':down_metric['nearest_signed_offset_ms'],'phase_median_absolute_ms':down_metric['nearest_median_absolute_ms'],
                     'phase_drift_over_30s_ms':down_metric['nearest_drift_over_30s_ms'],
                     'reference_meter':reference_meter,
                     'criterion_note':'Natural coarse errors separated from annotation jitter. 99%/25ms remains controlled engineering gate, not natural music correctness certification.'}
            natural_error=any(natural[k] for k in ('octave_error','meter_error','gross_bar_phase_error','tempo_error_over_half_percent'))
            score=max((region.get('p95_residual_ms',1e6) for region in timing['local_fits']),default=1e6)
            scores.append({'name':item['name'],'song_group':item['song_group'],'split':item['split'],'method':method,
                           'eligible':timing['eligibility']['v32'],'grid_correct_at_99pct_25ms':correct,
                           'natural_grid_errors':natural,'natural_coarse_wrong_accept':natural_error,
                           'confidence_proxy_residual_ms':score,'grid_metrics':{'beat':beat_metric,'downbeat':down_metric},
                           'detection_metrics':prediction['metrics'],'timing_id':timing['id']})
    curves={}
    for split in ('development','holdout'):
        curves[split]={}
        for method in methods:
            subset=[x for x in scores if x['split']==split and x['method']==method];eligible=sorted([x for x in subset if x['eligible']],key=lambda x:(x['confidence_proxy_residual_ms'],x['name']))
            curve=[]
            for target in (.01,.05,.1,.25,.5,.75,.9,.95,1):
                accepted=eligible[:int(np.ceil(target*len(subset)))];errors=sum(not x['grid_correct_at_99pct_25ms'] for x in accepted)
                natural_errors=sum(x['natural_coarse_wrong_accept'] for x in accepted)
                curve.append({'target_coverage':target,'coverage':len(accepted)/len(subset) if subset else 0,'accepted':len(accepted),
                              'errors':errors,'risk':errors/len(accepted) if accepted else None,
                              'natural_coarse_errors':natural_errors,'natural_coarse_risk':natural_errors/len(accepted) if accepted else None,
                              'correct_coverage':(len(accepted)-errors)/len(subset) if subset else 0})
            curves[split][method]={'denominator_recording_groups':len(subset),'eligible_maps':len(eligible),'curve':curve}
    paired={};rng=np.random.default_rng(20261005)
    for split in ('development','holdout'):
        left={x['name']:x for x in scores if x['split']==split and x['method']=='final0'}
        right={x['name']:x for x in scores if x['split']==split and x['method']=='ensemble_mean_logits'}
        keys=sorted(set(left)&set(right));differences=np.asarray([right[k]['detection_metrics']['beat']['f1']-left[k]['detection_metrics']['beat']['f1'] for k in keys])
        boot=np.asarray([float(np.mean(differences[rng.integers(0,len(differences),len(differences))])) for _ in range(2000)]) if len(differences) else np.array([])
        paired[split]={'recording_groups':len(keys),'mean_ensemble_minus_final0_f1':float(np.mean(differences)) if len(differences) else None,
                       'paired_bootstrap_95pct':np.quantile(boot,[.025,.975]).tolist() if len(boot) else None}
    atomic(output/'selection-grid-risk.json',{'schema':'timing-grid-risk-v1','protocol_sha256':hashlib.sha256((output/'frozen-public-protocol.json').read_bytes()).hexdigest(),
        'rows':scores,'risk_coverage':curves,'paired_bootstrap':paired,'decision':'not_accepted_requires_independent_review',
        'confidence_note':'Residual is an uncalibrated ranking proxy; musical beat level remains separately evaluated',
        'missing_spectrograms':['gtzan_reggae_00086'],'beat_only_groups_excluded_from_grid_certification':unavailable_downbeat,
        'public_audio_methods_not_run':['librosa','V32 ordinary','V32 super']})
    print(json.dumps({'public_grid_risk_rows':len(scores),'output':str(output/'selection-grid-risk.json')}),flush=True)

def audit_controlled():
    items={x['id']:x for x in json.loads((HOME/'synthetic/ground-truth.json').read_text())};rows=[]
    for path in (HOME/'runs').glob('*.json'):
        row=json.loads(path.read_text())
        if row.get('item_id') not in items or row['status']!='completed':continue
        item=items[row['item_id']];sr=item['source']['sample_rate'];raw=row['raw']
        metrics={key:detection_metrics(item['labels'][key],raw[key],sr) for key in ('beat_samples','downbeat_samples')}
        timing=timing_map(raw,item['source'])
        if 'independent_v32_disagreement' in raw.get('reasons',[]):timing['eligibility']['v32']=False
        gate={}
        for key,m in metrics.items():
            if not item['labels'][key]:gate[key]=not raw[key]
            else:gate[key]=bool(m['recall']>=.99 and m['precision']>=.99 and m['p95_ms'] is not None and m['p95_ms']<=25 and m['drift_over_30s_ms'] is not None and abs(m['drift_over_30s_ms'])<=20 and not m['half_or_double'])
        rows.append({'run_id':row['run_id'],'finished':row.get('finished',0),'item_id':row['item_id'],'method':row['method'],'comparison_revision':row.get('comparison_revision'),
                     'metrics':metrics,'engineering_gate':gate,'eligible_map':timing['eligibility']['v32'],
                     'wrong_accept':bool(timing['eligibility']['v32'] and not all(gate.values())),
                     'map_version':timing['id'],'label_note':item['label_note']})
    result={'schema':'controlled-audit-v1','gate':{'minimum_recall':.99,'minimum_precision':.99,'p95_all_reference_ms':25,'drift_30s_ms':20},
            'note':'Postprocessing audit of retained raw predictions; model inference unchanged. Synthetic accuracy is not natural music accuracy.',
            'rows':rows,'method_totals':{}}
    for method in METHODS:
        latest={}
        for row in sorted(rows,key=lambda x:x['finished']):
            if row['method']==method:latest[row['item_id']]=row
        values=list(latest.values());accepted=[x for x in values if x['eligible_map']]
        result['method_totals'][method]={'evaluated_cases':len(values),'accepted':len(accepted),'wrong_accepts':sum(x['wrong_accept'] for x in accepted),
                                      'coverage':len(accepted)/len(items),'risk':sum(x['wrong_accept'] for x in accepted)/len(accepted) if accepted else None}
    atomic(HOME/'controlled-audit.json',result)
    print(json.dumps({'controlled_audit_cases':len(rows),'output':str(HOME/'controlled-audit.json')}),flush=True)

def downstream_ab():
    """Functional reference consumption experiment, never production approval."""
    from malody_studio.resident import call
    from malody_studio.advanced_generation import write_reference
    from malody_studio.mapperatorinator import read_osu
    audit_selected()
    records=json.loads((HOME/'selected-final1-audit.json').read_text())['rows']
    audio_paths={x['source']['pcm_sha256']:x['audio'] for x in json.loads((HOME/'frozen-protocol.json').read_text(encoding='utf-8'))['items']}
    selected=None
    # Deterministic first structurally supported window. This is not an accuracy
    # benchmark or a search for good generation output; no notes are inspected.
    for record in sorted(records,key=lambda x:x['item_id']):
        if record['ground_truth']:continue
        sr=record['source']['sample_rate']
        for start in range(0,int(record['source']['effective_end_sample']/sr)-29,10):
            raw={key:[x-start*sr for x in record['raw'][key] if start*sr<=x<(start+30)*sr] for key in ('beat_samples','downbeat_samples')}
            data,actual_sr=sf.read(audio_paths[record['item_id']],dtype='float32',always_2d=True,start=start*sr,frames=30*sr)
            source={'sample_rate':sr,'samples':len(data),'effective_end_sample':len(data),'channels':data.shape[1],
                    'pcm_sha256':hashlib.sha256(data.astype('<f4').tobytes()).hexdigest()}
            timing=timing_map(raw,source)
            if timing['eligibility']['v32']:
                selected=(record,start,data,source,timing);break
        if selected:break
    if selected is None:raise RuntimeError('No measured structurally supported 30-second reference; no fabricated reference permitted')
    record,start,data,source,timing=selected
    output=HOME/'downstream-ab'/uuid.uuid4().hex;output.mkdir(parents=True)
    audio=output/'source.wav';sf.write(audio,data,source['sample_rate'],subtype='FLOAT')
    atomic(output/'timing-map.json',timing)
    reference=write_reference(output/'experimental-reference.osu',{'timing_map':timing})
    protocol={'source':source,'original_pcm_sha256':record['source']['pcm_sha256'],'original_start_second':start,
              'difficulty':'hard','sr':8.0,'seed':20261005,'reference_experiment_only':True,'production_strong_reference_approved':False,
              'window_selection':'first structurally eligible 30s window on 10s stride, sorted PCM identity; not ground truth',
              'selected_final1_run':record['actual_prediction_run'],'no_fallback_reference':True}
    atomic(output/'protocol.json',protocol)
    for variant in ('normal_no_reference','experimental_final1_reference'):
        folder=output/variant;folder.mkdir()
        request={'audio':str(audio),'output':str(folder),'title':'Timing reference functional A/B','artist':'Local experiment',
                 'seed':20261005,'ln_ratio':.4,'temperature':.9,'top_p':.9,'mania_column_temperature':.8,'cfg_scale':1.,'year':2024,
                 'experimental_parameter_snapshot':True,'presets':[{'key':'hard','label':'Hard','sr':8.,'seed':20261005}]}
        if variant=='experimental_final1_reference':request['timing_reference']=str(reference)
        atomic(folder/'request.json',request);started=time.perf_counter()
        result={'status':'running','variant':variant,'request':request};atomic(folder/'experiment-result.json',result)
        try:
            result['worker']=call('v32',{'request_path':str(folder/'request.json')})
            charts={}
            for key,path in result['worker']['charts'].items():
                notes,points,inherited=read_osu(path)
                charts[key]={'path':path,'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                             'timing_points':points,'inherited_timing':inherited,
                             'notes':[{'lane':n.lane,'start_ms':n.start,'end_ms':n.end} for n in notes]}
            result.update(status='completed' if charts else 'failed',charts=charts)
        except Exception as exc:result.update(status='failed',error=str(exc),error_type=type(exc).__name__)
        result['elapsed_seconds']=time.perf_counter()-started;atomic(folder/'experiment-result.json',result)
        print(json.dumps({'downstream_ab':variant,'status':result['status'],'seconds':result['elapsed_seconds'],'output':str(folder)}),flush=True)

def audit_selected():
    """Reuse actual independently decoded final1 logits saved by B1 inference."""
    items={x['id']:x for x in json.loads((HOME/'synthetic/ground-truth.json').read_text())}
    latest={}
    for path in (HOME/'runs').glob('*.json'):
        row=json.loads(path.read_text())
        if row.get('comparison_revision')!=COMPARISON_REVISION or row['method']!='B1' or row['status']!='completed':continue
        if row.get('finished',0)>latest.get(row['item_id'],{}).get('finished',-1):latest[row['item_id']]=row
    records=[]
    for identity,row in latest.items():
        raw=next((x for x in row['raw'].get('individual_predictions',[]) if x['checkpoint']=='final1'),None)
        if raw is None:
            if row['raw'].get('provenance',{}).get('inference_skipped')!='exact_zero_source':continue
            raw={'beat_samples':[],'downbeat_samples':[],'inference_skipped':'exact_zero_source'}
        raw={**raw,'provenance':{'adapter':'beat_this_final1','extracted_from_actual_run':row['run_id']}}
        timing=timing_map(raw,row['source'])
        result={'item_id':identity,'source':row['source'],'split':row['split'],'actual_prediction_run':row['run_id'],
                'raw':raw,'timing':timing,'cost_note':'Final1 individual prediction reused from measured three-checkpoint B1; individual cost not isolated',
                'ground_truth':identity in items}
        if identity in items:
            item=items[identity];variants={'raw':raw,'robust_fit':ba.fitted_prediction(raw,row['source']['sample_rate'])}
            result['controlled']={}
            for variant,prediction in variants.items():
                metrics={key:detection_metrics(item['labels'][key],prediction[key],row['source']['sample_rate']) for key in ('beat_samples','downbeat_samples')}
                gate={key:bool(not prediction[key]) if not item['labels'][key] else bool(m['recall']>=.99 and m['precision']>=.99 and m['p95_ms'] is not None and m['p95_ms']<=25 and m['drift_over_30s_ms'] is not None and abs(m['drift_over_30s_ms'])<=20 and not m['half_or_double']) for key,m in metrics.items()}
                result['controlled'][variant]={'metrics':metrics,'engineering_gate':gate}
        records.append(result)
    atomic(HOME/'selected-final1-audit.json',{'schema':'selected-final1-audit-v1','method':'final1 actual saved independent decode',
        'production_strong_reference_approved':False,'synthetic_cases':sum(x['ground_truth'] for x in records),
        'local_unlabelled_cases':sum(not x['ground_truth'] for x in records),'rows':records})
    print(json.dumps({'selected_final1_cases':len(records)}),flush=True)

if __name__=='__main__':main()
