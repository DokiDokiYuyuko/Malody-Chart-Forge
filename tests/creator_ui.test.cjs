const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../web/app.js'),'utf8');
const html=fs.readFileSync(require.resolve('../web/index.html'),'utf8');
function actual(name){
  const start=source.search(new RegExp('^(?:async )?function '+name+'\\(', 'm'));
  assert.ok(start>=0,name);
  const end=source.indexOf('\n',start);
  return source.slice(start,source.slice(start,end).trimEnd().endsWith('}')?end:source.indexOf('\n}',start)+2);
}
function harness(){
  const elements=new Map(),calls=[],errors=[],saved=new Map();
  const $=id=>{if(!elements.has(id))elements.set(id,{value:'',checked:false,disabled:false,addEventListener(name,fn){this['on'+name]=fn;},getAttribute(){return 'false';}});return elements.get(id);};
  for(const match of html.matchAll(/<input\b[^>]*>/g)){const id=/\bid="([^"]+)"/.exec(match[0]);if(id)$(id[1]).value=/\bvalue="([^"]*)"/.exec(match[0])?.[1]||'';}
  const c={$,FormData,document:{querySelectorAll:selector=>selector.includes('difficulty')&&selector.includes(':checked')?[{value:'expert'}]:[],querySelector:()=>null},
    npsRangeValues:()=>({expert:{min:1,max:2}}),selectedPatterns:()=>['balanced'],validNpsRange:(min,max)=>min!==undefined&&max!==undefined,npsRangeInputs:()=>({min:{value:1},max:{value:2}}),syncNpsRangeSummary(){},roundNps:Number,
    localStorage:{setItem:(k,v)=>saved.set(k,v)},window:{PlayfieldRenderer:{speedValue:Number}},
    ready:true,pendingImport:null,useReference:false,importedTrack:null,selectedFile:null,localMusicRequest:0,localMetadataEdits:{title:0,artist:0,cover:0},
    showStep(){},showError:(id,message)=>errors.push({id,message}),setCover(){},clearTimeout(){},musicTimer:null,
    activeJob:'fixture',report:{difficulties:[{key:'expert',difficulty:'expert'}]},display(){},loadQueue:async()=>{},watchJob:async()=>{},
    request:async(path,options)=>{calls.push({path,options});return {id:'fixture',asset:{title:'Recognized',artist:'Artist',creator:'Should not replace'}};}};
  vm.createContext(c);
  for(const name of ['buildGenerationFormData','generationSettingsSnapshot','fileSelected','clearMusicSelection','runTierIteration','readPreferences','applyPreferences','persistPreferences'])vm.runInContext(actual(name),c);
  vm.runInContext(source.match(/^const preferenceIds = .*$/m)[0],c);
  vm.runInContext(source.match(/^\$\('creator'\)\.addEventListener.*$/m)[0],c);
  const start=source.indexOf("$('generate-form').onsubmit =");
  vm.runInContext(source.slice(start,source.indexOf('\n};',start)+3),c);
  $('title').value='Song';$('iteration-difficulty').value='expert';
  return {c,$,calls,errors,saved,submit:()=>$('generate-form').onsubmit({preventDefault(){}})};
}
test('creator controls use the exact historical default and a 120 character limit',()=>{
  const advanced=fs.readFileSync(require.resolve('../web/advanced.js'),'utf8');
  for(const [text,id] of [[html,'creator'],[advanced,'aw-song-creator']]){
    const tag=text.match(new RegExp('<input[^>]*id="'+id+'"[^>]*>'))[0];
    assert.match(tag,/maxlength="120"/);assert.match(tag,/value="Startrail"/);assert.match(tag,/required/);
  }
  assert.equal(harness().c.buildGenerationFormData().get('creator'),'Startrail');
  assert.match(html,/app\.js\?v=20261007-library-pagination-v1-creator-v1/);
  assert.match(html,/advanced\.js\?v=20261007-transport-v1-metadata-v1-creator-v2/);
});
for(const [label,setup,path] of [
  ['local upload',h=>h.c.selectedFile=new Blob(['audio']),'/api/jobs'],
  ['reference',h=>h.c.useReference=true,'/api/reference'],
  ['music library',h=>h.c.importedTrack={id:'music-fixture'},'/api/music/music-fixture/generate']
])test(`actual Simple submit includes trimmed creator: ${label}`,async()=>{
  const h=harness();setup(h);h.$('creator').value='  自填谱师  ';await h.submit();
  assert.equal(h.calls[0].path,path);assert.equal(h.calls[0].options.body.get('creator'),'自填谱师');
  assert.equal(h.c.generationSettingsSnapshot().creator,'自填谱师');
});
for(const value of ['   ','x'.repeat(121)])test(`invalid creator blocks submit and regeneration (${value.length})`,async()=>{
  const h=harness();h.c.useReference=true;h.$('creator').value=value;await h.submit();await h.c.runTierIteration();
  assert.equal(h.calls.length,0);assert.match(h.errors.at(-1).message,/谱师名字/);
});
test('120 trimmed characters are accepted and regenerate sends creator',async()=>{
  const h=harness();h.$('creator').value=' '+ '華'.repeat(120)+' ';await h.c.runTierIteration();
  assert.equal(JSON.parse(h.calls[0].options.body).creator,'華'.repeat(120));
});
test('selecting songs and asynchronous identification preserve creator',async()=>{
  const h=harness();h.$('creator').value='First';let resolve;
  h.c.request=()=>new Promise(r=>resolve=r);
  const file=new Blob(['audio']);file.name='song.mp3';const selecting=h.c.fileSelected(file);
  h.$('creator').value='During recognition';resolve({asset:{title:'Found',artist:'Found artist',creator:'Ignored'}});await selecting;
  assert.equal(h.$('creator').value,'During recognition');assert.equal(h.$('title').value,'Found');assert.equal(h.$('artist').value,'Found artist');
  h.c.request=async()=>({asset:{title:'Second',artist:'Second artist'}});
  const other=new Blob(['audio']);other.name='other.mp3';await h.c.fileSelected(other);assert.equal(h.$('creator').value,'During recognition');
});
test('creator input persists immediately and restores through existing preferences',()=>{
  const h=harness();h.$('creator').value='Remember me';h.$('creator').oninput();
  const saved=JSON.parse(h.saved.get('malody-chart-forge.preferences.v1'));assert.equal(saved.values.creator,'Remember me');
  const reopened=harness();reopened.c.applyPreferences(saved);assert.equal(reopened.$('creator').value,'Remember me');
  const historical=harness();historical.c.applyPreferences({version:1,values:{}});assert.equal(historical.$('creator').value,'Startrail');
});

test('reference selection preserves the user creator before the actual reference POST',async()=>{
  const h=harness();h.$('creator').value='参考曲署名';
  const start=source.indexOf("$('reference').onclick =");
  vm.runInContext(source.slice(start,source.indexOf('\n};',start)+3),h.c);
  h.$('reference').onclick();assert.equal(h.$('creator').value,'参考曲署名');
  await h.submit();assert.equal(h.calls[0].path,'/api/reference');assert.equal(h.calls[0].options.body.get('creator'),'参考曲署名');
});
test('batch settings snapshot serializes an immutable creator without source fields',()=>{
  const h=harness();h.$('creator').value='  批量署名  ';
  const body=JSON.parse(JSON.stringify({settings:h.c.generationSettingsSnapshot()}));
  h.$('creator').value='Later edit';assert.equal(body.settings.creator,'批量署名');
  for(const field of ['title','artist','file','artwork_url'])assert.equal(field in body.settings,false);
});
