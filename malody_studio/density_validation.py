"""Density acceptance on the frozen acoustic clock, never the model's span."""
from bisect import bisect_left
import math

VERSION = 'density-validation-v1'
RATES = dict(zip(('easy','medium','hard','expert','master','lunatic'), (2.5,5.,8.5,13.,18.5,26.)))
PEAKS = dict(zip(RATES, (5,8,13,19,27,38)))
MAX_EXTRA_ROUNDS = 2


def contract():
    return {'version': VERSION, 'count_band': [.85,1.15], 'rates':dict(RATES),
            'peak_ceilings':dict(PEAKS), 'max_extra_rounds':MAX_EXTRA_ROUNDS,
            'denominator':'frozen_acoustic_active_seconds', 'peak_is_ceiling':True}


def _starts(events, low, high):
    return sorted(float(e['start_ms'] if isinstance(e,dict) else e.start)
                  for e in events if low <= float(e['start_ms'] if isinstance(e,dict) else e.start) < high)


def evaluate(events, plan, difficulty, bounds, duration=None, candidates=None, attempts=0):
    """bounds are original samples; duration is informational and cannot shrink time.

    candidates may be a dict with explicit model_heads/acoustic_capacity_heads and
    constraint_removed. A raw model note count is not evidence of acoustic capacity.
    """
    if not plan or not plan.get('sections'):
        return {'version':VERSION,'difficulty':difficulty,'bounds':list(bounds),'status':'not_evaluated',
                'passed':False,'retry_allowed':False,'reason':'missing_frozen_plan','cause':'missing_frozen_plan',
                'actual_heads':len(events),'target_heads':None,'attempts':int(attempts),'regions':[],'local_deficits':[]}
    sr=float(plan.get('sample_rate',44100)); low,high=map(float,bounds)
    if not math.isfinite(sr) or sr<=0 or high<low:raise ValueError('Invalid frozen density bounds')
    times=_starts(events,low*1000/sr,high*1000/sr)
    local=[]; total=0.; active=0.; infeasible=[]; peak_caps=[]
    for section in plan.get('sections',[]):
        a=max(low,section['core'][0]); b=min(high,section['core'][1])
        if a>=b:continue
        profile=section.get('profile',[])
        if profile:
            seconds=sum(max(0.,min(b,p['end_sample'])-max(a,p['start_sample']))/sr*
                        max(0.,min(1.,float(p.get('active_fraction',0)))) for p in profile)
        else:
            seconds=float(section.get('active_seconds',(section['core'][1]-section['core'][0])/sr))*(b-a)/(section['core'][1]-section['core'][0])
        detail=section.get('per_difficulty',{}).get(difficulty,{})
        rate=float(detail.get('target_rate',RATES[difficulty]))
        peak=float(detail.get('hard_caps',{}).get('peak_1s',PEAKS[difficulty]))
        # Frozen quotas take priority over recomputing a rounded target_rate.
        frozen_active=float(section.get('active_seconds',0))
        if profile:
            frozen_active=sum(max(0.,min(section['core'][1],p['end_sample'])-max(section['core'][0],p['start_sample']))/sr*
                              max(0.,min(1.,float(p.get('active_fraction',0)))) for p in profile)
        target=(float(detail['target_heads_soft'])*seconds/frozen_active
                if 'target_heads_soft' in detail and frozen_active>0 else seconds*rate)
        count=bisect_left(times,b*1000/sr)-bisect_left(times,a*1000/sr)
        if target*.85>peak*(b-a)/sr+1e-6:infeasible.append(section.get('id'))
        # Peaks use half-open rolling seconds, including coincident chord heads.
        # Every rolling window touching this region must obey its ceiling,
        # including windows spanning a boundary with a neighboring region.
        actual_peak=max((bisect_left(times,t+1000)-i for i,t in enumerate(times)
                         if t<b*1000/sr and t+1000>a*1000/sr),default=0)
        local.append({'id':section.get('id'),'section_id':section.get('id'),'range':[a,b],'bounds':[a,b],'active_seconds':seconds,
                      'target_heads':target,'min_heads':target*.85,'max_heads':target*1.15,
                      'actual_heads':count,'deficit':max(0.,target*.85-count),
                      'peak_1s':actual_peak,'peak_ceiling':peak,'peak_violation':actual_peak>peak,
                      'status':'constraints' if actual_peak>peak else 'underfilled' if count<target*.85-1e-6
                               else 'overfilled' if count>target*1.15+1e-6 else 'pass'})
        total+=target; active+=seconds; peak_caps.append(peak)
    evidence=candidates if isinstance(candidates,dict) else {}
    model=evidence.get('model_heads'); capacity=evidence.get('acoustic_capacity_heads')
    candidate_heads=evidence.get('candidate_heads', model)
    constraints=int(evidence.get('constraint_removed',0) or 0)
    deficits=[r for r in local if r['deficit']>1e-6]
    global_ok=total*.85-1e-6<=len(times)<=total*1.15+1e-6
    peaks=any(r['peak_violation'] for r in local)
    covered=sum(r['bounds'][1]-r['bounds'][0] for r in local)
    if covered<high-low-1e-6:infeasible.append('uncovered_bounds')
    cause=None
    if infeasible:cause='infeasible_plan'
    elif peaks:cause='constraints'
    elif len(times)>total*1.15+1e-6:cause='constraints'
    elif deficits or not global_ok:
        if constraints and candidate_heads is not None and candidate_heads>=total*.85:cause='constraints'
        elif capacity is not None and capacity<total*.85:cause='acoustic_capacity'
        elif evidence.get('model_supply_failure') is True:cause='model_supply'
        else:cause='unknown'
    elif peaks:cause='constraints'
    passed=global_ok and not deficits and not peaks and not infeasible
    peak_nps=max((r['peak_1s'] for r in local),default=0)
    status=('pass' if passed else 'infeasible' if infeasible else 'constraints' if peaks
            else 'overfilled' if len(times)>total*1.15+1e-6 else 'constraints' if cause=='constraints'
            else 'underfilled' if deficits or len(times)<total*.85 else 'overfilled')
    whole_seconds=max((high-low)/sr,float(duration) if duration is not None else 0.)
    return {'version':VERSION,'difficulty':difficulty,'bounds':list(bounds),'active_seconds':active,
            'target_heads':total,'min_heads':total*.85,'max_heads':total*1.15,'actual_heads':len(times),
            'achieved_rate':len(times)/active if active else 0.,'global_count_passed':global_ok,
            'local_sections':local,'local_deficits':deficits,'peak_violation':peaks,
            'infeasible_sections':infeasible,'passed':passed,'status':status,
            'target_nps':total/active if active else 0.,'active_nps':len(times)/active if active else 0.,
            'whole_nps':len(times)/whole_seconds if whole_seconds>0 else 0.,
            'peak_nps':peak_nps,'peak_cap':min(peak_caps) if peak_caps else PEAKS[difficulty],
            'completion':len(times)/total if total else (1. if not times else 0.),'regions':local,'reason':cause,
            'cause':cause,'model_supply':{'heads':model,'known':model is not None},
            'cause_assessment':'evidence-v2','candidate_supply':{'heads':candidate_heads,
                'acoustic_heads':evidence.get('acoustic_candidate_heads'),
                'known':candidate_heads is not None},
            'reason_label':{'unknown':'原因待核验','model_supply':'模型候选供给不足',
                'acoustic_capacity':'已核验的声音证据不足','constraints':'硬约束阻塞',
                'infeasible_plan':'计划与限制冲突'}.get(cause),
            'acoustic_capacity':{'heads':capacity,'known':capacity is not None},
            'constraints':{'removed_heads':constraints},'attempts':int(attempts),
            'retry_allowed':not passed and cause=='model_supply' and int(attempts)<MAX_EXTRA_ROUNDS}


def candidate_rank(events, report, rule=None):
    """Lower is better. Structural conflicts dominate count closeness."""
    occupied={}; invalid=0; heads={}; gap=float((rule or {}).get('gap',0))
    for event in sorted(events,key=lambda e:(e['start_ms'],e['lane'])):
        lane=event['lane']; start=event['start_ms']; tail=event.get('end_ms')
        if lane not in range(4) or not math.isfinite(start) or (tail is not None and (not math.isfinite(tail) or tail<=start)):
            invalid+=1;continue
        if start<occupied.get(lane,-math.inf) or start-heads.get(lane,-math.inf)<max(.1,gap):invalid+=1
        occupied[lane]=tail if tail is not None else start;heads[lane]=start
    return (invalid,int(report.get('peak_violation',False)),int(bool(report.get('infeasible_sections'))),
            sum(r['deficit'] for r in report.get('local_deficits',[])),
            abs(report['actual_heads']-report['target_heads']))
