const {test} = require('node:test');
const assert = require('node:assert/strict');
const {normalizeAppearance,createMeteorSystem} = require('../web/appearance.js');

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
test('continuous mouse movement cannot grow the effect without bound',()=>{
  const system = createMeteorSystem(()=>.5);
  for (let i=0;i<2000;i++) system.emit(i,100,20,-5);
  assert.equal(system.particles.length,64);
  assert.equal(system.particles.at(-1).x,1999);
});
test('all meteors expire after motion stops and clear releases the remaining particles',()=>{
  const system = createMeteorSystem(()=>.5);
  system.emit(5,10,0,0);
  system.step(16);
  assert.ok(system.particles.every(p=>Number.isFinite(p.x)&&Number.isFinite(p.y)));
  for (let i=0;i<60;i++) system.step(16);
  assert.equal(system.particles.length,0);
  system.emit(5,10,3,4); system.clear();
  assert.equal(system.particles.length,0);
});
test('a long suspension does not fling particles across the screen',()=>{
  const system = createMeteorSystem(()=>.5);
  system.emit(100,100,1,1); system.step(100000);
  assert.equal(system.particles[0].age,48);
  assert.ok(Math.abs(system.particles[0].x-100)<5);
});
