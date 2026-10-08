"""Versioned empirical inverse lookup; observed response need not be monotone."""
from copy import deepcopy
from collections import defaultdict
import hashlib
import json
import math
from statistics import median
from .density_validation import contract, RATES, MAX_EXTRA_ROUNDS

VERSION='density-calibration-v2'


def v32_execution_identity():
    """Freeze the deployed execution semantics, never consult them during retry."""
    from pathlib import Path
    from .paths import ROOT
    from .generation_context import contract as context_contract
    from .v32_event_serialization import POLICY
    adapters={}
    for name,path in (('worker',ROOT/'tools'/'mapperatorinator_worker.py'),
                      ('request_adapter',Path(__file__).with_name('mapperatorinator.py')),
                      ('event_serialization',Path(__file__).with_name('v32_event_serialization.py')),
                      ('cfg_batch_order',Path(__file__).with_name('v32_cfg.py')),
                      ('generation_context',Path(__file__).with_name('generation_context.py'))):
        adapters[name]=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'unavailable'
    identity={'engine':'v32','serializer_policy':POLICY,
              'timing_conditioning':'clock_only_preserve_model_defaults_v1',
              'generation_context_policy':context_contract(),'adapter_sha256':adapters}
    identity['hash']=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    return identity


def _development_rows(observations):
    rows=[]
    for row in observations:
        # Unqualified parent-clock trials are causal diagnostics, not production
        # density calibration, even when their model output parsed successfully.
        if row.get('experimental_only') is True or row.get('reference_mode')=='revision':continue
        if row.get('reference_qualified') is False:continue
        if isinstance(row.get('reference'),dict) and row['reference'].get('qualified') is False:continue
        if row.get('split','development') in ('holdout','test','validation') or row.get('holdout',False):continue
        if row.get('status','completed') not in ('completed','success','passed'):continue
        try:
            condition=float(row['condition'])
            seconds=float(row.get('active_seconds',1))
            response=float(row['achieved_rate']) if 'achieved_rate' in row else float(row.get('heads',0))/seconds
        except (KeyError,TypeError,ValueError,ZeroDivisionError):continue
        if seconds<=0 or not all(math.isfinite(x) for x in (condition,seconds,response)) or response<0:continue
        rows.append({**row,'condition':condition,'achieved_rate':response})
    return rows


def response_curves(observations):
    """Repeated passes cannot give a recording extra voting weight.

    A median response is descriptive calibration evidence, not a claim of
    cross-recording predictive validity. Role and pattern never share a fit.
    """
    groups=defaultdict(lambda:defaultdict(list))
    for row in _development_rows(observations):
        key=(row.get('engine'),row.get('source_role'),row.get('pattern'),round(row['condition'],6))
        recording=str(row.get('recording_id') or row.get('source_pcm_sha256') or row.get('project_id') or 'unidentified')
        groups[key][recording].append(row['achieved_rate'])
    curves=[]
    for (engine,role,pattern,condition),recordings in groups.items():
        responses={key:median(values) for key,values in sorted(recordings.items())}
        curves.append({'engine':engine,'source_role':role,'pattern':pattern,'condition':condition,
                       'achieved_rate':median(responses.values()),'recording_count':len(responses),
                       'recording_responses':responses,'response_min':min(responses.values()),
                       'response_max':max(responses.values()),'observation_count':sum(map(len,recordings.values()))})
    return sorted(curves,key=lambda row:(str(row['engine']),str(row['source_role']),str(row['pattern']),row['condition']))


def freeze(settings, observations=None, execution_identity=None):
    """Return a detached JSON snapshot. No unverified response curve is invented."""
    eligible=[];excluded=defaultdict(int)
    for row in observations or []:
        if execution_identity is not None and row.get('engine')=='v32':
            observed=row.get('execution_identity')
            if observed!=execution_identity:
                excluded['missing_execution_identity' if observed is None else 'execution_identity_mismatch']+=1
                continue
        eligible.append(row)
    curves=response_curves(eligible)
    policy={'version':VERSION,'contract':contract(),'conditions':deepcopy(settings.get('conditions',{})),
            'generation_settings':deepcopy({key:value for key,value in settings.items() if key!='density_policy'}),
            'difficulty_rules':deepcopy(settings.get('difficulty_rules',{})),
            'observations':deepcopy(observations or []),'max_extra_rounds':MAX_EXTRA_ROUNDS,
            'calibration_status':'empirical' if curves else 'experimental_fallback',
            'calibration_opt_in':bool(settings.get('calibration_opt_in',False)),
            'calibration_qualification':deepcopy(settings.get('calibration_qualification',{})),
            'aggregation':'per-recording-median-then-equal-recording-median-v1','response_curves':curves}
    if execution_identity is not None:
        policy.update(execution_identity=deepcopy(execution_identity),
            excluded_count=sum(excluded.values()),excluded_observation_reasons=dict(excluded),
            observation_eligibility_reason='V32 calibration requires the exact frozen execution identity; original observations retained for audit')
    policy['policy_hash']=hashlib.sha256(json.dumps(policy,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    return policy


def highest_requested(keys, rules=None):
    return max(keys,key=lambda key:float((rules or {}).get(key,{}).get('rate',RATES[key])))


def condition_next(policy, engine, difficulty, target_rate=None, attempt=0, used_conditions=(), source_role=None, pattern=None):
    if int(attempt)>=min(MAX_EXTRA_ROUNDS,int(policy.get('max_extra_rounds',MAX_EXTRA_ROUNDS))):return None
    target=float(target_rate if target_rate is not None else policy.get('difficulty_rules',{}).get(difficulty,{}).get('rate',RATES[difficulty]))
    used={round(float(c),6) for c in used_conditions}
    lower,upper=(1.,10.) if engine=='v32' else (1.5,8.)
    curves=policy.get('response_curves')
    if curves is None:curves=response_curves(policy.get('observations',[]))
    observations=[r for r in curves if r.get('engine')==engine
                  and (source_role is None or r.get('source_role') in (None,source_role))
                  and (pattern is None or r.get('pattern') in (None,pattern))
                  and lower<=float(r['condition'])<=upper
                  and round(float(r['condition']),6) not in used]
    if observations:
        def loss(row):
            response=float(row['achieved_rate'])
            return (abs(response-target),float(row['condition']))
        return float(min(observations,key=loss)['condition'])
    # An exploration schedule, explicitly not an empirical prediction. Try a
    # distant condition, then the opposite endpoint; never assume +.35 works.
    base=float(policy.get('conditions',{}).get(engine,{}).get(difficulty,upper))
    for value in (base,upper,lower,(lower+upper)/2):
        value=max(lower,min(upper,value))
        if round(value,6) not in used:return value
    return None


def initial_condition(policy, engine, difficulty, fallback, target_rate=None, source_role=None, pattern=None):
    """Initial settings change only with explicitly vetted calibration opt-in."""
    qualification=policy.get('calibration_qualification') or {}
    if not (policy.get('calibration_opt_in') is True and qualification.get('qualified') is True
            and qualification.get('validation_id') and qualification.get('min_recordings',0)>=3):
        return float(fallback)
    curves=policy.get('response_curves')
    if curves is None:curves=response_curves(policy.get('observations',[]))
    applicable=[row for row in curves if row.get('engine')==engine
                and (source_role is None or row.get('source_role')==source_role)
                and (pattern is None or row.get('pattern')==pattern)]
    applicable=[row for row in applicable if row.get('recording_count',0)>=qualification['min_recordings']]
    if not applicable:return float(fallback)
    qualified={**policy,'response_curves':applicable}
    selected=condition_next(qualified,engine,difficulty,target_rate,source_role=source_role,pattern=pattern)
    return float(fallback) if selected is None else selected
