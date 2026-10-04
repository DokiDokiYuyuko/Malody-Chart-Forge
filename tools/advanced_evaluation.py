"""Summarize real user A/B records; never generate or invent human evaluations."""
import argparse
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from malody_studio.advanced import ProjectStore,atomic,now

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='outputs/advanced-evaluation.json');args=parser.parse_args()
    output=(ROOT/args.output).resolve()
    if ROOT not in output.parents:raise ValueError('Evaluation output must remain in the project directory')
    store=ProjectStore();records=[];sample_projects=[]
    for p in store.list():
        path=store.directory(p['id'])/'evaluations.json'
        if path.exists():records.extend({'project_id':p['id'],'title':p['title'],**r} for r in json.loads(path.read_text(encoding='utf-8')))
        sample_projects.append({'project_id':p['id'],'title':p['title']})
    report={'created':now(),'human_comparisons':len(records),'target':20,'human_evaluation_complete':len(records)>=20,
            'required_sample_categories':['low_bpm','fast_electronic','complex_vocals','sustained_sounds','variable_bpm','seams'],
            'criteria':['structure','rhythm_alignment','pattern_intent','difficulty_consistency','seam_experience'],
            'profiles':{profile:sum(r['profile']==profile for r in records) for profile in ('phone','keyboard')},'records':records,'projects':sample_projects,
            'note':'Sample-category coverage and musical quality require human review; no automated quality score.'}
    atomic(output,report);print(json.dumps({k:report[k] for k in ('human_comparisons','target','human_evaluation_complete','profiles')},ensure_ascii=False))

if __name__=='__main__':main()
