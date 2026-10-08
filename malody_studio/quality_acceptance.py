"""Export acceptance is separate from task completion and density compliance."""
from .chart_quality import summarize_region
from .section_plan import canonical_hash


def assembly_quality(revisions):
    events = [event for revision in revisions for event in revision['events']]
    decisions = {}
    policies = set()
    evaluated = True
    for revision in revisions:
        provenance = revision.get('provenance', {})
        evaluated = evaluated and bool(provenance.get('quality_summary'))
        policy = provenance.get('quality_policy', {})
        if policy.get('version'): policies.add(policy['version'])
        for decision in provenance.get('quality_decisions', []):
            decisions[canonical_hash(decision)] = decision
    if not evaluated:
        return {'status': 'not_evaluated', 'reason': 'missing_recorded_quality_evaluation',
                'musical_quality_status': 'not_human_verified', 'policy_versions': sorted(policies)}
    summary = summarize_region(events, list(decisions.values()))['summary']
    return {**summary, 'status': summary['alignment_status'],
            'policy_versions': sorted(policies), 'actual_listening': 'not_verified',
            'game_import_and_feel': 'not_verified', 'density_is_separate': True}
