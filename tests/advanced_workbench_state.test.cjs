const test=require('node:test'),assert=require('node:assert/strict');
const W=require('../web/advanced-workbench-state.js');
const segment={id:'s',included:true,start_sample:0,end_sample:100,active:{'balanced--easy':'active'},versions:{'balanced--easy':[
  {id:'active',range:[0,100],source_role:'mix'}, {id:'raw-v',range:[0,100],source_role:'vocals',source_id:'v'},
  {id:'raw-a',range:[0,100],source_role:'accompaniment',source_id:'a'}, {id:'old',range:[0,90],source_role:'mix'},
  {id:'fusion',range:[0,100],source_role:'fusion'}]}};
test('primary choices separate adopted and merged charts from raw stems and stale history',()=>{
  assert.deepEqual(W.candidates(segment,'balanced--easy').map(v=>v.id),['fusion','active']);
  assert.deepEqual(W.candidates(segment,'balanced--easy',{source:'v'}).map(v=>v.id),['fusion','raw-v','active']);
  assert.equal(W.candidates(segment,'balanced--easy',{raw:true}).length,4);
});
test('bulk adoption is explicit and picks main candidates, never incidental latest stems',()=>{
  const p={segments:[segment],variants:[{key:'balanced--easy'}]};
  assert.deepEqual(W.selectionChoices(p,new Set()),[]);
  assert.deepEqual(W.selectionChoices(p,new Set(['s'])),[{segment_id:'s',variant:'balanced--easy',revision_id:'fusion'}]);
});
test('missing selection and export readiness have different meanings',()=>{
  const candidateOnly={...segment,id:'c',active:{}},empty={...segment,id:'e',active:{},versions:{}};
  const p={segments:[segment,candidateOnly,empty],variants:[{key:'balanced--easy'}]};
  assert.deepEqual(W.missingSegments(p),['e']);
  assert.equal(W.exportReadiness(p)[0].missing.length,2);
});
test('context tokens invalidate responses after variant, project or sequence changes',()=>{
  const store=W.create();store.change({project:{id:'p'},segmentId:'s',variant:'easy'});
  const first=store.context();assert.ok(store.matches(first));store.context();assert.equal(store.matches(first),false);
  const second=store.context();store.change({variant:'hard'});assert.equal(store.matches(second),false);
});
test('only exact-range generation jobs affect a current segment status',()=>{
  const tasks=[{task_type:'generation',segment_id:'s',start_sample:0,end_sample:100,status:'completed',created:'2026-01-01'},
    {task_type:'generation',segment_id:'s',start_sample:0,end_sample:90,status:'running',created:'2026-01-02'},
    {task_type:'separation',segment_id:'s',start_sample:0,end_sample:100,status:'running',created:'2026-01-03'}];
  assert.equal(W.taskFor(segment,tasks).status,'completed');
});
test('A/B differences identify timing, lane, hold and deletion without treating renewed IDs as changes',()=>{
  const before=[{id:'same',start_ms:100,lane:0},{id:'move',start_ms:200,lane:1},{id:'lane',start_ms:300,lane:2},
    {id:'hold',start_ms:400,lane:3,end_ms:900},{id:'deleted',start_ms:500,lane:0}];
  const after=[{id:'renewed',start_ms:100,lane:0},{id:'move',start_ms:210,lane:1},{id:'lane',start_ms:300,lane:3},
    {id:'hold',start_ms:400,lane:3,end_ms:950},{id:'added',start_ms:600,lane:2}];
  assert.deepEqual(W.comparisonDiff(before,after).map(r=>r.type),['move','lane','hold','add','delete']);
  assert.deepEqual(W.comparisonDiff(before,before),[]);
});

test('generation range defaults to all actual segments, independently of export inclusion or old candidates',()=>{
  const p={segments:[segment,{...segment,id:'excluded',included:false}]};
  assert.deepEqual(W.generationTargets(p,'all','s',new Set()),['s','excluded']);
  assert.deepEqual(W.generationTargets(p,'current','excluded',new Set(['s'])),['excluded']);
  assert.deepEqual(W.generationTargets(p,'custom','s',new Set(['excluded','deleted'])),['excluded']);
});

test('batch adoption accepts exact selected primary results only and never borrows a previous candidate',()=>{
  const p={segments:[segment],variants:[{key:'balanced--easy'}]};
  const batch={results:[
    {segment_id:'s',variant:'balanced--easy',revision_id:'fusion',range:[0,100],primary:true,adoptable:true},
    {segment_id:'s',variant:'balanced--easy',revision_id:'raw-v',range:[0,100],primary:false,adoptable:true},
    {segment_id:'s',variant:'balanced--easy',revision_id:'old',range:[0,90],primary:true,adoptable:true},
    {segment_id:'s',variant:'removed--hard',revision_id:'removed',range:[0,100],primary:true,adoptable:true},
    {segment_id:'deleted',variant:'balanced--easy',revision_id:'deleted',range:[0,100],primary:true,adoptable:true},
    {segment_id:'s',variant:'balanced--easy',revision_id:'failed',range:[0,100],primary:true,adoptable:false},
  ]};
  assert.deepEqual(W.batchChoices(p,batch,new Set(['fusion','raw-v','old','removed','deleted','failed','active'])),[{segment_id:'s',variant:'balanced--easy',revision_id:'fusion'}]);
  assert.deepEqual(W.batchChoices(p,{results:[]},new Set(['fusion'])),[]);
  assert.deepEqual(W.batchChoices(p,batch,new Set()),[]);
});

test('completion navigation is a one-shot action only in the uninterrupted foreground generation context',()=>{
  const state={project:{id:'p'},mode:'generate',segmentId:'s',variant:'easy'},submission={projectId:'p',segmentId:'s',variant:'easy',autoAllowed:true,delivered:false};
  const foreground={visible:true,playing:false,game:false};
  assert.equal(W.completionCanOpen(submission,state,foreground),true);
  for(const options of [{visible:false},{playing:true},{game:true}])assert.equal(W.completionCanOpen(submission,state,{...foreground,...options}),false);
  for(const change of [{mode:'finish'},{segmentId:'other'},{variant:'hard'},{project:{id:'other'}}])assert.equal(W.completionCanOpen(submission,{...state,...change},foreground),false);
  assert.equal(W.completionCanOpen({...submission,delivered:true},state,foreground),false);
  assert.equal(W.completionCanOpen({...submission,autoAllowed:false},state,foreground),false);
});
