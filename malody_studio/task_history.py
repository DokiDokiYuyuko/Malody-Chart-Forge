"""Generation activity records and exact links to immutable candidate results."""
from __future__ import annotations

import os
import copy
from datetime import datetime, timezone
from pathlib import Path

from .advanced import identifier, read, version_source_metadata

TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}
TYPES = {'', 'advanced', 'song', 'separation', 'separation_trial', 'music_analysis'}


def _snapshot(job):
    return job.get('options', {}).get('_advanced', {})


def history_snapshot(jobs):
    """Copy list-view fields under the job lock, without frozen inference payloads."""
    rows = []
    for job in jobs:
        row = {key: job[key] for key in ('id', 'title', 'artist', 'status', 'message',
            'created', 'queue_order', 'error', 'advanced_errors', 'advanced_warnings', 'advanced_revisions',
            'advanced_reused_revisions', 'stem_set_id', '_generation_region_view') if key in job}
        row['report'] = {'partial': job.get('report', {}).get('partial', False)}
        snapshot = _snapshot(job)
        if snapshot:
            def select(value, keys):
                return {key: value[key] for key in keys if key in value}
            part_keys = ('id', 'name', 'start_sample', 'end_sample')
            light = select(snapshot, ('task_type', 'batch_id', 'auto_fuse', 'stem_set_id'))
            light['project'] = select(snapshot.get('project', {}), ('id', 'title', 'artist'))
            light['segment'] = select(snapshot.get('segment', {}), part_keys)
            light['member_segments'] = [select(part, part_keys) for part in snapshot.get('member_segments', [])]
            light['variants'] = [select(v, ('key',)) for v in snapshot.get('variants', [])]
            light['settings'] = select(snapshot.get('settings', {}), ('source_mode',))
            for key in ('source', 'input_sources'):
                if key in snapshot:
                    source_keys = ('source_id', 'source_role', 'role')
                    light[key] = ([select(source, source_keys) for source in snapshot[key]]
                                  if key == 'input_sources' else select(snapshot[key], source_keys))
            row['options'] = {'_advanced': light}
        rows.append(copy.deepcopy(row))
    return rows


def _region_jobs(jobs):
    views=[]
    for job in jobs:
        parts=_snapshot(job).get('member_segments')
        if not parts or job.get('_generation_region_view'):views.append(job);continue
        for part in parts:
            view={**job, 'options': {**job.get('options', {}),
                '_advanced': {**_snapshot(job), 'segment': dict(part)}}}
            view['_generation_region_view']=True
            views.append(view)
    return views


def _view_key(job):
    return (job['id'],_snapshot(job).get('segment',{}).get('id'))


def _time(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _type(job):
    snapshot = _snapshot(job)
    if not snapshot:
        return 'song'
    return 'advanced' if snapshot.get('task_type', 'generation') == 'generation' else snapshot.get('task_type')


def _identity(job):
    snapshot = _snapshot(job)
    if _type(job) == 'advanced' and snapshot.get('batch_id'):
        return 'batch:' + snapshot['project']['id'] + ':' + snapshot['batch_id']
    return 'job:' + job['id']


def _groups(jobs):
    groups = {}
    for job in jobs:
        groups.setdefault(_identity(job), []).append(job)
    return groups


def _sources(job):
    snapshot = _snapshot(job)
    rows = snapshot.get('input_sources') or ([snapshot['source']] if snapshot.get('source') else [])
    return [{'source_id': row.get('source_id'), 'source_role': row.get('source_role', row.get('role', 'mix'))}
            for row in rows]


def _auto_fuse(job):
    snapshot = _snapshot(job)
    return bool(snapshot.get('auto_fuse', not bool(snapshot.get('input_sources'))
                and snapshot.get('settings', {}).get('source_mode') == 'vocals_accompaniment'))


def _source_label(job):
    roles = {row['source_role'] for row in _sources(job)}
    if roles == {'vocals', 'accompaniment'} or _auto_fuse(job):
        return '人声 + 伴奏 → 合并谱面' if _auto_fuse(job) else '人声 + 伴奏'
    if roles == {'vocals'}:
        return '人声'
    if roles == {'accompaniment'}:
        return '伴奏'
    if _type(job) in ('separation', 'separation_trial'):
        return '局部人声与伴奏分离' if _type(job) == 'separation_trial' else '完整原曲分离'
    return '原曲'


def _primary_rows(job, rows):
    if _auto_fuse(job):
        return [row for row in rows if row['kind'] in ('fusion', 'fusion_candidate')]
    roles = {row['source_role'] for row in _sources(job)}
    if roles in ({'vocals'}, {'accompaniment'}):
        finished=[row for row in rows if row['source_role'] in roles and row['kind'] in ('quality','arranged','rules','fast')]
        return finished or [row for row in rows if row['source_role'] in roles and row['kind'] == 'stem_raw']
    playable = [row for row in rows if row['kind'] in ('rules', 'fast', 'adaptive', 'arranged', 'silence', 'quality')]
    budgeted={row['variant'] for row in playable if row.get('global_budget_applied') or row['kind']!='quality'}
    playable=[row for row in playable if row['kind']!='quality' or row.get('global_budget_applied') or row['variant'] not in budgeted]
    keys = {row['variant'] for row in playable}
    return playable + [row for row in rows if row['source_role'] not in ('vocals', 'accompaniment')
                       and row['kind'] != 'stem_raw' and row['variant'] not in keys]


def _latest_variants(jobs):
    attempts = {}
    for job in sorted(jobs, key=lambda item: (_time(item.get('created')), item.get('queue_order', 0), item['id'])):
        segment = _snapshot(job).get('segment', {})
        for variant in _snapshot(job).get('variants', []):
            slot = (segment.get('id'), segment.get('start_sample'), segment.get('end_sample'), variant['key'])
            attempts[slot] = job['id']
    return {_view_key(job): [variant['key'] for variant in _snapshot(job).get('variants', [])
                       if attempts.get((_snapshot(job).get('segment', {}).get('id'),
                                        _snapshot(job).get('segment', {}).get('start_sample'),
                                        _snapshot(job).get('segment', {}).get('end_sample'), variant['key'])) == job['id']]
            for job in jobs}


def candidate_results(store, project, jobs):
    """Job-owned revision IDs include reused files; historical batch labels do not."""
    jobs=_region_jobs(jobs)
    pid = project['id']
    current = {segment['id']: segment for segment in project.get('segments', [])}
    variants = {variant['key'] for variant in project.get('variants', [])}
    manifest_versions = [version for segment in project.get('segments', [])
                         for versions in segment.get('versions', {}).values() for version in versions]
    latest = _latest_variants(jobs)
    results = []; revision_cache = {}
    for job in jobs:
        snapshot = _snapshot(job)
        segment = snapshot.get('segment', {})
        expected_range = [segment.get('start_sample'), segment.get('end_sample')]
        revision_ids = list(job.get('advanced_revisions') or [])
        # Recovery after a commit completed but before the job status was written.
        revision_ids.extend(version['id'] for version in manifest_versions
                            if version.get('generation_job_id') == job['id'])
        rows = []
        for rid in dict.fromkeys(revision_ids):
            try:
                if rid not in revision_cache:
                    full = store.revision(pid, identifier(rid))
                    # Continuous jobs expose the same immutable IDs in every
                    # region. Keep small readback metadata, not repeated copies
                    # of full model notes and inference-attempt provenance.
                    revision_cache[rid] = {key:full[key] for key in
                        ('id','segment_id','range','variant','kind','stats','created') if key in full}
                    revision_cache[rid]['source_metadata'] = version_source_metadata(full)
                    revision_cache[rid]['provenance'] = {key:full['provenance'][key]
                        for key in ('density_validation','global_budget_applied') if key in full.get('provenance',{})}
                revision = revision_cache[rid]
            except (ValueError, OSError):
                continue
            if (revision.get('segment_id') != segment.get('id') or revision.get('range') != expected_range
                    or revision.get('variant') not in {variant['key'] for variant in snapshot.get('variants', [])}):
                continue
            metadata = revision['source_metadata']
            part = current.get(revision['segment_id'])
            current_range = bool(part and revision['range'] == [part['start_sample'], part['end_sample']])
            reused = rid in job.get('advanced_reused_revisions', []) or bool(
                metadata.get('generation_job_id') and metadata['generation_job_id'] != job['id']) or bool(
                metadata.get('cache_job') and metadata['cache_job'] != job['id'])
            rows.append({'segment_id': revision['segment_id'], 'segment_name': segment.get('name', '旧片段'),
                         'variant': revision['variant'], 'revision_id': rid, 'kind': revision.get('kind'),
                         'source_role': metadata['source_role'], 'source_id': metadata['source_id'],
                         'stem_set_id': metadata.get('stem_set_id'), 'stats': revision.get('stats', {}),
                         'density_validation':revision.get('provenance',{}).get('density_validation'),
                         'global_budget_applied':revision.get('provenance',{}).get('global_budget_applied',False),
                         'range': revision['range'], 'current_range': current_range,
                         'adoptable': current_range and revision['variant'] in variants,
                         'job_id': job['id'], 'reused': reused, 'cached': reused,
                         'created': revision.get('created'), 'primary': False,
                         'current_attempt': revision['variant'] in latest[_view_key(job)]})
        primary = {row['revision_id'] for row in _primary_rows(job, rows)}
        for row in rows:
            row['primary'] = row['revision_id'] in primary and row['current_attempt']
        results.extend(rows)
    return results


def _job_summary(job, rows, latest_variants=None):
    snapshot = _snapshot(job)
    segment = snapshot.get('segment', {})
    raw_status = job.get('status', 'interrupted')
    status = raw_status
    # Non-fatal notices (e.g. an empty contribution of a silent stem) are never failures.
    notices = list(job.get('advanced_warnings') or [])
    errors = []
    for item in job.get('advanced_errors') or []:
        (notices if isinstance(item, dict) and item.get('non_fatal') else errors).append(item)
    if job.get('error'):
        errors.append({'error': job['error']})
    chosen = [variant['key'] for variant in snapshot.get('variants', [])]
    latest_variants = chosen if latest_variants is None else latest_variants
    superseded = _type(job) == 'advanced' and bool(chosen) and not latest_variants
    if raw_status == 'completed' and not superseded:
        if _type(job) == 'advanced':
            delivered = {row['variant'] for row in rows if row['primary']}
            missing = set(latest_variants) - delivered
            if errors or not delivered or missing:
                status = 'partial'
            for key in sorted(missing):
                errors.append({'variant': key, 'error': '该组合未生成合并谱面' if _auto_fuse(job) else '该组合未生成可用谱面'})
        elif _type(job) == 'song' and job.get('report', {}).get('partial'):
            status = 'partial'
    error = '；'.join(str(item.get('error') or item.get('summary') or '') for item in errors if isinstance(item, dict))
    return {'id': job['id'], 'job_id': job['id'], 'title': job.get('title', ''),
            'status': status, 'raw_status': raw_status, 'message': job.get('message', ''),
            'created': job.get('created'), 'segment_id': segment.get('id'),
            'segment_name': segment.get('name'), 'variants': chosen,
            'latest_variants': latest_variants, 'superseded': superseded,
            'range': [segment.get('start_sample'), segment.get('end_sample')] if segment else None,
            'result_count': len(rows), 'primary_result_count': sum(row['primary'] for row in rows),
            'error_count': len(errors), 'errors': errors, 'error': error,
            'warning_count': len(notices), 'warnings': notices,
            'stem_set_id': job.get('stem_set_id') or snapshot.get('stem_set_id'),
            'source_label': _source_label(job), 'input_sources': _sources(job), 'auto_fuse': _auto_fuse(job),
            'density_validation':[row.get('density_validation') for row in rows if row['primary']]}


def _counts(summaries):
    summaries = [row for row in summaries if not row['superseded']]
    counts = {'total': len(summaries), 'completed': 0, 'failed': 0, 'active': 0,
              'partial': 0, 'cancelled': 0, 'interrupted': 0}
    for row in summaries:
        key = row['status'] if row['status'] in counts and row['status'] != 'total' else 'active'
        counts[key] += 1
    return counts


def _status(counts):
    if counts['active']:
        return 'running'
    if counts['completed'] == counts['total']:
        return 'completed'
    if counts['partial'] or counts['completed']:
        return 'partial'
    if counts['failed']:
        return 'failed'
    if counts['interrupted']:
        return 'interrupted'
    return 'cancelled'


def _manifest_jobs(store, pid, bid, jobs):
    identifier(pid); identifier(bid)
    members = [job for job in jobs if _snapshot(job).get('project', {}).get('id') == pid
               and _type(job) == 'advanced' and (_snapshot(job).get('batch_id') == bid
               or (job['id'] == bid and not _snapshot(job).get('batch_id')))]
    for path in (store.directory(pid) / 'batches').glob('*.json'):
        manifest = read(path)
        if manifest.get('id') != bid:
            continue
        accepted = {row['id'] for row in manifest.get('jobs', [])}
        members.extend(job for job in jobs if job['id'] in accepted and job not in members
                       and _snapshot(job).get('project', {}).get('id') == pid and _type(job) == 'advanced')
        break
    if not members:
        raise ValueError('生成批次不存在，或任务记录已删除')
    return sorted(members, key=lambda job: (_snapshot(job).get('segment', {}).get('start_sample', 0), job['id']))


def batch_detail(store, pid, bid, jobs):
    project = store.load(identifier(pid))
    members = _region_jobs(_manifest_jobs(store, pid, bid, jobs))
    results = candidate_results(store, project, members)
    latest = _latest_variants(members)
    summaries = [_job_summary(job, [row for row in results if row['job_id'] == job['id'] and row['segment_id']==_snapshot(job).get('segment',{}).get('id')], latest[_view_key(job)]) for job in members]
    counts = _counts(summaries)
    return {'id': bid, 'project_id': pid, 'title': project['title'], 'status': _status(counts),
            'counts': counts, 'jobs': summaries, 'results': results,
            'created': min((job.get('created', '') for job in members), key=_time),
            'source_label': _source_label(members[0]), 'auto_fuse': _auto_fuse(members[0]),
            'model_job_count':len({job['id'] for job in members})}


def _metadata(record_id, members):
    first = members[0]
    snapshot = _snapshot(first)
    kind = _type(first)
    return {'id': record_id, 'record_id': record_id, 'type': kind,
            'title': snapshot.get('project', {}).get('title', first.get('title', '')),
            'artist': snapshot.get('project', {}).get('artist', first.get('artist', '')),
            'created': min((job.get('created', '') for job in members), key=_time),
            'project_id': snapshot.get('project', {}).get('id'),
            'batch_id': (snapshot.get('batch_id') or first['id']) if kind == 'advanced' else None,
            'job_id': first['id'] if len(members) == 1 else None, 'source_label': _source_label(first)}


def _record(store, record_id, members):
    record = _metadata(record_id, members)
    members=_region_jobs(members)
    results = []
    if record['type'] == 'advanced':
        try:
            results = candidate_results(store, store.load(record['project_id']), members)
        except (ValueError, OSError):
            pass
    latest = _latest_variants(members)
    summaries = [_job_summary(job, [row for row in results if row['job_id'] == job['id'] and row['segment_id']==_snapshot(job).get('segment',{}).get('id')], latest[_view_key(job)]) for job in members]
    counts = _counts(summaries)
    record.update(created_at=record['created'], status=_status(counts), counts=counts, jobs=summaries,
                  section_count=len({row['segment_id'] for row in summaries if row['segment_id']}),
                  combination_count=len({key for row in summaries for key in row['variants']}),
                  input_sources=_sources(members[0]), success_count=counts['completed'],
                  failed_count=counts['failed'] + counts['interrupted'], partial_count=counts['partial'],
                  result_count=len(results), primary_result_count=sum(row['primary'] for row in results),
                  stem_set_id=summaries[0]['stem_set_id'],model_job_count=len({job['id'] for job in members}))
    if record['type'] == 'song':
        record['result_target'] = {'type': 'job', 'job_id': members[0]['id']}
    else:
        record['result_target'] = {'type': 'advanced', 'project_id': record['project_id'],
                                   'batch_id': record['batch_id'], 'job_id': record['job_id'],
                                   'mode': 'results' if record['type'] == 'advanced' else 'prepare',
                                   'stem_set_id': record['stem_set_id'], 'task_type': record['type']}
    return record


def history_page(store, jobs, page=1, page_size=10, query='', kind='', project_id=''):
    if kind not in TYPES:
        raise ValueError('完成记录类型无效')
    if project_id:
        identifier(project_id)
    groups = {key: members for key, members in _groups(jobs).items()
              if all(job.get('status') in TERMINAL for job in members)}
    needle = query.strip().casefold()
    matching = []
    for key, members in groups.items():
        meta = _metadata(key, members)
        if project_id and meta['project_id'] != project_id:
            continue
        if kind and meta['type'] != kind and not (kind == 'separation' and meta['type'] == 'separation_trial'):
            continue
        if needle and needle not in (meta['title'] + ' ' + meta['artist'] + ' ' + meta['source_label']).casefold():
            continue
        matching.append((meta, members))
    matching.sort(key=lambda entry: (_time(entry[0]['created']), entry[0]['id']), reverse=True)
    total = len(matching)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    return {'items': [_record(store, meta['id'], members) for meta, members in matching[(page-1)*page_size:page*page_size]],
            'page': page, 'pages': pages, 'total_pages': pages, 'page_size': page_size,
            'total': total, 'total_all': len(groups), 'filters': {'q': query, 'type': kind, 'project_id': project_id}}


def open_folder(store, root, jobs, payload):
    if set(payload) - {'record_id', 'job_id'}:
        raise ValueError('仅支持通过任务记录打开文件夹')
    groups = _groups(jobs)
    record_id, job_id = payload.get('record_id'), payload.get('job_id')
    if record_id:
        members = groups.get(record_id)
        if not members:
            raise ValueError('任务记录不存在')
        if job_id and not any(job['id'] == job_id for job in members):
            raise ValueError('任务不属于所选记录')
    elif job_id:
        identifier(job_id)
        members = [job for job in jobs if job['id'] == job_id]
        if not members:
            raise ValueError('任务记录不存在')
        record_id = _identity(members[0])
    else:
        raise ValueError('请选择任务记录')
    root = Path(root).resolve()
    outputs = (root / 'outputs').resolve()
    snapshot = _snapshot(members[0])
    if job_id or not snapshot:
        target = outputs / identifier(job_id or members[0]['id'])
    else:
        target = store.directory(identifier(snapshot['project']['id']))
    target = target.resolve()
    if not target.is_relative_to(root) or not target.is_relative_to(outputs):
        raise ValueError('结果目录越过项目输出目录')
    if not target.is_dir():
        raise ValueError('结果目录不存在')
    from .folder_open import open_registered_directory
    open_registered_directory(target)
    return {'opened': True, 'record_id': record_id, 'job_id': job_id}
