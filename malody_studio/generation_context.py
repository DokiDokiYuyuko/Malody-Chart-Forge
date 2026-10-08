"""Frozen continuous model passes with unchanged user-owned region boundaries."""
import copy
from .advanced import SR
from .section_plan import canonical_hash

VERSION='continuous-model-pass-v1'


def contract():
    return {'version':VERSION,'model_condition_scope':'requested_difficulty_base',
            'regional_intensity_scope':'frozen_selection_budget',
            'decoder_scope':'continuous_compatible_span','ownership':'original_half_open_core'}


def continuous(snapshot):
    policy=snapshot.get('generation_context_policy')
    if not policy:return False
    if policy!=contract():raise ValueError('冻结连续推理策略版本或内容不受支持')
    parts=snapshot.get('member_segments')
    if parts:
        cursor=snapshot['segment']['start_sample'];seen=set()
        for part in parts:
            if part.get('id') in seen or part['start_sample']!=cursor or part['end_sample']<=cursor:
                raise ValueError('连续推理区域须唯一且完整连续覆盖冻结范围')
            seen.add(part.get('id'));cursor=part['end_sample']
        if cursor!=snapshot['segment']['end_sample']:raise ValueError('连续推理区域未覆盖冻结范围')
    return True


def members(snapshot):
    return snapshot.get('member_segments') or [snapshot.get('segment',{})]


def group_entries(entries):
    """Merge only frozen-compatible adjacent regions; never change project data."""
    groups=[]
    for options,source in entries:
        snapshot=options['_advanced']
        if not continuous(snapshot):
            groups.append([(options,source)]);continue
        signature=canonical_hash({key:value for key,value in snapshot.items()
            if key not in ('segment','silent_core_ids','silence_verified','member_segments')})
        previous=groups[-1][-1] if groups else None
        if previous:
            old=previous[0]['_advanced']
            compatible=(continuous(old) and previous[1]==source
                and old['segment']['end_sample']==snapshot['segment']['start_sample']
                and signature==canonical_hash({key:value for key,value in old.items()
                    if key not in ('segment','silent_core_ids','silence_verified','member_segments')}))
        else:compatible=False
        if compatible:groups[-1].append((options,source))
        else:groups.append([(options,source)])
    result=[]
    for group in groups:
        options,source=copy.deepcopy(group[0]);snapshot=options['_advanced']
        if continuous(snapshot):
            parts=[copy.deepcopy(item[0]['_advanced']['segment']) for item in group]
            snapshot['member_segments']=parts
            snapshot['segment']={**parts[0],'start_sample':parts[0]['start_sample'],
                                 'end_sample':parts[-1]['end_sample'],
                                 'name':'连续推理 · '+str(len(parts))+' 个区域'}
            snapshot['silence_verified']=all(item[0]['_advanced'].get('silence_verified',False) for item in group)
            snapshot['silent_core_ids']=list(dict.fromkeys(core for item in group
                for core in item[0]['_advanced'].get('silent_core_ids',[])))
        result.append((options,source))
    return result


def selected_model_sources(original,result):
    """Resolve this candidate's selected raw ancestors, excluding other trials."""
    if original['kind'] in ('model_raw','stem_raw'):return [original]
    by_id={row['id']:row for row in result['advanced_result'] if row.get('id')}
    found={};seen=set()
    def visit(row):
        key=row.get('id')
        if key in seen:return
        seen.add(key)
        if row['kind'] in ('model_raw','stem_raw'):
            found[key]=row;return
        provenance=row.get('provenance',{})
        parents=set(provenance.get('parents') or [])
        if provenance.get('parent'):parents.add(provenance['parent'])
        # Declared parents identify the whole selected supply, including unused
        # voices. Origins alone describe surviving heads and are only a fallback
        # for older candidates without a declared parent set.
        if not parents:
            parents.update(origin['revision_id'] for event in row.get('events',[])
                           for origin in event.get('origins',[]) if origin.get('revision_id'))
        for parent in parents:
            if parent in by_id:visit(by_id[parent])
    visit(original)
    return list(found.values())


def owned_rows(snapshot,result):
    """Partition final output after one span budget, retaining model ancestry."""
    if not snapshot.get('member_segments'):
        return [(snapshot['segment'],row,result['bounds']) for row in result['advanced_result']]
    rows=[]
    all_ids={}
    original_heads={}
    for part in members(snapshot):
        core=[part['start_sample'],part['end_sample']]
        a,b=(value*1000/SR for value in core)
        for original in result['advanced_result']:
            if not original.get('id'):continue
            rid=(original['id'] if len(members(snapshot))==1 else
                 canonical_hash({'span_revision':original['id'],'segment':part['id'],'range':core})[:32])
            all_ids[(original['id'],part['id'])]=rid
            for event in original['events']:
                if a<=event['start_ms']<b:original_heads[(original['id'],event['id'])]=rid
    for part in members(snapshot):
        bounds=[part['start_sample'],part['end_sample']]
        a,b=(value*1000/SR for value in bounds)
        ids={row['id']:all_ids[(row['id'],part['id'])]
             for row in result['advanced_result'] if row.get('id')}
        for original in result['advanced_result']:
            row=copy.deepcopy(original)
            row['events']=[event for event in row['events'] if a<=event['start_ms']<b]
            if row.get('id'):row['id']=ids[row['id']]
            for event in row['events']:
                for origin in event.get('origins',[]):
                    source=(origin.get('revision_id'),origin.get('note_id'))
                    if source in original_heads:origin['revision_id']=original_heads[source]
                    elif origin.get('revision_id') in ids:origin['revision_id']=ids[origin['revision_id']]
            provenance=row['provenance']
            provenance.update(generation_context_policy=copy.deepcopy(snapshot['generation_context_policy']),
                inference_span=[snapshot['segment']['start_sample'],snapshot['segment']['end_sample']],
                member_segment_id=part['id'],owned_range=bounds)
            for name in ('parent','parents'):
                if name=='parents' and isinstance(provenance.get(name),list):
                    provenance[name]=[ids.get(value,value) for value in provenance[name]]
                elif provenance.get(name) in ids:provenance[name]=ids[provenance[name]]
            if 'core_conditions' in provenance:
                provenance['core_conditions']=[record for record in provenance['core_conditions']
                    if record['core'][0]<bounds[1] and record['core'][1]>bounds[0]]
            selected_sources=selected_model_sources(original,result)
            model_heads=(sum(sum(a<=event['start_ms']<b for event in raw['events'])
                for raw in selected_sources) if selected_sources else None)
            if provenance.get('direct_v32_policy') and provenance.get('density_validation'):
                from .advanced import as_notes
                from .nps_star_calibration import chart_span_density
                previous=provenance['density_validation']
                provenance['span_density_validation']=copy.deepcopy(previous)
                provenance['density_validation']=chart_span_density(as_notes(row['events']),previous['target_range'])
            elif provenance.get('density_validation'):
                from .density_validation import evaluate
                previous=provenance['density_validation']
                provenance['span_density_validation']=copy.deepcopy(previous)
                report=evaluate(row['events'],snapshot.get('section_plan'),row['variant'].split('--')[-1],bounds,
                    candidates={'model_heads':model_heads,'candidate_heads':model_heads,
                                'constraint_removed':max(0,model_heads-len(row['events'])) if model_heads is not None else 0},
                    attempts=previous.get('attempts',0))
                if model_heads is not None and report.get('target_heads') and model_heads<report['target_heads']*.85:
                    report=evaluate(row['events'],snapshot.get('section_plan'),row['variant'].split('--')[-1],bounds,
                        candidates={'model_heads':model_heads,'candidate_heads':model_heads,'model_supply_failure':True},
                        attempts=previous.get('attempts',0))
                report['retry_scope']='continuous_parent_span'
                provenance['density_validation']=report
            if provenance.get('quality_summary'):
                from .chart_quality import summarize_region
                provenance['span_quality_summary']=copy.deepcopy(provenance['quality_summary'])
                provenance['span_quality_decisions']=copy.deepcopy(provenance.get('quality_decisions',[]))
                checked=summarize_region(row['events'],provenance.get('quality_decisions',[]),
                    ln_target=provenance['quality_summary'].get('ln_target',row.get('settings',{}).get('ln_ratio',.15)))
                provenance['quality_summary']=checked['summary']
                provenance['quality_decisions']=checked['decisions']
            provenance['budget_partition_only']=True
            rows.append((part,row,bounds))
    return rows
