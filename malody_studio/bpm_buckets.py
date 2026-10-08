"""Bounded BPM classes for difficulty; never a serialization or snapping clock."""
import math
import numpy as np

VERSION_V2 = 'bpm-buckets-v2'
VERSION_V3 = 'bpm-buckets-v3'
# VERSION is the estimator new analyses use. Frozen plans/jobs keep whichever
# known version they were built with; only a missing/unknown value means v2.
VERSION = VERSION_V3
KNOWN_VERSIONS = (VERSION_V2, VERSION_V3)
DEFAULT_FROZEN_VERSION = VERSION_V2
REGION_ASSIGNMENT = 'user_region_dominant_bucket_v1'


# --- bpm-buckets-v3 -------------------------------------------------------
# A detector can mark the pulse one level up or down (x2, x3, x3/2 ...). These
# ratios are folded back onto the song's own reference tempo; a local rate that
# cannot be explained that way and is far from the reference is not trusted.
FOLD_RATIOS = (1/4, 1/3, 1/2, 2/3, 3/4, 1., 4/3, 3/2, 2., 3., 4.)
FOLD_TOLERANCE = .06
REFERENCE_OCTAVE = (80., 170.)
UNEXPLAINED_RATIO = (.6, 1.7)
MIN_REGIME_SECONDS = 8.
MIN_REGIME_INTERVALS = 16
REGIME_DEVIATION = .08


def _local_rates(gaps):
    """Per-interval rate (gaps in seconds): nine-interval median, mean of supported."""
    rates = []
    for i in range(len(gaps)):
        window = gaps[max(0, i-4):min(len(gaps), i+5)]
        median = float(np.median(window))
        supported = window[abs(window/median-1) <= .12]
        # An edge window can have no value within 12% of its median (NaN in v2).
        rates.append(60/float(np.mean(supported)) if len(supported) else 60/median)
    return np.asarray(rates)


def _weighted_median(values, weights):
    order = np.argsort(values)
    cumulative = np.cumsum(np.asarray(weights)[order])
    return float(np.asarray(values)[order][np.searchsorted(cumulative, cumulative[-1]/2)])


def _reference_tempo(rates, gaps):
    tempo = _weighted_median(rates, gaps)
    low, high = REFERENCE_OCTAVE
    while tempo >= high:tempo /= 2
    while tempo < low:tempo *= 2
    return tempo


def _fold(rate, reference):
    error, ratio = min((abs(math.log(rate*k/reference)), k) for k in FOLD_RATIOS)
    if error <= math.log(1+FOLD_TOLERANCE):
        return rate*ratio
    if not UNEXPLAINED_RATIO[0] <= rate/reference <= UNEXPLAINED_RATIO[1]:
        return reference
    return rate


def _harmonic_support(values):
    values = np.asarray(values, dtype=float)
    median = float(np.median(values))
    supported = values[abs(values/median-1) <= .12]
    if not len(supported):supported = values  # two-value spans can have no member near their median
    return len(supported)/float(np.sum(1/supported)), len(supported)


def tempo_segments_v3(timing):
    """Folded local tempo regimes; the stored detector beats are never altered.

    Rates are folded onto the song's reference tempo, a regime needs eight
    seconds and sixteen intervals, and shorter ones join the nearest-tempo
    neighbour. `observed_bpm` keeps the unfolded value for transparency.
    """
    source = timing['source'];sr = source['sample_rate'];stop = source['effective_end_sample']
    beats = np.asarray(timing.get('beat_samples', []), dtype=np.int64)
    if len(beats) < 5:return []
    gaps = np.diff(beats).astype(float)
    centers = (beats[:-1]+beats[1:])/2
    observed = _local_rates(gaps/sr)
    reference = _reference_tempo(observed, gaps)
    rates = np.asarray([_fold(float(rate), reference) for rate in observed])
    bounds = [0];start = 0;pending = None
    for i, rate in enumerate(rates):
        median = float(np.median(rates[start:i+1]))
        if abs(rate/median-1) > REGIME_DEVIATION:
            if pending is None:pending = i
            if centers[i]-centers[pending] >= MIN_REGIME_SECONDS*sr and i-pending >= MIN_REGIME_INTERVALS:
                bounds.append(pending);start = pending;pending = None
        else:pending = None
    bounds.append(len(gaps))
    spans = [[left, right] for left, right in zip(bounds, bounds[1:]) if right > left]

    def seconds(left, right):
        a = 0 if left == 0 else centers[left]
        b = beats[-1] if right == len(gaps) else centers[right]
        return (b-a)/sr
    while len(spans) > 1:
        short = [i for i, (left, right) in enumerate(spans)
                 if seconds(left, right) < MIN_REGIME_SECONDS or right-left < MIN_REGIME_INTERVALS]
        if not short:break
        index = min(short, key=lambda i:seconds(*spans[i]))
        mine = _harmonic_support(rates[spans[index][0]:spans[index][1]])[0]
        neighbour = min((j for j in (index-1, index+1) if 0 <= j < len(spans)),
                        key=lambda j:(abs(math.log(_harmonic_support(rates[spans[j][0]:spans[j][1]])[0]/mine)),
                                      -seconds(*spans[j])))
        low, high = sorted((index, neighbour))
        spans[low:high+1] = [[spans[low][0], spans[high][1]]]
    result = []
    for left, right in spans:
        a = 0 if left == 0 else int(round(centers[left]))
        b = min(stop, int(beats[-1]+np.median(gaps[-9:]))) if right == len(gaps) else int(round(centers[right]))
        if a >= b:continue
        bpm, supported = _harmonic_support(rates[left:right])
        result.append({'range':[a, b],'bpm':bpm,'observed_bpm':float(np.median(observed[left:right])),
                       'interval_count':right-left,'supported_intervals':supported,
                       'support_fraction':supported/(right-left),
                       'source':'beat_this_folded_local_pulses'})
    return result


def tempo_segments(timing):
    """Read locally sustained pulse rates without a whole-song constant fit.

    The detector's observations are immutable. A nine-interval median rejects
    single missing/extra pulses; a new regime needs two seconds of support.
    No downbeat/meter certification or model-timing reference is implied.
    """
    source=timing['source'];sr=source['sample_rate'];stop=source['effective_end_sample']
    beats=np.asarray(timing.get('beat_samples',[]),dtype=np.int64)
    if len(beats)<5:return []
    gaps=np.diff(beats).astype(float)
    centers=(beats[:-1]+beats[1:])/2
    rates=[]
    for i in range(len(gaps)):
        window=gaps[max(0,i-4):min(len(gaps),i+5)]
        median=float(np.median(window))
        supported=window[abs(window/median-1)<=.12]
        rates.append(60*sr/float(np.mean(supported)))
    rates=np.asarray(rates)
    bounds=[0];start=0;pending=None
    for i,rate in enumerate(rates):
        reference=float(np.median(rates[start:i+1]))
        if abs(rate/reference-1)>.08:
            if pending is None:pending=i
            if centers[i]-centers[pending]>=2*sr and i-pending>=3:
                bounds.append(pending);start=pending;pending=None
        else:pending=None
    bounds.append(len(gaps))
    result=[]
    for left,right in zip(bounds,bounds[1:]):
        values=gaps[left:right]
        median=float(np.median(values));supported=values[abs(values/median-1)<=.12]
        a=0 if left==0 else int(round(centers[left]))
        b=min(stop,int(beats[-1]+np.median(gaps[-9:]))) if right==len(gaps) else int(round(centers[right]))
        if a>=b:continue
        result.append({'range':[a,b],'bpm':60*sr/float(np.mean(supported)),
                       'interval_count':len(values),'supported_intervals':len(supported),
                       'support_fraction':len(supported)/len(values),
                       'source':'beat_this_local_observed_pulses'})
    return result


def build(timing,settings,version=None):
    """`version` selects the estimator; None means the historical v2 estimator."""
    version=DEFAULT_FROZEN_VERSION if version is None else version
    if version not in KNOWN_VERSIONS:raise ValueError('未知 BPM 桶版本')
    v3=version==VERSION_V3
    count=settings.get('bpm_bucket_count',5)
    amplitude=float(settings.get('bpm_bucket_range',.2))*float(settings.get('dynamic_strength',1))
    enabled=bool(settings.get('dynamic_enabled'))
    segments=tempo_segments_v3(timing) if v3 else tempo_segments(timing)
    # Collapse harmless rate jitter before bounding the number of classes.
    clusters=[]
    for row in sorted(segments,key=lambda row:row['bpm']):
        weight=row['range'][1]-row['range'][0]
        if clusters and abs(row['bpm']/clusters[-1]['bpm']-1)<=.06:
            old=clusters[-1];total=old['weight']+weight
            old.update(bpm=math.exp((math.log(old['bpm'])*old['weight']+math.log(row['bpm'])*weight)/total),
                       weight=total,maximum=row['bpm'])
        else:clusters.append({'bpm':row['bpm'],'minimum':row['bpm'],'maximum':row['bpm'],'weight':weight})
    while len(clusters)>count:
        index=min(range(len(clusters)-1),key=lambda i:math.log(clusters[i+1]['bpm']/clusters[i]['bpm']))
        left,right=clusters[index:index+2];weight=left['weight']+right['weight']
        clusters[index:index+2]=[{'bpm':math.exp((math.log(left['bpm'])*left['weight']+math.log(right['bpm'])*right['weight'])/weight),
                                 'minimum':left['minimum'],'maximum':right['maximum'],'weight':weight}]
    nearest=lambda row:min(range(len(clusters)),key=lambda i:abs(math.log(row['bpm']/clusters[i]['bpm'])))
    if v3:
        # The baseline is the tempo the song spends most time in, not the
        # middle rank: a three-second blip must not become the reference.
        held=[0.]*len(clusters)
        for row in segments:held[nearest(row)]+=row['range'][1]-row['range'][0]
        baseline=max(range(len(clusters)),key=lambda i:(held[i],-abs(i-(len(clusters)-1)/2),-i)) if clusters else None
    else:baseline=(len(clusters)-1)//2 if clusters else None
    span=max(baseline or 0,len(clusters)-1-(baseline or 0),1)
    buckets=[{'id':index,'bpm':row['bpm'],'range':[row['minimum'],row['maximum']],
              'offset':(index-baseline)/span,
              'factor':1+amplitude*(index-baseline)/span if enabled else 1.}
             for index,row in enumerate(clusters)]
    for row in segments:
        index=min(range(len(buckets)),key=lambda i:abs(math.log(row['bpm']/buckets[i]['bpm'])))
        row.update(bucket_id=index,factor=buckets[index]['factor'],offset=buckets[index]['offset'])
    # Many BPM estimates do not become many requests: adjacent equal buckets
    # share one generation condition and one source-clock span.
    merged=[]
    for row in segments:
        if merged and row['bucket_id']==merged[-1]['bucket_id'] and row['range'][0]==merged[-1]['range'][1]:
            old=merged[-1];weight=old['range'][1]-old['range'][0];extra=row['range'][1]-row['range'][0]
            intervals=old['interval_count']+row['interval_count'];supported=old['supported_intervals']+row['supported_intervals']
            old.update(bpm=(old['bpm']*weight+row['bpm']*extra)/(weight+extra),range=[old['range'][0],row['range'][1]],
                       interval_count=intervals,supported_intervals=supported,support_fraction=supported/intervals)
            if v3:old['observed_bpm']=(old['observed_bpm']*weight+row['observed_bpm']*extra)/(weight+extra)
        else:merged.append(dict(row))
    if merged:
        # Do not create a tiny, evidence-free decoder request at a fade-out.
        # Keep the last difficulty bucket through the remaining audible tail;
        # the measured range stays explicit and no extra beat is invented.
        merged[-1]['observed_end_sample']=merged[-1]['range'][1]
        merged[-1]['range']=[merged[-1]['range'][0],timing['source']['effective_end_sample']]
        merged[-1]['trailing_policy']='carry_last_difficulty_bucket_no_new_beats'
    result={'version':version,'enabled':enabled,'maximum_count':count,'count':len(buckets),
            'baseline_bucket_id':baseline,'maximum_density_offset':amplitude,
            'baseline_rule':'dominant_duration_bucket' if v3 else 'middle_ordered_bucket_lower_when_even',
            'buckets':buckets,'segments':merged,
            'observed_segments':segments,
            'source_adapter':timing.get('provenance',{}).get('adapter'),'timing_map_id':timing['id'],
            'status':'detected_local_pulses' if segments else 'insufficient_beats',
            'model_condition_scope':'bucket_actual_request','export_clock':'fixed-scroll-120-v1'}
    return result


def split_regions(regions,policy):
    """Respect manual regions/gaps while assigning every BPM change to a bucket."""
    result=[]
    for region in regions:
        a,b=region['start_sample'],region['end_sample']
        cuts=sorted({a,b,*[point for row in policy['segments'] for point in row['range'] if a<point<b]})
        for left,right in zip(cuts,cuts[1:]):
            tempo=next((row for row in policy['segments'] if row['range'][0]<=left<row['range'][1]),None)
            row={**region,'start_sample':left,'end_sample':right,
                 'bpm_bucket_id':tempo['bucket_id'] if tempo else None,
                 'detected_bpm':tempo['bpm'] if tempo else None,
                 'bpm_bucket_factor':tempo['factor'] if tempo else 1.,
                 'bpm_bucket_offset':tempo['offset'] if tempo else 0.}
            if left!=a:row['cut_reason']='bpm_bucket_change' if tempo else 'beat_evidence_end'
            result.append(row)
    return result


def summarize_range(policy,a,b):
    """Tempo facts for one user range: dominant bucket and a duration-weighted BPM.

    Weights are the seconds each folded segment overlaps [a, b); `observed_bpm`
    is only the transparency value and never the headline.
    """
    held={};bpm=observed=total=0.;ids=set()
    for row in policy.get('segments') or []:
        left,right=max(a,row['range'][0]),min(b,row['range'][1])
        if left>=right:continue
        weight=right-left;total+=weight;bpm+=row['bpm']*weight
        observed+=row.get('observed_bpm',row['bpm'])*weight
        held[row['bucket_id']]=held.get(row['bucket_id'],0)+weight;ids.add(row['bucket_id'])
    if not total:
        return {'bpm_bucket_id':None,'detected_bpm':None,'observed_bpm':None,'bucket_ids':[],'multi_tempo':False}
    buckets=policy['buckets']
    dominant=max(held,key=lambda i:(held[i],-abs(buckets[i]['offset']),-i))
    return {'bpm_bucket_id':dominant,'detected_bpm':bpm/total,'observed_bpm':observed/total,
            'bucket_ids':sorted(ids),'multi_tempo':len(ids)>1}


def assign_regions(regions,policy):
    """One user region is one condition: never re-split it at bucket boundaries."""
    result=[]
    for region in regions:
        summary=summarize_range(policy,region['start_sample'],region['end_sample'])
        bucket=policy['buckets'][summary['bpm_bucket_id']] if summary['bpm_bucket_id'] is not None else None
        result.append({**region,'bpm_bucket_id':summary['bpm_bucket_id'],'detected_bpm':summary['detected_bpm'],
                       'bpm_bucket_factor':bucket['factor'] if bucket else 1.,
                       'bpm_bucket_offset':bucket['offset'] if bucket else 0.,
                       'bpm_bucket_ids':summary['bucket_ids'],'bpm_multi_tempo':summary['multi_tempo']})
    return result
