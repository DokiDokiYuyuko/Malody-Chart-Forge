const {test} = require('node:test');
const assert = require('node:assert/strict');
const {normalizeAppearance,createTrailSystem} = require('../web/appearance.js');

test('appearance handles missing and corrupt saved preferences without enabling unexpected themes',()=>{
  for (const value of [null,undefined,[],42,'invalid',{theme:'unknown',trail:'false'}]) {
    assert.deepEqual(normalizeAppearance(value),{theme:'light',trail:true});
  }
  assert.deepEqual(normalizeAppearance({theme:'dark',trail:false}),{theme:'dark',trail:false});
});
test('reduced-motion users start with the effect disabled, preserving explicit preferences',()=>{
  assert.equal(normalizeAppearance(null,true).trail,false);
  assert.deepEqual(normalizeAppearance({theme:'dark',trail:true},true),{theme:'dark',trail:true});
});
test('fast movement samples a continuous seven-color path instead of sparse particles',()=>{
  const system=createTrailSystem();system.add(0,0,0);system.add(200,0,16);
  assert.equal(system.points.length,51);assert.ok(system.points.every((p,i)=>!i||p.x-system.points[i-1].x<=4));
  assert.ok(new Set(system.points.map(p=>Math.floor(p.hue/40))).size>=6);
});
test('the rainbow cache is bounded and all samples expire after movement stops',()=>{
  const s=createTrailSystem();for(let i=0;i<2000;i++)s.add(i,100,i/10);
  assert.equal(s.points.length,256);s.live(800);assert.equal(s.points.length,0);
  s.add(1,1,900);s.clear();assert.equal(s.points.length,0);
});
test('a discontinuity, excluded region or long suspension never draws a stale line',()=>{
  const s=createTrailSystem();s.add(0,0,0);s.add(20,0,20);s.resetPointer();s.add(100,0,30);assert.equal(s.points.at(-1).break,true);
  s.add(200,0,1000);assert.equal(s.points.at(-1).break,true);s.add(1000,0,1010);assert.notEqual(s.points.at(-1).break,true,'a fast wide movement still gets a continuous trail');
});
