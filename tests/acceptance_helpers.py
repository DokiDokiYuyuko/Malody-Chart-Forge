"""Pure group-identity checks with synthetic test inputs."""
import hashlib
def decision_head_ids(decision):
    """Quality groups may identify their heads solely through movement rows."""
    identities = set(decision.get('event_ids', [])) | set(decision.get('note_ids', []))
    if decision.get('event_id') is not None:
        identities.add(decision['event_id'])
    for row in decision.get('moves', []):
        if row.get('event_id') is not None:
            identities.add(row['event_id'])
    return identities

def nearby(events):
    ordered = sorted(events, key=lambda e: (e['start_ms'], e['lane'], e['id']))
    groups = []
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        anchor = ordered[cursor]['start_ms']
        while end < len(ordered) and ordered[end]['start_ms'] - anchor <= 15 + 1e-07:
            end += 1
        group = ordered[cursor:end]
        span = group[-1]['start_ms'] - anchor
        if len(group) > 1 and span > 1e-07:
            ids = sorted((e['id'] for e in group))
            groups.append({'group_id': hashlib.sha256('|'.join(ids).encode()).hexdigest()[:20], 'anchor_ms': anchor, 'end_ms': group[-1]['start_ms'], 'span_ms': span, 'head_ids': [e['id'] for e in group], 'lanes': [e['lane'] for e in group], 'types': ['hold' if e.get('end_ms') is not None else 'tap' for e in group], 'before_times_ms': [e['start_ms'] for e in group], 'before_hold_tails_ms': [e.get('end_ms') for e in group], 'classification': 'unresolved'})
        cursor = end
    return groups

def origin_identity(event):
    return tuple(sorted(((o['revision_id'], o['note_id'], o.get('source_id')) for o in event['origins'])))

def introduced_near_groups(before, after):
    """Compare whole anchored groups by original sources, never counts or IDs."""
    old_index = {e['id']: e for e in before}
    new_index = {e['id']: e for e in after}
    assert len(old_index) == len(before) and len(new_index) == len(after)
    original_sets = {frozenset((origin_identity(old_index[i]) for i in group['head_ids'])) for group in nearby(before)}
    return [group for group in nearby(after) if frozenset((origin_identity(new_index[i]) for i in group['head_ids'])) not in original_sets]
