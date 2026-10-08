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


test('export cannot use stale or deleted versions and gives exact missing segment identities',()=>{
  const p={segments:[{...segment,active:{'balanced--easy':'old'}},{...segment,id:'deleted',active:{'balanced--easy':'unknown'}}],variants:[{key:'balanced--easy'}]};
  const row=W.exportReadiness(p)[0];assert.equal(row.ready,false);assert.deepEqual(row.missingIds,['s','deleted']);
  assert.equal(W.exportReadiness({segments:[],variants:p.variants})[0].ready,false);
});

test('sample trim excludes tail without invalidating adopted full-range versions, and restore includes it',()=>{
  const p={samples:200,tail_trim:{enabled:true,cutoff_sample:80},segments:[segment,{...segment,id:'tail',start_sample:100,end_sample:200,active:{},versions:{}}],variants:[{key:'balanced--easy'}]};
  assert.equal(W.exportReadiness(p)[0].ready,true);
  assert.deepEqual(W.generationTargets(p,'all','s',new Set()),['s']);
  p.tail_trim.enabled=false;
  assert.deepEqual(W.exportReadiness(p)[0].missingIds,['tail']);
});

test('recovery prioritizes active requests then retries only current-range failed combinations',()=>{
  const failed={id:'fail',task_type:'generation',segment_id:'s',start_sample:0,end_sample:100,variants:[{key:'balanced--easy'}],created:'2026-01-01',status:'failed'};
  assert.equal(W.recoveryFor(segment,'balanced--easy',[failed]).kind,'retry');
  const completed={...failed,status:'completed'};
  assert.equal(W.recoveryFor({...segment,active:{}},'balanced--easy',[completed]).kind,'choose');
  assert.equal(W.recoveryFor({...segment,active:{}},'balanced--easy',[completed]).version.id,'fusion');
  const queued={...failed,id:'retry',status:'queued',created:'2026-01-02'};
  assert.equal(W.recoveryFor(segment,'balanced--easy',[queued,failed]).kind,'waiting');
  assert.equal(W.recoveryFor(segment,'balanced--hard',[failed]).kind,'generate');
  assert.equal(W.recoveryFor({...segment,end_sample:101},'balanced--easy',[failed]).kind,'generate');
});

test('one-click export requires every selected combination to be complete',()=>{
  const p={segments:[segment],variants:[{key:'balanced--easy'},{key:'balanced--hard'}]};
  assert.equal(W.exportReadiness(p)[0].ready,true);
  assert.equal(W.allExportReady(p),false);
  const hard={...segment.versions['balanced--easy'][0],id:'hard'};
  const complete={...p,segments:[{...segment,active:{...segment.active,'balanced--hard':'hard'},versions:{...segment.versions,'balanced--hard':[hard]}}]};
  assert.equal(W.allExportReady(complete),true);
  assert.equal(W.allExportReady({...complete,variants:[]}),false);
});

test('scheme intent ignores checkbox order but invalidates each generation input',()=>{
  const intent=(settings={},source='original',patterns=['balanced'],difficulties=['hard'],profile='keyboard')=>W.schemeIntent({id:'p'},settings,source,patterns,difficulties,profile);
  assert.equal(intent({},'original',['balanced','stream']),intent({},'original',['stream','balanced']));
  for(const changed of [intent({engine:'mug'}),intent({},'vocals'),intent({},'original',['stream']),intent({},'original',['balanced'],['easy']),intent({},'original',['balanced'],['hard'],'other')])assert.notEqual(changed,intent());
});

test('scheme communicates musical activity without exposing numeric model controls',()=>{
  assert.equal(W.intensityLabel('calm'),'舒缓');assert.equal(W.intensityLabel('regular'),'常规');assert.equal(W.intensityLabel('dense'),'密集');
  assert.equal(W.intensityLabel(-.8),'舒缓');assert.equal(W.intensityLabel(.1),'常规');assert.equal(W.intensityLabel(.8),'密集');
});

test('stem fusion controls: defaults for new settings, labels, primary only for relane',()=>{
  assert.deepEqual(W.fusionModes.map(m=>[m.value,m.label]),[['vocals_priority','人声为主'],['accompaniment_priority','伴奏为主'],['relane','融合（自动避让）']]);
  assert.deepEqual(W.fusionPrimaries.map(m=>[m.value,m.label]),[['vocals','人声'],['accompaniment','伴奏']]);
  const fresh=W.normalizeFusion({});
  assert.equal(fresh.fusion_mode,'relane');assert.equal(fresh.fusion_primary,'vocals');
  const kept=W.normalizeFusion({fusion_mode:'accompaniment_priority',fusion_primary:'accompaniment'});
  assert.deepEqual([kept.fusion_mode,kept.fusion_primary],['accompaniment_priority','accompaniment']);
  const invalid=W.normalizeFusion({fusion_mode:'union',fusion_primary:'mix'});
  assert.deepEqual([invalid.fusion_mode,invalid.fusion_primary],['relane','vocals']);
  assert.equal(W.fusionPrimaryVisible('relane'),true);
  assert.equal(W.fusionPrimaryVisible('vocals_priority'),false);
  assert.equal(W.fusionPrimaryVisible('accompaniment_priority'),false);
  assert.match(W.fusionModeHint('relane'),/冲突的音符会挪到同一时刻的空轨，挪不开时按所选主次取舍。/);
});

test('stem fusion settings are serialised in the generation draft and only matter for stem inputs',()=>{
  const draft=W.normalizeFusion(structuredClone({engine:'v32',fusion_mode:'relane'}));
  draft.fusion_primary='accompaniment';
  const sent=JSON.parse(JSON.stringify(draft));
  assert.equal(sent.fusion_mode,'relane');assert.equal(sent.fusion_primary,'accompaniment');
  assert.equal(W.fusionUsesStems('dual:abc'),true);assert.equal(W.fusionUsesStems('vocals_accompaniment'),true);
  assert.equal(W.fusionUsesStems('original'),false);assert.equal(W.fusionUsesStems('stem-source-id'),false);
  const a=W.schemeIntent({id:'p'},{...draft,fusion_mode:'vocals_priority'},'dual:x',[],[],'k');
  assert.notEqual(a,W.schemeIntent({id:'p'},draft,'dual:x',[],[],'k'));
});

test('fused revision details summarise the merge decisions',()=>{
  const counts={vocals:{input:10,kept:9,dropped:1,relaned:0,shortened:0,to_tap:0},accompaniment:{input:20,kept:20,dropped:0,relaned:4,shortened:2,to_tap:1}};
  assert.equal(W.fusionSummaryText({mode:'relane',primary:'vocals',counts}),'声部合并：融合（自动避让） · 挪不开时保留人声 · 挪轨 4 · 舍弃 1 · 缩短长条 2 · 长条转单点 1');
  assert.equal(W.fusionSummaryText({mode:'vocals_priority',primary:'vocals',counts:{vocals:{},accompaniment:{}}}),'声部合并：人声为主 · 没有冲突');
  const silentCounts={vocals:{input:30,kept:11,dropped:19,relaned:0,shortened:0,to_tap:0,stem_silent:19},accompaniment:{input:20,kept:20,dropped:0,relaned:0,shortened:0,to_tap:0}};
  assert.equal(W.fusionSummaryText({mode:'relane',primary:'vocals',counts:silentCounts}),'声部合并：融合（自动避让） · 挪不开时保留人声 · 舍弃 19（含静音段内 19）');
  assert.equal(W.fusionSummaryText({mode:'vocals_priority',primary:'vocals',counts:{vocals:{},accompaniment:{}},stem_silence_skipped:['vocals']}),'声部合并：人声为主 · 静音段规则未应用：人声');
  const alignedCounts={vocals:{aligned:0,tail_spacing:3},accompaniment:{aligned:7,dropped:2}};
  assert.equal(W.fusionSummaryText({mode:'vocals_priority',primary:'vocals',counts:alignedCounts}),'声部合并：人声为主 · 舍弃 2 · 对齐同一击点 7 · 拉开长条尾间距 3');
  const spacingCounts={vocals:{tail_spacing:2},accompaniment:{relaned:1,dropped:5,tail_spacing_relane:3,lane_gap_relane:2,lane_gap_dropped:4,stem_silent:1}};
  assert.equal(W.fusionSummaryText({mode:'relane',primary:'vocals',counts:spacingCounts,residual_same_stem_lane_gap:6}),'声部合并：融合（自动避让） · 挪不开时保留人声 · 挪轨 1 · 为保护长条尾挪轨 3 · 为拉开同轨间距挪轨 2 · 舍弃 5（含静音段内 1、同轨间距过近 4） · 拉开长条尾间距 2 · 模型自身同轨过近未改动 6');
  assert.equal(W.fusionSummaryText(null),'');assert.equal(W.fusionSummaryText({mode:'union'}),'');
});
