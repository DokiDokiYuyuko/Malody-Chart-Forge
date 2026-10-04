"""Count recorded model passes, including coalesced requests and bounded retries."""
from .advanced import read


def execution_summary(directory, result):
    main = retries = 0
    for path in directory.rglob('*-mother.json'):
        cache = read(path)
        if cache.get('format') == 2:
            records = cache.get('records', [])
            if cache.get('model') == 'v32':
                main += len(cache.get('inference_groups', []))
                retries += len({r.get('inference_group', r['key']) for r in records if r.get('retry_heads') is not None})
            else:
                main += len(records)
                retries += sum(max(0, len(r.get('attempts', []))-1) for r in records)
        else:
            keys = list(cache.get('raw', {}))
            main += sum(not k.endswith('__retry') for k in keys)
            retries += sum(k.endswith('__retry') for k in keys)
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
    if 'fusion_seconds' in result:telemetry['fusion_seconds']=result['fusion_seconds']
    return {**telemetry,'model_passes': main, 'retry_passes': retries, 'reused_versions': len(reused),
            'fusion_candidates': sum(row.get('kind') == 'fusion' for row in rows)}
