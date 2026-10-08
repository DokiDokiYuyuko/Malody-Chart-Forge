"""Read native rhythm proof for legacy revisions without rewriting their files."""
import copy
import math
from pathlib import Path

from .advanced import SR, identifier, read
from .charts import Note
from .difficulty import PRESETS
from . import v32_rhythm


def _frozen_cap(owner, start, plan):
    key=owner['variant'].split('--')[-1]
    cap=owner.get('settings',{}).get('difficulty_rules',{}).get(key,{}).get('hold_ms',PRESETS[key]['hold_ms'])
    for section in (plan or {}).get('sections',[]):
        core=section.get('core') or section.get('range') or section.get('source_range')
        sr=(plan or {}).get('sample_rate',SR)
        if core and core[0]*1000/sr<=start<core[1]*1000/sr:
            detail=(section.get('per_difficulty') or section.get('perDifficulty') or {}).get(key,{})
            cap=detail.get('hard_caps',{}).get('hold_max_ms',cap)
            break
    return float(cap)


def hydrate_events(store, pid, events, *, owners=None, plan=None, root=None):
    """Hydrate derived copies using exact immutable raw-head ownership only."""
    if root is None:
        from .paths import ROOT
        root=ROOT
    root=Path(root);output=copy.deepcopy(events);sources={};caches={}
    for event in output:
        existing=copy.deepcopy(event.get('model_rhythm',{}))
        rhythm=v32_rhythm.unavailable('raw_origin_missing');matched=None
        # Fusion stores its actual representative first, then leakage copies.
        # A missing representative cannot borrow another copy's native rhythm.
        origins=event.get('origins') or []
        origin=origins[0] if origins else {}
        try:
            rid=identifier(origin['revision_id'])
            if rid not in sources:sources[rid]=store.revision(pid,rid)
            revision=sources[rid]
            if revision.get('kind') not in ('stem_raw','model_raw') or revision.get('provenance',{}).get('global_budget_applied'):
                rhythm=v32_rhythm.unavailable('origin_is_not_immutable_model_supply')
            else:
                start=float(origin['original_start_ms']);lane=int(origin['original_lane'])
                raw=[row for row in revision['events'] if row['start_ms']==start and row['lane']==lane
                     and (origin.get('note_id') is None or row['id']==origin['note_id'])]
                if len(raw)!=1:
                    rhythm=v32_rhythm.unavailable('exact_raw_head_missing_or_ambiguous')
                else:
                    matched=raw[0]
                    if matched.get('model_rhythm',{}).get('available'):
                        rhythm=copy.deepcopy(matched['model_rhythm'])
                    else:
                        prov=revision.get('provenance',{});job=identifier(prov['cache_job'])
                        directory=(root/'outputs'/job).resolve();path=(directory/prov['cache_file']).resolve()
                        if not path.is_relative_to(directory):raise ValueError('cache_path_outside_job')
                        key=revision['variant'].split('--')[-1];cache_key=(str(path),key)
                        if cache_key not in caches:
                            cache=read(path)
                            if key not in cache.get('raw',{}):raise ValueError('raw_cache_difficulty_missing')
                            notes=[Note(*values) for values in cache['raw'][key]]
                            v32_rhythm.hydrate_cache(cache,{key:notes},directory)
                            caches[cache_key]=notes
                        exact=[note for note in caches[cache_key] if note.start==start and note.lane==lane]
                        rhythm=copy.deepcopy(getattr(exact[0],'model_rhythm',v32_rhythm.unavailable('native_head_missing'))) if len(exact)==1 else v32_rhythm.unavailable('exact_cache_head_missing_or_ambiguous')
                    rhythm['source_role']=revision.get('provenance',{}).get('source_role',origin.get('stem_role'))
                    rhythm['raw_revision_id']=rid
                    rhythm['raw_note_id']=matched['id']
        except (KeyError,ValueError,TypeError,OSError,OverflowError):
            rhythm=v32_rhythm.unavailable('native_revision_cache_missing_or_invalid')
        event['model_rhythm']=existing if existing.get('available') else rhythm
        owner=(owners or {}).get(event['id'])
        owner=owner[0] if isinstance(owner,tuple) else owner
        if matched is not None and owner and owner.get('kind')=='fusion' and owner.get('provenance',{}).get('fusion_version') and not event.get('tail_policy'):
            try:
                original_start=float(matched['start_ms']);model_end=float(matched['end_ms'])
                tail=float(event['end_ms']);cap=_frozen_cap(owner,original_start,plan)
                if math.isfinite(cap) and cap>=100 and model_end>tail and event['start_ms']==original_start and abs(tail-(original_start+cap))<1e-6:
                    event['tail_policy']={'kind':'rule_cap','model_end_ms':model_end,'cap_ms':cap,
                        'uncapped_start_ms':original_start,'proof':'legacy_fusion_exact_recorded_cap'}
            except (KeyError,ValueError,TypeError,OverflowError):pass
    return output
