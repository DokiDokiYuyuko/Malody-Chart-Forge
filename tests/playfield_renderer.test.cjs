const {test}=require('node:test');
const assert=require('node:assert/strict');
const R=require('../web/playfield-renderer.js');
const {normalize,needsPatternSelector}=require('../web/game-appearance.js');

test('appearance starts with the floating skin and isolates invalid saved fields',()=>{
  assert.deepEqual(normalize(null),{skin:'float',hitEffects:'standard'});
  assert.deepEqual(normalize({skin:'retro',hitEffects:'off'}),{skin:'float',hitEffects:'off'});
  for(const skin of R.skins)assert.equal(normalize({skin}).skin,skin);
});
test('all skins and speeds share a fixed 64 pixel judgment area',()=>{
  assert.equal(R.geometry(900,600).hit,536);assert.equal(R.geometry(900,600).lane,225);
  assert.equal(R.speedValue(12),12);assert.equal(R.speedValue(16),16);assert.equal(R.speedValue(99),16);assert.equal(R.speedValue('corrupt'),1);
});
test('note spacing is local to each lane, including a chord and a dense stream',()=>{
  const notes=R.prepareNotes([{lane:0,start:100},{lane:1,start:100},{lane:0,start:125},{lane:0,start:300}]);
  assert.equal(notes[0].spacingMs,25);assert.equal(notes[1].spacingMs,Infinity);assert.equal(notes[2].spacingMs,25);
});
test('visual effects are bounded, expire and cannot mutate note judgments',()=>{
  const notes=[{head:false,tail:false}],e=R.createEffects();
  for(let i=0;i<1000;i++)e.emit(i%4,'hit',i);
  assert.equal(e.items.length,32);assert.equal(e.items.at(-1).lane,3);assert.deepEqual(notes,[{head:false,tail:false}]);
  e.emit(5,'hit',1000);assert.equal(e.items.length,32);e.live(1300);assert.equal(e.items.length,0);e.emit(0,'press',1400);e.clear();assert.equal(e.items.length,0);
});
test('multiple pattern choices use a bounded-height selector instead of overflowing tabs',()=>{
  assert.equal(needsPatternSelector(1,180),false);assert.equal(needsPatternSelector(3,300),false);
  assert.equal(needsPatternSelector(2,180),true);assert.equal(needsPatternSelector(9,900),true);
});
test('all skins render held heads and tails without changing the shared note state',()=>{
  const calls=[],ctx=new Proxy({createLinearGradient:()=>({addColorStop(){}})},{get:(obj,key)=>obj[key]||((...args)=>calls.push({method:key,args,fill:obj.fillStyle})),set:(obj,key,value)=>(obj[key]=value,true)});
  const notes=R.prepareNotes([{lane:2,start:1000,end:3000,head:true,tail:false,holding:true}]),before=JSON.stringify(notes);
  for(const skin of R.skins){calls.length=0;R.render(ctx,800,600,{skin,notes,now:2000,speed:12,pressed:new Set([2])});assert.ok(calls.some(c=>c.method==='fillRect'&&c.fill==='#ffd16650'&&c.args[3]>100));assert.equal(JSON.stringify(notes),before);}
});

test('all skins preserve failed holds in gray while successful tails disappear',()=>{
  const calls=[],ctx=new Proxy({createLinearGradient:()=>({addColorStop(){}})},{get:(o,k)=>o[k]||((...args)=>calls.push({method:k,args,fill:o.fillStyle})),set:(o,k,v)=>(o[k]=v,true)});
  for(const skin of R.skins){
    const note={lane:1,start:1000,end:2000,head:true,tail:true,holding:false,headGrade:'Perfect',tailGrade:'Miss',failedAt:1400};
    const before=JSON.stringify(note);calls.length=0;R.render(ctx,640,480,{skin,notes:[note],now:1600,speed:8});
    assert.ok(calls.some(c=>c.method==='fillRect'&&c.fill==='#87949f44'&&c.args[3]>0));assert.equal(JSON.stringify(note),before);
    calls.length=0;R.render(ctx,640,480,{skin,notes:[{...note,tailGrade:'Perfect'}],now:1600,speed:8});assert.ok(!calls.some(c=>c.fill==='#87949f44'));
    calls.length=0;R.render(ctx,640,480,{skin,notes:[{...note,tail:false,headGrade:'Miss',headMissed:true}],now:1600,speed:8});assert.ok(calls.some(c=>c.fill==='#87949f44'));
  }
});
