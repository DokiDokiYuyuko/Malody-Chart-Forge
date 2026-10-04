const {test}=require('node:test');
const assert=require('node:assert/strict');
const state=require('../web/advanced-state.js');

test('formats timeline time to rounded milliseconds without float residue',()=>{
  assert.equal(state.formatTime(149.3150000243365),'02:29.315');
  assert.equal(state.formatTimeMs(27318.9999),'00:27.319');
  assert.equal(state.formatTime(-1),'00:00.000');
});

test('playback remains full length until selection or assembly mode is explicit',()=>{
  assert.deepEqual(state.playbackBounds({duration:93,selectionRange:[28,44]}),[0,93]);
  assert.deepEqual(state.playbackBounds({mode:'full',duration:93,selectionRange:[28,44]}),[0,93]);
  assert.deepEqual(state.playbackBounds({mode:'selection',duration:93,selectionRange:[28,44]}),[28,44]);
  assert.deepEqual(state.playbackBounds({mode:'selection',duration:93,selectionRange:[100,120]}),[0,93]);
  assert.deepEqual(state.playbackBounds({mode:'assembly',duration:93,assemblyDuration:25.5}),[0,25.5]);
});

test('chooses only active or latest revision whose saved range matches this segment',()=>{
  const versions=[
    {id:'old',range:[0,529200]},
    {id:'candidate',range:[1234800,1940400]},
    {id:'newer-stale',range:[1234800,1940401]},
  ];
  assert.equal(state.latestMatchingRevision(versions,'old',[1234800,1940400]),'candidate');
  assert.equal(state.latestMatchingRevision(versions,'candidate',[1234800,1940400]),'candidate');
  assert.equal(state.latestMatchingRevision(versions,null,[500,900]),null);
  assert.equal(state.revisionMatchesRange([0,100],[0,101]),false);
});

test('summarizes source roles and stores only settings that differ from the project template',()=>{
  const sets=[{id:'12345678',stems:[{source_id:'stem-v',role:'vocals'},{source_id:'stem-a',role:'accompaniment'}]}];
  assert.equal(state.summarizeSource('original',sets),'原曲');
  assert.equal(state.summarizeSource('stem-v',sets),'人声 · 分离 123456');
  assert.equal(state.summarizeSource('gone',sets),null);
  assert.deepEqual(state.settingsDiff({engine:'v32',conditions:{easy:2,hard:5},tags:['a']},{engine:'v32',conditions:{easy:3,hard:5},tags:['a']}),{conditions:{easy:3}});
});


test('full audio playback never replaces the selected stem or fusion chart with adopted notes',()=>{
  const adopted=[{id:'adopted-accompaniment'}],voice=[{id:'voice'}],fusion=[{id:'fused'}];
  assert.strictEqual(state.previewNotes({revision:{id:'v'},notes:voice,adoptedNotes:adopted,mode:'full'}),voice);
  assert.strictEqual(state.previewNotes({revision:{id:'f'},notes:fusion,adoptedNotes:adopted,mode:'selection'}),fusion);
  assert.deepEqual(state.previewNotes({revision:{id:'empty'},notes:[],adoptedNotes:adopted}),[]);
  assert.strictEqual(state.previewNotes({adoptedNotes:adopted}),adopted);
  assert.strictEqual(state.previewNotes({assembly:true,notes:fusion,adoptedNotes:adopted}),fusion);
});

test('stem preview follows its frozen source and fusion plays the original mixture',()=>{
  assert.equal(state.revisionSource({kind:'stem_raw',provenance:{source_role:'vocals',source_id:'s:vocals'}}),'s:vocals');
  assert.equal(state.revisionSource({kind:'stem_raw',provenance:{source_role:'accompaniment',source_id:'s:accompaniment'}}),'s:accompaniment');
  assert.equal(state.revisionSource({kind:'fusion',provenance:{source_role:'fusion',source_id:'fusion:hash'}}),'original');
  assert.equal(state.revisionLabel({provenance:{source_role:'vocals'}}),'人声谱面');
  assert.equal(state.revisionLabel({kind:'fusion'}),'融合谱面');
});

test('late preview responses cannot overwrite a newer selection, project, segment or variant',()=>{
  const current={sequence:3,project:'p',segment:'s',variant:'balanced--medium'};
  assert.equal(state.selectionMatches({...current},current),true);
  for(const key of Object.keys(current))assert.equal(state.selectionMatches({...current,[key]:'stale'},current),false);
});


test('queue polling must not restore the adopted chart during a manual candidate request',()=>{
  // A candidate request clears the old data while waiting for network response.
  assert.equal(state.canRestorePreview({revision:null,loading:true}),false);
  assert.equal(state.canRestorePreview({revision:{id:'vocals'},loading:false}),false);
  assert.equal(state.canRestorePreview({revision:null,playing:true}),false);
  assert.equal(state.canRestorePreview({revision:null,assembly:true}),false);
  assert.equal(state.canRestorePreview({revision:null,loading:false}),true);
});
