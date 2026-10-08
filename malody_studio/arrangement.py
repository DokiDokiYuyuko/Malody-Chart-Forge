"""Derive sealed musical regions and a conserved budget from frozen evidence."""
import copy
import math
import numpy as np
from . import section_plan as sp, music_timing, bpm_buckets

SCHEMA = 'arrangement-plan-v1'
REGION_VERSION = 'phrase-regions-v2'
REGION_POLICIES = {
    'fine': {'minimum_seconds': 4, 'fallback_seconds': 10, 'bars': 4, 'beats': 16},
    'balanced': {'minimum_seconds': 8, 'fallback_seconds': 20, 'bars': 8, 'beats': 32},
    'coarse': {'minimum_seconds': 12, 'fallback_seconds': 32, 'bars': 16, 'beats': 64},
}


def _suggest_regions(source, timing, activity, acoustic, settings):
    """Compress display regions without quantizing or discarding timing evidence."""
    sr, stop, samples = source['sample_rate'], source['effective_end_sample'], source['samples']
    mode = settings.get('region_granularity', 'balanced')
    policy = {**REGION_POLICIES[mode], 'max_count': settings.get('region_max_count', 24),
              'tempo_change_fraction': .03, 'activity_persistence_seconds': 3,
              'version': REGION_VERSION, 'mode': mode}
    if not stop:
        return [{'start_sample': 0, 'end_sample': samples, 'cut_reason': 'original_start'}], {**policy, 'merges': []}
    cuts = {0: ('original_start', 10), stop: ('effective_audio_end', 10)}
    def add(sample, reason, priority):
        if 0 < sample < stop and priority > cuts.get(sample, ('', 0))[1]:
            cuts[sample] = (reason, priority)
    trusted = all(timing['eligibility'][key] for key in ('meter', 'phase', 'beat'))
    beat_this=timing.get('provenance',{}).get('adapter','').startswith('beat_this')
    anchors = timing['downbeat_samples'] if trusted else timing['beat_samples'] or ([] if beat_this else acoustic.get('beat_estimate_samples', []))
    stride = policy['bars'] if trusted else policy['beats']
    # The first observed beat is a pickup, never a standalone introductory region.
    for sample in anchors[stride::stride]:
        add(sample, 'measured_bar_group' if trusted else 'beat_group_meter_uncertain', 1)
    if len(cuts) == 2:
        for sample in range(policy['fallback_seconds']*sr, stop, policy['fallback_seconds']*sr):
            add(sample, 'activity_window_meter_uncertain', 1)
    # A one-second fluctuation is not a new musical section. Require three
    # seconds of sustained contrast, then collapse the edge neighbourhood.
    changes = []
    for index in range(3, len(activity)-2):
        before = activity[index-3:index]; after = activity[index:index+3]
        if any(row['end_sample'] > stop for row in after): continue
        left = [r['activity_delta'] for r in before]; right = [r['activity_delta'] for r in after]
        if max(left)-min(left) > .35 or max(right)-min(right) > .35: continue
        delta = abs(float(np.median(left)) - float(np.median(right)))
        if delta > .55: changes.append((activity[index]['start_sample'], delta))
    accepted_changes = []
    for sample, delta in sorted(changes, key=lambda row: (-row[1], row[0])):
        if all(abs(sample-other) >= 3*sr for other in accepted_changes):
            add(sample, 'sustained_activity_change', 3)
            accepted_changes.append(sample)
    # Preserve strong rests as preferred boundaries, but merge short display
    # fragments; silence and budgets remain exact in the underlying profile.
    rest_start = None
    for row in activity:
        if row['start_sample'] >= stop: break
        if row.get('exact_silence') and rest_start is None: rest_start = row['start_sample']
        if not row.get('exact_silence') and rest_start is not None:
            if row['start_sample']-rest_start >= 2*sr:
                add(rest_start, 'strong_rest', 4); add(row['start_sample'], 'strong_rest_end', 4)
            rest_start = None
    # Small BPM jitter alone does not split the display. Larger confirmed
    # changes get priority, while every original tempo point stays untouched.
    points = timing.get('tempo_points', [])
    if timing['eligibility']['beat']:
        for left, right in zip(points, points[1:]):
            if abs(right['bpm']/left['bpm']-1) > policy['tempo_change_fraction']:
                add(right['sample'], 'measured_tempo_change', 5)
    bounds = sorted(cuts); merges = []; minimum = round(policy['minimum_seconds']*sr)
    def discard(sample, reason):
        bounds.remove(sample)
        merges.append({'sample': sample, 'original_reason': cuts[sample][0], 'reason': reason})
    while len(bounds) > 2:
        short = next((i for i in range(len(bounds)-1) if bounds[i+1]-bounds[i] < minimum), None)
        if short is None: break
        choices = [x for x in (bounds[short], bounds[short+1]) if x not in (0, stop)]
        discard(min(choices, key=lambda x: (cuts[x][1], x)), 'minimum_region_duration')
    def activity_at(a, b):
        rows = [r for r in activity if r['start_sample'] < b and r['end_sample'] > a]
        return float(np.median([r['activity_delta'] for r in rows])) if rows else 0.
    while len(bounds)-1 > policy['max_count']:
        # Prefer removing a weak subdivision between similar musical activity.
        rank = lambda i: (cuts[bounds[i]][1], abs(activity_at(bounds[i-1], bounds[i])-
                                                activity_at(bounds[i], bounds[i+1])), bounds[i+1]-bounds[i-1], bounds[i])
        discard(bounds[min(range(1, len(bounds)-1), key=rank)], 'region_count_limit')
    regions = [{'start_sample': a, 'end_sample': b, 'cut_reason': cuts[a][0]}
               for a, b in zip(bounds, bounds[1:])]
    if stop < samples:
        regions.append({'start_sample': stop, 'end_sample': samples, 'cut_reason': 'excluded_silent_tail'})
    return regions, {**policy, 'merges': merges, 'suggested_count': len(bounds)-1}


def validate(value, evidence=None, timing=None):
    if value.get('id') != music_timing.digest({k:v for k,v in value.items() if k!='id'}):
        raise ValueError('编排方案封印校验失败')
    if evidence and value['evidence_id'] != evidence['id']:raise ValueError('编排音乐证据不符')
    if timing and value['timing_map_id'] != timing['id']:raise ValueError('编排节拍证据不符')
    if value.get('sections'):
        sp.validate_plan(value['section_plan'],value['source']['pcm_sha256'])
        if value['sections'] != value['section_plan']['sections']:raise ValueError('编排共享段落不符')
    return value


def _normalized(factors,weights):
    if not sum(weights):return [1. for _ in factors]
    lo,hi=.1,10.
    for _ in range(70):
        mid=(lo+hi)/2
        mean=sum(w*min(1.25,max(.75,f*mid)) for f,w in zip(factors,weights))/sum(weights)
        if mean>1:hi=mid
        else:lo=mid
    return [min(1.25,max(.75,f*(lo+hi)/2)) for f in factors]


def build(evidence,timing,acoustic,settings,chosen,profile='keyboard',source_id='original',stem_set_id=None,regions=None,bucket_version=None):
    # bucket_version None is the historical v2 estimator: callers that create NEW
    # analyses pass bpm_buckets.VERSION; frozen plans pass the version they carry.
    music_timing.validate_evidence(evidence);music_timing.validate_timing(timing,evidence['source'])
    from .acoustic_evidence import validate as validate_acoustic
    validate_acoustic(acoustic,evidence['source'])
    source=evidence['source'];sr=source['sample_rate'];stop=source['effective_end_sample'];samples=source['samples']
    activity=copy.deepcopy(acoustic['profile'])
    rates=[r['onset_rate'] for r in activity if not r.get('exact_silence')]
    median=float(np.median(rates)) if rates else 0.;scale=max(2.,float(np.ptp(rates))) if rates else 2.
    for row in activity:row['activity_delta']=float(np.clip((row['onset_rate']-median)/scale,-1,1))
    bucket_policy=(bpm_buckets.build(timing,settings,bucket_version)
                   if timing.get('provenance',{}).get('adapter','').startswith('beat_this') else None)
    region_policy = None
    user_regions = regions is not None
    if regions is None:
        regions, region_policy = _suggest_regions(source, timing, activity, acoustic, settings)
    if bucket_policy and bucket_policy['version']==bpm_buckets.VERSION_V3:
        bucket_policy['region_assignment']=bpm_buckets.REGION_ASSIGNMENT if user_regions else 'automatic_bucket_split'
    if bucket_policy and bucket_policy['enabled']:
        # v2 re-cut every region at each bucket boundary. From v3 a supplied
        # (user-confirmed) region is one condition; only suggestions are split.
        regions=(bpm_buckets.assign_regions(regions,bucket_policy)
                 if user_regions and bucket_policy['version']==bpm_buckets.VERSION_V3
                 else bpm_buckets.split_regions(regions,bucket_policy))
    # Preserve omitted user gaps on the source clock with zero-budget cores.
    filled=[];cursor=0
    for region in sorted(regions,key=lambda r:r['start_sample']):
        if region['start_sample']<cursor:raise ValueError('编排区域重叠')
        if region['start_sample']>cursor:filled.append({'start_sample':cursor,'end_sample':region['start_sample'],'included':False,'gap':True})
        filled.append(region);cursor=region['end_sample']
    if cursor<samples:filled.append({'start_sample':cursor,'end_sample':samples,'included':False,'gap':True})
    regions=filled
    sections=[];out=[];weights=[];factors=[]
    for index,region in enumerate(regions):
        a,b=region['start_sample'],region['end_sample'];local=[r for r in activity if r['start_sample']<b and r['end_sample']>a]
        active=sum(max(0,min(b,stop,r['end_sample'])-max(a,r['start_sample']))/sr*r['active_fraction'] for r in local)
        if region.get('included') is False:
            active=0.
            local=[{**r,'active_fraction':0.} for r in local]
        delta=sum(max(0,min(b,r['end_sample'])-max(a,r['start_sample']))*r['activity_delta'] for r in local)/max(1,b-a)
        intensity=region.get('intensity','calm' if delta<-.25 else 'dense' if delta>.25 else 'regular')
        if intensity not in ('calm','regular','dense'):raise ValueError('区域强弱无效')
        factor=float(region.get('factor',{'calm':.8,'regular':1.,'dense':1.2}[intensity]))
        if bucket_policy:
            factor=region.get('bpm_bucket_factor',1.) if bucket_policy['enabled'] else 1.
        if not math.isfinite(factor) or not .75<=factor<=1.25:raise ValueError('区域因子须在 .75–1.25')
        weights.append(active);factors.append(factor)
        beats=[x for x in timing['beat_samples'] if a<x<min(b,stop)]
        phrase_cuts=[a,*beats[::4],b];phrase_cuts=sorted(set(phrase_cuts))
        phrases=[{'range':[x,y],'reason':'measured_beat_group' if beats else 'activity_region'} for x,y in zip(phrase_cuts,phrase_cuts[1:])]
        confidence={'eligible_boost':False,'bpm':None,'source':'shared_timing_map','uncertain':timing['uncertain'],'note_shift_ms':0}
        if bucket_policy:
            confidence.update(bpm=region.get('detected_bpm'),source='beat_this_bpm_bucket',
                              bucket_id=region.get('bpm_bucket_id'),
                              difficulty_bucket_available=region.get('bpm_bucket_id') is not None)
        detail=sp.difficulty_budget(settings,active,0,confidence)
        sections.append({'id':f'region-{index}-{a}-{b}','core':[a,b],'context':[max(0,a-4*sr),min(samples,b+4*sr)],
                         'active_seconds':active,'rhythm_activity':delta,'profile':local,'phrases':phrases,
                         'beat_samples':[x for x in timing['beat_samples'] if a<=x<b],
                         'onsets':[[sample,strength] for sample,strength in zip(acoustic['onset_samples'],acoustic['onset_strengths']) if a<=sample<b],
                         'tempo_confidence':confidence,'per_difficulty':detail})
        if bucket_policy:
            sections[-1].update(bpm_bucket_id=region.get('bpm_bucket_id'),
                                bpm_bucket_factor=factor,bpm_bucket_offset=region.get('bpm_bucket_offset',0.))
        out.append({**region,'intensity':intensity,'factor':factor,'cut_reason':region.get('cut_reason','preserved_user_boundary')})
    # The middle BPM bucket is exactly the user's base difficulty. Global
    # renormalization would shift that baseline and erase the bucket contract.
    normalized=factors if bucket_policy else _normalized(factors,weights)
    for region,section,factor in zip(out,sections,normalized):
        region['normalized_factor']=factor
        section['normalized_factor']=factor
        for key,detail in section['per_difficulty'].items():
            detail['target_heads_soft']=detail['target_heads_soft']*factor;detail['target_rate']*=factor
            detail['effective_boost']=factor-1
            if bucket_policy:
                detail['target_rate']=settings['difficulty_rules'].get(key,sp.PRESETS[key])['rate']*factor
                detail['target_heads_soft']=detail['target_rate']*section['active_seconds']
                detail['tempo_boost']=factor-1
                detail['model_condition']=float(np.clip(detail['base_model_condition']+
                    sp.DELTAS[key]*section.get('bpm_bucket_offset',0.)*settings.get('dynamic_strength',1.)
                    *settings.get('bpm_bucket_range',.2)/.2 if settings.get('dynamic_enabled') else detail['base_model_condition'],
                    1 if settings['engine']=='v32' else 1.5,10 if settings['engine']=='v32' else 8))
            elif settings.get('dynamic_enabled'):detail['model_condition']=float(np.clip(detail['base_model_condition']+sp.DELTAS[key]*(factor-1)/.25,1 if settings['engine']=='v32' else 1.5,10 if settings['engine']=='v32' else 8))
    plan={'schema_version':1,'version':sp.VERSION,'source_pcm_sha':source['pcm_sha256'],'sample_rate':sr,'samples':samples,
          'effective_range':[0,stop],'dynamic_enabled':settings.get('dynamic_enabled',False),'arrangement_enabled':True,
          'settings_hash':sp.plan_settings_hash(settings),'reference_hash':timing['id'],'tempo_reference':{},'sections':sections,
          'budget_application':'once-at-selection-or-fusion','acoustic_evidence_id':acoustic['id']}
    adapter=timing.get('provenance',{}).get('adapter','unknown')
    plan.update(analysis_adapter=adapter,music_evidence_id=evidence['id'],timing_map_id=timing['id'])
    if adapter.startswith('beat_this'):
        plan.update(planner='beat-this-buckets-v1',detector_execution='beat-this-primary-v1',
                    analysis_policy_hash=sp.canonical_hash(evidence.get('policy',{})))
    if bucket_policy:plan['bpm_buckets']=bucket_policy
    if region_policy: plan['region_policy'] = region_policy
    plan['id']=plan['content_hash']=sp.canonical_hash(plan)
    body={'schema':SCHEMA,'evidence_id':evidence['id'],'timing_map_id':timing['id'],'acoustic_evidence_id':acoustic['id'],
          'source':copy.deepcopy(source),'effective_range':[0,stop],'section_plan_id':plan['id'],'section_plan':plan,'sections':sections,
          'regions':[r for r in out if not r.get('gap')],'settings':settings,'variants':chosen,'profile':profile,'source_id':source_id}
    if region_policy: body['region_policy'] = region_policy
    if stem_set_id:body['stem_set_id']=stem_set_id
    return validate({**body,'id':music_timing.digest(body)},evidence,timing)
