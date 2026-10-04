const {test}=require('node:test');const assert=require('node:assert/strict');
const s=require('../web/separation-audition.js');
test('natural clip end resumes a loop even after media becomes paused; manual pause never restarts',()=>{
 assert.equal(s.shouldRestart(true,{paused:true,ended:true}),true);
 assert.equal(s.shouldRestart(true,{paused:true,ended:false},true),true);
 assert.equal(s.shouldRestart(true,{paused:false,ended:false}),true);
 assert.equal(s.shouldRestart(true,{paused:true,ended:false}),false);
 assert.equal(s.shouldRestart(false,{paused:true,ended:true},true),false);
});
test('trial clock uses original offset and confines seeks to actual audio frames',()=>{
 assert.equal(s.originalTime(2.5,14),16.5);assert.equal(s.localTime(16.5,14,4),2.5);
 assert.equal(s.localTime(3,14,4),0);assert.equal(s.localTime(30,14,4),4);
 assert.deepEqual(s.bounds([14,18],129),[14,18]);assert.throws(()=>s.bounds([18,14],129));assert.throws(()=>s.bounds([14,130],129));
});
test('audition RMS matching accounts for peak safety and attenuates only',()=>{
 const gain=s.matchingVolumes([{rms:.4,peak:2},{rms:.1,peak:.5}]);
 assert.ok(Math.abs(gain[0]-.1/(.4*.49))<1e-10);assert.equal(gain[1],1);
 assert.deepEqual(s.matchingVolumes([{rms:0,peak:0},{rms:.1,peak:.2}]),[1,1]);
 assert.equal(s.safeRms({rms:NaN,peak:1}),0);
});
test('catalog parameters distinguish coverage counts and fixed windows',()=>{
 const cover={label:'覆盖次数',type:'integer',min:2,max:8,choices:[2,4,8]};
 assert.equal(s.parameterError(cover,4),'');assert.notEqual(s.parameterError(cover,6),'');assert.notEqual(s.parameterError(cover,.25),'');
 assert.notEqual(s.parameterError({label:'窗口',type:'integer',min:8,max:8},7.8),'');
});
