"""Atomic timeline drafts and immutable version inheritance for the workbench."""
import copy
import math
from .advanced import SR, atomic, identifier, now, read, stats, uid, valid_events, version_source_metadata

MIN_SAMPLES = math.ceil(SR * .25)


def segmentation_preview(store, pid, payload):
    with store.lock:
        p = store.load(pid)
        store.check(p, payload.get('expected_revision'))
        plan_id = payload['plan_id']
        if not isinstance(plan_id, str) or len(plan_id) not in (32, 64) or any(c not in 'abcdef0123456789' for c in plan_id):
            raise ValueError('分析计划 ID 无效')
        plan = read(store.directory(pid)/'section-plans'/(plan_id+'.json'))
        if plan.get('samples') != p['samples'] or plan.get('source_pcm_sha') != p.get('source_pcm_sha256'):
            raise ValueError('分析不是当前完整原曲的结果，请重新分析')
        cuts = payload.get('cuts', [part['core'][1] for part in plan['sections'][:-1]])
        if not isinstance(cuts, list) or any(type(c) is not int for c in cuts) or len(set(cuts)) != len(cuts) or any(not 0 < c < p['samples'] for c in cuts):
            raise ValueError('建议边界须为原曲范围内不重复的整数采样点')
        # Existing boundaries always win; suggestions never extend a manual
        # segment into a deliberately omitted gap or alter its inclusion.
        parents = p['segments'] or [{'id': None, 'name': '片段', 'start_sample': 0, 'end_sample': p['samples'], 'included': True}]
        rows = []
        for parent in parents:
            a, b = parent['start_sample'], parent['end_sample']
            points = [a, *sorted(c for c in cuts if a < c < b), b]
            for left, right in zip(points, points[1:]):
                if right-left < MIN_SAMPLES:
                    raise ValueError('边界过近；每段至少 250 ms，请移动或合并建议边界')
                rows.append({'parent_id': parent['id'], 'start_sample': left, 'end_sample': right,
                             'name': parent['name'] if len(points) == 2 and parent['id'] else f'片段 {len(rows)+1}',
                             'included': parent.get('included', True)})
        selected = payload.get('included')
        if selected is not None:
            if not isinstance(selected, list) or len(selected) != len(rows) or any(type(x) is not bool for x in selected):
                raise ValueError('片段选择须与建议区间逐项对应')
            for row, included in zip(rows, selected): row['included'] = included
        draft = {'id': uid(), 'created': now(), 'project_revision': p['revision'], 'plan_id': plan['id'],
                 'cuts': sorted(cuts), 'segments': rows, 'preserved_boundaries': sorted({s[k] for s in p['segments'] for k in ('start_sample', 'end_sample')}),
                 'previous_count': len(p['segments']), 'proposed_count': len(rows)}
        atomic(store.directory(pid)/'segmentation-drafts'/(draft['id']+'.json'), draft)
        return draft


def segmentation_apply(store, pid, payload, tasks=()):
    from .advanced_history import perform
    def apply():
        p = store.load(pid)
        store.check(p, payload.get('expected_revision'))
        draft = read(store.directory(pid)/'segmentation-drafts'/(identifier(payload['draft_id'])+'.json'))
        store.check(p, draft['project_revision'])
        affected = {s['id'] for s in p['segments'] if any(r['parent_id'] == s['id'] and
                    [r['start_sample'], r['end_sample']] != [s['start_sample'], s['end_sample']] for r in draft['segments'])}
        if any(t.get('segment_id') in affected and t.get('status') in ('queued', 'paused', 'running') for t in tasks):
            raise ValueError('受影响片段尚未完成生成；请等待运行任务完成或取消排队任务后应用划段')
        before = copy.deepcopy(p['segments'])
        children, files = [], []
        for row in draft['segments']:
            parent = next((s for s in before if s['id'] == row['parent_id']), None)
            a, b = row['start_sample'], row['end_sample']
            if parent and [a, b] == [parent['start_sample'], parent['end_sample']]:
                child = copy.deepcopy(parent); child['included'] = row['included']; children.append(child); continue
            child = {'id': uid(), 'name': row['name'], 'start_sample': a, 'end_sample': b, 'included': row['included'],
                     'overrides': copy.deepcopy(parent.get('overrides', {})) if parent else {}, 'versions': {}, 'active': {},
                     'boundary_source': {'type': 'analysis', 'plan_id': draft['plan_id'], 'parent_id': row['parent_id']}}
            if parent:
                # All matching candidates survive subdivision, not just the
                # adopted chart. Absolute event times and crossing hold tails
                # remain untouched; only half-open head ownership is filtered.
                for variant, versions in parent['versions'].items():
                    for old in versions:
                        if old.get('range') != [parent['start_sample'], parent['end_sample']]: continue
                        r = copy.deepcopy(store.revision(pid, old['id']))
                        r.update(id=uid(), created=now(), segment_id=child['id'], range=[a, b], kind='split',
                                 events=[e for e in r['events'] if a*1000/SR <= e['start_ms'] < b*1000/SR],
                                 review={'status': 'unreviewed', 'reason': ''})
                        r['provenance'] = {**r.get('provenance', {}), 'parent': old['id'], 'parent_kind': old['kind'], 'segmentation_draft': draft['id']}
                        valid_events(r['events'], a*1000/SR, b*1000/SR, p['duration']*1000)
                        r['stats'] = stats(r['events'], a*1000/SR, b*1000/SR)
                        files.append(r)
                        summary = {k: r[k] for k in ('id', 'kind', 'created', 'stats', 'range', 'review')}
                        summary.update(engine=r['settings']['engine'], seed=r['provenance'].get('seed'), parent_revisions=[old['id']])
                        summary.update(version_source_metadata(r))
                        child['versions'].setdefault(variant, []).append(summary)
                        if parent['active'].get(variant) == old['id']: child['active'][variant] = r['id']
            children.append(child)
        for r in files: atomic(store.directory(pid)/'revisions'/(r['id']+'.json'), r)
        # Archive is independent of the limited undo stack: even old, stale
        # candidate metadata remains discoverable after repeated edits.
        atomic(store.directory(pid)/'layouts'/(draft['id']+'.json'), {'id': draft['id'], 'created': now(), 'segments': before})
        p['segments'] = children
        return store.save(p)
    return perform(store, pid, apply, '应用建议划段')


def select_many(store, pid, payload):
    with store.lock:
        p = store.load(pid); store.check(p, payload.get('expected_revision'))
        choices = payload.get('choices')
        if not isinstance(choices, list) or not choices: raise ValueError('请选择要采用的方案')
        seen = set()
        for choice in choices:
            s = store.segment(p, choice['segment_id']); r = store.revision(pid, choice['revision_id']); variant = choice['variant']
            if (s['id'], variant) in seen: raise ValueError('同一片段组合不能重复采用')
            seen.add((s['id'], variant))
            if r['segment_id'] != s['id'] or r['variant'] != variant or r['range'] != [s['start_sample'], s['end_sample']]:
                raise ValueError('候选属于其他片段或范围已变化，请刷新后选择')
            s['active'][variant] = r['id']
        return store.save(p)
