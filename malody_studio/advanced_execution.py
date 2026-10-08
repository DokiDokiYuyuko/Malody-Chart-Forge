"""Count recorded model passes, including coalesced requests and bounded retries."""
from .advanced import read
import re
from pathlib import Path


def spent_retry_rounds(record, metadata=None):
    """Count attempted rounds, even when their output failed validation."""
    attempts = record.get('attempts', [])
    explicit = [int(a['retry']) for a in attempts if a.get('retry') is not None]
    count = max(explicit, default=max(0, len(attempts)-1))
    count = max(count, int(record.get('spent_retry_rounds', 0)),
                1 if record.get('retry_heads') is not None else 0)
    keys = {record.get('key'), record.get('inference_group')}
    for retry in (metadata or {}).get('local_retries', []):
        if retry.get('key') in keys:
            count = max(count, int(retry.get('bounded_attempts', 1)))
    return count


def _native_passes(directory, issues):
    """Count canonical immutable calls; copied evidence is not another call."""
    workers={}
    for parent in directory.rglob('inference-attempts'):
        if not parent.is_dir() or parent.parent.parent.name!='v32-original':continue
        indices=[]
        for folder in parent.iterdir():
            if not folder.is_dir() or not folder.name.isdigit():continue
            indices.append(int(folder.name))
            if not (folder/'attempt-result.json').is_file():
                issues.append({'path':str(folder),'reason':'missing_attempt_result'})
        if indices and sorted(indices)!=list(range(1,max(indices)+1)):
            issues.append({'path':str(parent),'reason':'noncontiguous_attempt_indices'})
    for path in directory.rglob('attempt-result.json'):
        folder=path.parent
        if folder.parent.name!='inference-attempts' or not folder.name.isdigit():continue
        output=folder.parent.parent
        worker=output.parent
        if worker.name!='v32-original':continue
        # Optional diagnostics must not turn a successfully generated candidate
        # into a failure if their own manifest could not be written completely.
        try:attempt=read(path)
        except (OSError,ValueError):
            issues.append({'path':str(path),'reason':'unreadable_attempt_result'});continue
        if not isinstance(attempt,dict):
            issues.append({'path':str(path),'reason':'invalid_attempt_result'});continue
        if (attempt.get('schema')!='v32-inference-attempt-v1'
                or attempt.get('status') not in ('completed','failed')
                or attempt.get('attempt')!=int(folder.name)
                or not attempt.get('original_output_path')):
            issues.append({'path':str(path),'reason':'unverified_attempt_identity_or_status'});continue
        try:bound=Path(attempt['original_output_path']).resolve()
        except (TypeError,ValueError,OSError):
            issues.append({'path':str(path),'reason':'invalid_output_binding'});continue
        if bound!=output.resolve():
            issues.append({'path':str(path),'reason':'noncanonical_evidence_copy'});continue
        snapshot_path=folder/'resolved-parameters.json'
        try:
            snapshot=read(snapshot_path)
            identity=snapshot.get('inference_attempt',{})
            if (identity.get('attempt')!=attempt['attempt']
                    or Path(identity.get('original_output_path','')).resolve()!=bound
                    or Path(snapshot.get('args',{}).get('output_path','')).resolve()!=bound):
                issues.append({'path':str(path),'reason':'snapshot_result_identity_mismatch'});continue
        except (OSError,ValueError,TypeError,AttributeError):
            issues.append({'path':str(path),'reason':'missing_or_unreadable_attempt_snapshot'})
        if attempt.get('diagnostic_errors'):
            issues.append({'path':str(path),'reason':'attempt_diagnostic_errors'})
        workers.setdefault(worker,{}).setdefault(output.name,set()).add(int(folder.name))
    counts={}
    for worker,outputs in workers.items():
        groups={}
        for key,attempts in outputs.items():
            base=re.sub(r'__(?:retry|density_retry\d+)$','',key)
            main,retries,fallbacks=groups.get(base,(0,0,0))
            if base!=key:retries+=int(1 in attempts)
            else:main=int(1 in attempts)
            fallbacks+=sum(index>1 for index in attempts)
            groups[base]=(main,retries,fallbacks)
        cache=worker.parent.parent/(worker.parent.name+'-mother.json')
        counts[cache]=groups
    return counts


def execution_summary(directory, result):
    directory=Path(directory)
    issues=[]
    native=_native_passes(directory,issues)
    main = retries = 0
    for path in directory.rglob('*-mother.json'):
        cache_main=cache_retries=0
        cached_groups={}
        cache = read(path)
        parent_retry=any(re.fullmatch(r'density-round-\d+',part) for part in path.relative_to(directory).parts[:-1])
        if cache.get('format') == 2:
            records = cache.get('records', [])
            if cache.get('model') == 'v32':
                passes=len(cache.get('inference_groups', []))
                if parent_retry:cache_retries+=passes
                else:cache_main+=passes
                groups = {}
                for row in records:
                    key = row.get('inference_group', row.get('key'))
                    groups[key] = max(groups.get(key, 0), spent_retry_rounds(row, cache.get('metadata')))
                cache_retries += sum(groups.values())
                cached_groups={row['key']:(1,groups.get(row['key'],0))
                               for row in cache.get('inference_groups',[]) if row.get('key') is not None}
                for key,count in groups.items():
                    if key not in cached_groups:cached_groups[key]=(0,count)
            else:
                if parent_retry:cache_retries+=len(records)
                else:cache_main+=len(records)
                cache_retries += sum(spent_retry_rounds(r) for r in records)
        else:
            keys = list(cache.get('raw', {}))
            passes=sum(not k.endswith('__retry') for k in keys)
            if parent_retry:cache_retries+=passes
            else:cache_main+=passes
            cache_retries += sum(k.endswith('__retry') for k in keys)
            if cache.get('model')=='v32':
                for key in keys:
                    base=key.removesuffix('__retry')
                    before=cached_groups.get(base,(0,0))
                    cached_groups[base]=(max(before[0],int(base==key)),before[1]+int(base!=key))
        # Older or partially recorded workers retain their cache counts. Native
        # calls also include failed outputs with no mother cache and fallbacks.
        recorded=native.pop(path,{}) if cache.get('model')=='v32' else {}
        if recorded:
            for key,counts in recorded.items():
                old=cached_groups.get(key,(0,0))
                # Cache retry budgets represent distinct retry stages, not
                # extra generate calls after timing/OOM errors within a stage.
                cached_groups[key]=(max(old[0],counts[0]),max(old[1],counts[1])+counts[2])
            observed_main=sum(row[0] for row in cached_groups.values())
            observed_retry=sum(row[1] for row in cached_groups.values())
            if parent_retry:observed_retry+=observed_main;observed_main=0
            cache_main=max(cache_main,observed_main)
            cache_retries=max(cache_retries,observed_retry)
        main+=cache_main
        retries+=cache_retries
    for path,groups in native.items():
        observed_main=sum(row[0] for row in groups.values())
        observed_retry=sum(row[1]+row[2] for row in groups.values())
        if any(re.fullmatch(r'density-round-\d+',part) for part in path.relative_to(directory).parts[:-1]):
            observed_retry+=observed_main;observed_main=0
        main+=observed_main;retries+=observed_retry
    rows = result.get('advanced_result', [])
    reused = set(result.get('reused_revisions', []))
    reused.update(row['id'] for row in rows if row.get('id') and row.get('kind') == 'stem_raw'
                  and row.get('provenance', {}).get('cache_job') != directory.name)
    measured=[]
    for path in directory.rglob('v32-original/worker-result.json'):
        meta=read(path).get('resident')
        if meta:measured.append({'engine':'v32',**meta})
    for path in directory.glob('resident-mug-*.json'):
        measured.extend({'engine':'mug',**row} for row in read(path).get('requests',[]))
    telemetry={'resident_requests':measured} if measured else {}
    if issues:telemetry['native_attempt_diagnostics']={'status':'partial_capture','issues':issues}
    if 'fusion_seconds' in result:telemetry['fusion_seconds']=result['fusion_seconds']
    if result.get('generation_context_policy'):
        telemetry.update(generation_context_policy=result['generation_context_policy'],
                         owned_regions=len(result.get('member_segment_ids',[])))
    return {**telemetry,'model_passes': main, 'retry_passes': retries, 'reused_versions': len(reused),
            'fusion_candidates': sum(row.get('kind') == 'fusion' for row in rows)}
