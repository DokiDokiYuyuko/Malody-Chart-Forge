const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../web/advanced.js'),'utf8');
function functionSource(name){
  const start=source.search(new RegExp('  (?:async )?function '+name+'\\('));
  assert.ok(start>=0,`Missing actual function ${name}`);
  // Function declarations are either one line or close at the workbench's two-space indent.
  const lineEnd=source.indexOf('\n',start);
  return source.slice(start,source.slice(start,lineEnd).trimEnd().endsWith('}')?lineEnd:source.indexOf('\n  }',start)+4);
}
function harness(projectFields={}){
  const elements=new Map(),handlers={},calls=[];
  const element=()=>({value:'',checked:false,disabled:false,hidden:false,dataset:{},children:[],replaceChildren(){this.children=[];},append(...items){this.children.push(...items);},classList:{toggle(){}},click(){calls.push({download:this.href});},remove(){}});
  const $=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  let server={id:'p',revision:1,title:'Default title',artist:'Channel',creator:'Startrail',segments:[],variants:[],assemblies:[],duration:10,samples:441000,...projectFields};
  const c={$,s:{project:structuredClone(server),selected:new Set(),adoption:new Set()},view:element(),SR:44100,T:{time:String},patterns:{},variants:()=>[],node:element,option:element,
    renderSegments(){},renderVersions(){},renderExport(){},renderTempo(){},renderRange(){},draw(){},loadSelected:async()=>{},syncWhole:async()=>{},segment:()=>null,
    projectEpoch:0,base:()=>'/projects/'+c.s.project.id,bind:(id,fn)=>handlers[id]=fn,listProjects:async()=>{},notice(){},
    W:{allExportReady:()=>true},document:{createElement:element,body:element()},pollTasks:async()=>{},
    async api(path,method='GET',body){calls.push({path,method,body:body&&(body instanceof FormData?body:structuredClone(body))});if(c.intercept)await c.intercept(path,method,body);
      if(method==='PATCH'){assert.equal(body.expected_revision,server.revision);server={...server,...body,revision:server.revision+1};return structuredClone(server);}
      if(path.endsWith('/assemblies'))return {id:'new',download:'/frozen/new.mcz'};
      return structuredClone(server);
    }};
  vm.createContext(c);
  for(const name of ['renderProject','acceptProject'])vm.runInContext(functionSource(name),c);
  const helperStart=source.indexOf('  // Metadata drafts');
  if(helperStart>=0)vm.runInContext(source.slice(helperStart,source.indexOf('  function renderProject()',helperStart)),c);
  vm.runInContext(source.match(/  bind\('aw-save-metadata',[^\n]+/)[0],c);
  vm.runInContext(source.match(/  bind\('adv-assemble',[^\n]+/)[0],c);
  c.renderProject();
  const input=(title,artist,creator)=>{if(creator!==undefined){$('aw-song-creator').value=creator;$('aw-song-creator').oninput?.();}if(title!==undefined){$('aw-song-title').value=title;$('aw-song-title').oninput?.();}if(artist!==undefined){$('aw-song-artist').value=artist;$('aw-song-artist').oninput?.();}};
  return {c,$,calls,handlers,input,server:()=>server};
}
test('poll acceptance and rerender preserve edited title and explicitly empty artist',async()=>{
  const h=harness();h.input('My title','');await h.c.acceptProject({...h.c.s.project,revision:2},false);
  assert.equal(h.$('aw-song-title').value,'My title');assert.equal(h.$('aw-song-artist').value,'');
});
test('export captures and saves metadata before refreshing project and creates an independent assembly',async()=>{
  const h=harness();h.input('Export title','');await h.handlers['adv-assemble']();
  assert.equal(h.server().title,'Export title');assert.equal(h.server().artist,'');
  const patch=h.calls.findIndex(x=>x.method==='PATCH'),get=h.calls.findIndex(x=>x.method==='GET');assert.ok(patch>=0&&patch<get);
  assert.equal(h.calls.find(x=>x.path?.endsWith('/assemblies')).body.expected_revision,2);
  assert.equal(h.calls.at(-1).download,'/frozen/new.mcz');
});

function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
function workflows(h){
  const c=h.c;
  Object.assign(c,{busy:new Set(),visible:false,inputSource:'original',generationDraft:{engine:'mug'},validateNpsRanges(){},checked:id=>id==='aw-patterns'?['balanced']:['expert'],generationTargets:()=>['seg'],sourceOptions:()=>({source_id:'original'}),pendingSubmission:null,renderDelivery(){},localCandidateIds:new Set(),historicalCandidateIds:new Set(),
    defaults:{capabilities:{music_workflow:1}},analysisBusy:false,sourceIntentOptions:()=>({source_id:'original'}),renderDraft(){},pollMusicAnalysis:async()=>{},confirmedRequest:null,currentSchemeIntent:()=> 'intent',changeMode(){},
    async previewDraft(){h.calls.push({preview:true});c.s.draft={id:'refreshed',arrangement_plan_id:'plan',project_revision:c.s.project.revision};c.s.draftIntent='intent';}});
  c.s.segmentId='seg';c.s.variant='expert';c.s.draft={id:'original',arrangement_plan_id:'plan',project_revision:1};c.s.draftIntent='intent';
  h.$('aw-engine').value='mug';h.$('aw-settings-scope').value='project';
  c.crypto={randomUUID:()=> 'request-id'};
  const api=c.api;c.api=async(path,method,body)=>{
    if(path.endsWith('/generation-batches')||path.endsWith('/confirm-and-generate')||path.endsWith('/music-analysis')){
      h.calls.push({path,method,body:structuredClone(body)});
      return path.endsWith('/music-analysis')?{id:'analysis',status:'queued'}:{id:'batch',jobs:[{}],batch:{id:'batch'},project:structuredClone(h.server())};
    }
    return api(path,method,body);
  };
  for(const name of ['saveSettings','generate','startMusicAnalysis'])vm.runInContext(functionSource(name),c);
  const start=source.indexOf("  bind('aw-apply-segmentation',async()=>{");
  vm.runInContext(source.slice(start,source.indexOf('\n  });',start)+6),c);
  return c;
}
test('project switch resets drafts and late saves cannot affect another opening of the same project',async()=>{
  const h=harness();h.input('Old draft','Old artist','Old creator');const gate=deferred(),started=deferred();
  h.c.intercept=async()=>{started.resolve();await gate.promise;};
  const save=h.handlers['aw-save-metadata']();await started.promise;
  h.c.s.project={...h.c.s.project,id:'other',title:'Other title',artist:'Other artist',creator:'Other creator'};h.c.projectEpoch++;h.c.renderProject();
  assert.equal(h.$('aw-song-creator').value,'Other creator');assert.equal(h.$('aw-song-title').value,'Other title');assert.equal(h.$('aw-song-artist').value,'Other artist');
  h.c.s.project={...h.c.s.project,id:'p',title:'Reopened'};h.c.projectEpoch++;h.c.renderProject();h.input('New opening',undefined,'New creator');
  gate.resolve();await assert.rejects(save,/项目已切换/);assert.equal(h.$('aw-song-creator').value,'New creator');assert.equal(h.$('aw-song-title').value,'New opening');
});
test('explicit save keeps input typed while PATCH is pending and subsequent save commits it',async()=>{
  const h=harness(),gate=deferred(),started=deferred();h.input('First','');
  h.c.intercept=async()=>{started.resolve();await gate.promise;};const save=h.handlers['aw-save-metadata']();await started.promise;
  h.input('Second','New artist');gate.resolve();await save;
  assert.equal(h.server().title,'First');assert.equal(h.server().artist,'');assert.equal(h.$('aw-song-title').value,'Second');assert.equal(h.$('aw-song-artist').value,'New artist');
  h.c.intercept=null;await h.handlers['aw-save-metadata']();assert.equal(h.server().title,'Second');assert.equal(h.server().artist,'New artist');
});
test('untouched fields follow polling while edited fields survive',async()=>{
  const h=harness();h.input('Draft');await h.c.acceptProject({...h.c.s.project,artist:'Remote artist',revision:2},false);
  assert.equal(h.$('aw-song-title').value,'Draft');assert.equal(h.$('aw-song-artist').value,'Remote artist');
});
test('overlapping saves serialize with successive expected revisions',async()=>{
  const h=harness();h.input('First');const first=h.handlers['aw-save-metadata']();h.input('Second');const second=h.handlers['aw-save-metadata']();await Promise.all([first,second]);
  assert.deepEqual(h.calls.filter(x=>x.method==='PATCH').map(x=>x.body.expected_revision),[1,2]);assert.equal(h.server().title,'Second');
});
for(const [name,act,suffix] of [
  ['start processing',h=>h.c.startMusicAnalysis(),'/music-analysis'],
  ['confirm segmentation',h=>h.handlers['aw-apply-segmentation'](),'/confirm-and-generate'],
  ['generate all',h=>h.c.generate(false),'/generation-batches'],
  ['regenerate current',h=>h.c.generate(true),'/generation-batches'],
  ['save settings',h=>h.c.saveSettings(),null],
  ['export',h=>h.handlers['adv-assemble'](),'/assemblies'],
]){
  test(`${name} commits captured metadata before subsequent operations`,async()=>{
    const h=harness();workflows(h);h.input('Chosen title','','  自填谱师  ');await act(h);
    assert.equal(h.server().creator,'自填谱师');assert.equal(h.$('aw-song-creator').value,'自填谱师');assert.equal(h.server().title,'Chosen title');assert.equal(h.server().artist,'');
    assert.equal(h.calls[0].method,'PATCH');assert.equal(h.calls[0].body.title,'Chosen title');assert.equal(h.calls[0].body.artist,'');assert.equal(h.calls[0].body.expected_revision,1);
    if(suffix){const submission=h.calls.find(x=>x.path?.endsWith(suffix));assert.ok(submission);assert.equal(submission.body.expected_revision,h.server().revision);}
    if(name==='confirm segmentation'){assert.equal(h.calls.filter(x=>x.preview).length,1);assert.equal(h.calls.find(x=>x.path?.endsWith(suffix)).body.draft_id,'refreshed');}
  });
  test(`${name} stops on metadata conflict and retains inputs`,async()=>{
    const h=harness();workflows(h);h.input('Unsaved title','','失败保留');h.c.intercept=async()=>{throw new Error('409 revision conflict');};
    await assert.rejects(act(h),/409 revision conflict/);assert.equal(h.$('aw-song-creator').value,'失败保留');assert.equal(h.$('aw-song-title').value,'Unsaved title');assert.equal(h.$('aw-song-artist').value,'');
    assert.equal(h.calls.length,1);assert.equal(h.calls[0].method,'PATCH');
    h.c.intercept=null;await act(h);assert.equal(h.server().title,'Unsaved title');
  });
}
test('export captures before GET and preserves edits made while the GET is pending',async()=>{
  const h=harness(),gate=deferred(),started=deferred();h.input('Clicked title','Clicked artist','Clicked creator');
  h.c.intercept=async(path,method)=>{if(method==='GET'){started.resolve();await gate.promise;}};
  const exporting=h.handlers['adv-assemble']();await started.promise;h.input('Next title','','Next creator');gate.resolve();await exporting;
  assert.equal(h.server().creator,'Clicked creator');assert.equal(h.$('aw-song-creator').value,'Next creator');assert.equal(h.server().title,'Clicked title');assert.equal(h.$('aw-song-title').value,'Next title');assert.equal(h.$('aw-song-artist').value,'');
});
test('server rejection of empty title preserves draft and blocks export',async()=>{
  const h=harness();h.input('','');h.c.intercept=async()=>{throw new Error('title required');};
  await assert.rejects(h.handlers['adv-assemble'](),/title required/);assert.equal(h.$('aw-song-title').value,'');assert.equal(h.calls.length,1);
});

test('saving unchanged metadata acknowledges the draft so later server updates can render',async()=>{
  const h=harness();h.input('Default title','Channel');await h.handlers['aw-save-metadata']();assert.equal(h.calls.length,0);
  await h.c.acceptProject({...h.c.s.project,title:'External title',artist:'External artist',revision:2},false);
  assert.equal(h.$('aw-song-title').value,'External title');assert.equal(h.$('aw-song-artist').value,'External artist');
});
test('explicit metadata save refreshes an existing segmentation draft for confirmation',async()=>{
  const h=harness();workflows(h);h.input('Renamed');await h.handlers['aw-save-metadata']();
  assert.equal(h.c.s.draft.project_revision,2);await h.handlers['aw-apply-segmentation']();
  assert.equal(h.calls.find(x=>x.path?.endsWith('/confirm-and-generate')).body.expected_revision,2);
});
test('metadata saved during an obsolete response remains dirty and blocks export',async()=>{
  const h=harness(),gate=deferred(),started=deferred();h.input('Retain me',undefined,'Retain creator');
  h.c.intercept=async()=>{started.resolve();await gate.promise;};const exporting=h.handlers['adv-assemble']();await started.promise;
  await h.c.acceptProject({...h.c.s.project,title:'Concurrent update',revision:3},false);gate.resolve();
  await assert.rejects(exporting,/项目已更新/);assert.equal(h.$('aw-song-creator').value,'Retain creator');assert.equal(h.$('aw-song-title').value,'Retain me');assert.equal(h.calls.length,1);
});
test('historical assembly download cards retain frozen URLs after metadata changes',async()=>{
  const h=harness();const product={id:'old',download:'/immutable/old.mcz',duration:10,created:'2026-10-01',charts:[],title:'Frozen title',artist:'Frozen artist'};
  h.c.action=()=>({});h.c.loadAssembly=async()=>{};
  const cardSource=source.match(/    function productCard\([^\n]+/)[0];vm.runInContext(cardSource,h.c);
  const before=h.c.productCard(product,false);h.input('Future title','');await h.handlers['aw-save-metadata']();const after=h.c.productCard(product,false);
  assert.equal(before.children.at(-1).href,'/immutable/old.mcz');assert.equal(after.children.at(-1).href,'/immutable/old.mcz');
  assert.equal(product.title,'Frozen title');assert.equal(product.artist,'Frozen artist');assert.equal(h.calls.filter(x=>x.path?.includes('/assemblies')).length,0);
});
test('metadata drafts reset when the workbench has no project',()=>{
  const h=harness();h.input('Private title','Private artist');h.c.s.project=null;h.c.projectEpoch++;h.c.renderProject();
  assert.equal(h.$('aw-song-title').value,'');assert.equal(h.$('aw-song-artist').value,'');
});

test('segment settings commit metadata before project settings and segment overrides',async()=>{
  const h=harness();workflows(h);h.$('aw-settings-scope').value='segment';h.c.segment=()=>({id:'seg'});h.input('Segment title','');
  await h.c.saveSettings();const patches=h.calls.filter(x=>x.method==='PATCH');
  assert.deepEqual(patches.map(x=>x.body.expected_revision),[1,2,3]);assert.equal(patches[0].body.title,'Segment title');assert.equal(patches[0].body.artist,'');
  assert.equal(patches[2].path,'/projects/p/segments/seg');assert.equal(patches[2].body.overrides.engine,'mug');assert.equal(h.$('aw-song-title').value,'Segment title');
});
test('project switch during export refresh blocks assembly creation even when the same ID is reopened',async()=>{
  const h=harness(),gate=deferred(),started=deferred();h.input('Captured');
  h.c.intercept=async(path,method)=>{if(method==='GET'){started.resolve();await gate.promise;}};
  const exporting=h.handlers['adv-assemble']();await started.promise;h.c.projectEpoch++;h.c.renderProject();h.input('Reopened draft');gate.resolve();
  await assert.rejects(exporting,/项目已切换/);assert.equal(h.$('aw-song-title').value,'Reopened draft');assert.equal(h.calls.filter(x=>x.path?.endsWith('/assemblies')).length,0);
});

test('historical projects display and persist the default creator without a separate save',async()=>{
  const h=harness({creator:undefined});
  assert.equal(h.$('aw-song-creator').value,'Startrail');await h.handlers['adv-assemble']();
  assert.equal(h.calls[0].body.creator,'Startrail');assert.equal(h.server().creator,'Startrail');
});
test('creator input survives PATCH waits and export refresh; later submit commits new input',async()=>{
  const h=harness(),gate=deferred(),started=deferred();h.input(undefined,undefined,'First');
  h.c.intercept=async(path,method)=>{if(method==='PATCH'){started.resolve();await gate.promise;}};
  const exporting=h.handlers['adv-assemble']();await started.promise;h.input(undefined,undefined,'Second');gate.resolve();await exporting;
  assert.equal(h.server().creator,'First');assert.equal(h.$('aw-song-creator').value,'Second');
  h.c.intercept=null;await h.handlers['adv-assemble']();assert.equal(h.server().creator,'Second');
  await h.c.acceptProject(structuredClone(h.server()),false);assert.equal(h.$('aw-song-creator').value,'Second');
});
test('creator drafts are isolated by project opening and untouched creator follows polling',async()=>{
  const h=harness();await h.c.acceptProject({...h.c.s.project,creator:'Remote',revision:2},false);assert.equal(h.$('aw-song-creator').value,'Remote');
  h.input(undefined,undefined,'Private draft');await h.c.acceptProject({...h.c.s.project,creator:'Poll',revision:3},false);assert.equal(h.$('aw-song-creator').value,'Private draft');
  h.c.s.project={...h.c.s.project,id:'other',creator:'Other'};h.c.projectEpoch++;h.c.renderProject();assert.equal(h.$('aw-song-creator').value,'Other');
  h.c.s.project={...h.c.s.project,id:'p',creator:undefined};h.c.projectEpoch++;h.c.renderProject();assert.equal(h.$('aw-song-creator').value,'Startrail');
});
for(const value of ['   ','華'.repeat(121)])test(`invalid Advanced creator blocks all submission entries (${value.length})`,async()=>{
  const h=harness();workflows(h);h.input(undefined,undefined,value);
  for(const act of [()=>h.c.startMusicAnalysis(),()=>h.handlers['aw-apply-segmentation'](),()=>h.c.generate(false),()=>h.c.generate(true),()=>h.c.saveSettings(),()=>h.handlers['adv-assemble'](),()=>h.handlers['aw-save-metadata']()])await assert.rejects(act(),/谱师名字/);
  assert.equal(h.calls.length,0);assert.equal(h.$('aw-song-creator').value,value);
});
test('Advanced creator accepts 120 trimmed characters',async()=>{
  const h=harness();h.input(undefined,undefined,' '+ '華'.repeat(120)+' ');await h.handlers['adv-assemble']();assert.equal(h.server().creator,'華'.repeat(120));
});
function creationWorkflows(h){
  Object.assign(h.c,{FormData,run:fn=>fn(),exclusive:(_,fn)=>fn(),changeMode(){},
    async openProject(){h.c.projectEpoch++;h.c.s.project=structuredClone(h.server());h.c.renderProject();}});
  vm.runInContext(source.match(/  bind\('adv-import-existing',[^\n]+/)[0],h.c);
  vm.runInContext(source.match(/  \$\('adv-file'\)\.onchange=[^\n]+/)[0],h.c);
  h.c.s.project=null;h.c.projectEpoch++;h.c.renderProject();
  h.$('adv-import-source').value='asset:fixture';const file=new Blob(['audio']);file.name='fixture.mp3';h.$('adv-file').files=[file];
}
for(const kind of ['upload','from-source']){
  test(`Advanced first ${kind} carries creator through creation, first render and export`,async()=>{
    const h=harness();creationWorkflows(h);h.input(undefined,undefined,'  首次输入  ');
    if(kind==='upload')await h.$('adv-file').onchange();else await h.handlers['adv-import-existing']();
    const creation=h.calls[0];assert.equal(creation.path,'/projects/'+kind);
    assert.equal(kind==='upload'?creation.body.get('creator'):creation.body.creator,'首次输入');
    assert.equal(h.server().creator,'首次输入');assert.equal(h.$('aw-song-creator').value,'首次输入');
    assert.equal(h.calls.find(x=>x.method==='PATCH').body.creator,'首次输入');
    await h.handlers['adv-assemble']();assert.equal(h.$('aw-song-creator').value,'首次输入');
  });
}
test('Advanced creation carries edits during POST and protects newer edits during opening and PATCH',async()=>{
  const h=harness();creationWorkflows(h);h.input(undefined,undefined,'Initial');
  const post=deferred(),postStarted=deferred(),opening=deferred(),openStarted=deferred(),patch=deferred(),patchStarted=deferred();
  h.c.intercept=async(path,method)=>{if(method==='POST'){postStarted.resolve();await post.promise;}if(method==='PATCH'){patchStarted.resolve();await patch.promise;}};
  const originalOpen=h.c.openProject;h.c.openProject=async()=>{await originalOpen();openStarted.resolve();await opening.promise;};
  const creating=h.$('adv-file').onchange();await postStarted.promise;h.input(undefined,undefined,'During POST');post.resolve();
  await openStarted.promise;h.input(undefined,undefined,'During open');opening.resolve();
  await patchStarted.promise;h.input(undefined,undefined,'During PATCH');patch.resolve();await creating;
  assert.equal(h.server().creator,'During open');assert.equal(h.$('aw-song-creator').value,'During PATCH');
  h.c.intercept=null;await h.handlers['adv-assemble']();assert.equal(h.server().creator,'During PATCH');
});
test('Advanced creation preserves the latest edit made during POST across first render',async()=>{
  const h=harness();creationWorkflows(h);h.input(undefined,undefined,'Initial');const gate=deferred(),started=deferred();
  h.c.intercept=async(path,method)=>{if(method==='POST'){started.resolve();await gate.promise;}};
  const creating=h.$('adv-file').onchange();await started.promise;h.input(undefined,undefined,'Latest');gate.resolve();await creating;
  assert.equal(h.server().creator,'Latest');assert.equal(h.$('aw-song-creator').value,'Latest');
});
test('Advanced failed creation and invalid creator retain input without opening or saving',async()=>{
  const h=harness();creationWorkflows(h);h.input(undefined,undefined,'Keep on failure');h.c.intercept=async()=>{throw new Error('upload failed');};
  await assert.rejects(h.$('adv-file').onchange(),/upload failed/);assert.equal(h.$('aw-song-creator').value,'Keep on failure');assert.equal(h.c.s.project,null);
  h.calls.length=0;h.input(undefined,undefined,' ');await assert.rejects(h.$('adv-file').onchange(),/谱师名字/);assert.equal(h.calls.length,0);
});
test('Advanced switched project rejects a late creation without overwriting its creator',async()=>{
  const h=harness();creationWorkflows(h);h.input(undefined,undefined,'Creation');const gate=deferred(),started=deferred();
  h.c.intercept=async()=>{started.resolve();await gate.promise;};const creating=h.$('adv-file').onchange();await started.promise;
  h.c.projectEpoch++;h.c.s.project={...h.server(),id:'other',creator:'Other creator'};h.c.renderProject();gate.resolve();
  await assert.rejects(creating,/项目已切换/);assert.equal(h.$('aw-song-creator').value,'Other creator');assert.equal(h.calls.length,1);
});
test('Advanced creation PATCH failure retains creator for a later export retry',async()=>{
  const h=harness();creationWorkflows(h);h.input(undefined,undefined,'Retry creator');
  h.c.intercept=async(path,method)=>{if(method==='PATCH')throw new Error('409 creator save conflict');};
  await assert.rejects(h.$('adv-file').onchange(),/409 creator save conflict/);
  assert.equal(h.$('aw-song-creator').value,'Retry creator');assert.equal(h.calls.filter(x=>x.path?.endsWith('/assemblies')).length,0);
  h.c.intercept=null;await h.handlers['adv-assemble']();assert.equal(h.server().creator,'Retry creator');
});
