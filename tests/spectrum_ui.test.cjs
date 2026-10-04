const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const S=require('../web/spectrum.js');
const R=require('../web/playfield-renderer.js');

test('spectrum and chart share a projection to within one device pixel at every speed',()=>{
  for(const h of [220,480,700])for(const speed of [1,8,16])for(const dpr of [1,1.25,2]){
    const g=R.geometry(400,h),travel=5000/speed,p={...g,travelMs:travel},snapshot={displayTimeMs:28000};
    for(const time of [28000,28000+travel*.3,28000+travel]){
      const expected=g.hit-(time-28000)/travel*g.runway;
      assert.ok(Math.abs(S.timeToY(time,snapshot,p)-expected)*dpr<=1);
    }
    assert.deepEqual(S.visibleRange(snapshot,p,0),[28000,28000+travel]);
    assert.deepEqual(S.visibleRange({displayTimeMs:0},p,28000),[28000,28000+travel]);
  }
});

test('source mapping includes preroll void and half-open noncontinuous seams',()=>{
  const sr=44100, mapping=[{output_start:1.5*sr,output_end:17.5*sr,source_start:28*sr,source_end:44*sr,segment_id:'a'},
    {output_start:17.5*sr,output_end:25.5*sr,source_start:48*sr,source_end:56*sr,segment_id:'b'}];
  assert.equal(S.playbackToSource(0,mapping),null);
  assert.equal(S.playbackToSource(1.5*sr-1,mapping),null);
  assert.deepEqual(S.playbackToSource(1.5*sr,mapping),{sourceSample:28*sr,segmentId:'a'});
  assert.deepEqual(S.playbackToSource(17.5*sr-1,mapping),{sourceSample:44*sr-1,segmentId:'a'});
  assert.deepEqual(S.playbackToSource(17.5*sr,mapping),{sourceSample:48*sr,segmentId:'b'});
  assert.equal(S.playbackToSource(25.5*sr,mapping),null);
});

test('LOD picks fine frames at 16x and binary conversion keeps peak and time orientation',()=>{
  const m={dt_ms:110/22050*1000,levels:[1,2,4,8,16].map((stride,level)=>({stride,level})),total_frames:1100,tile_frames:1024};
  assert.equal(S.chooseLevel(312.5/400,m).stride,1);
  assert.equal(S.chooseLevel(5000/100,m).stride,8);
  assert.equal(S.tileRows(m,1,16),5);
  const bytes=new Uint8Array([0,0,0,255]); // mean zero both rows; late peak 255
  const pixels=S.rgba(bytes.buffer,2,1);
  assert.ok(pixels[1]>pixels[5],'later time is at the bitmap top');
  assert.throws(()=>S.rgba(new Uint8Array(3).buffer,2,1));
});

function app(){
  let finish;const requests=[],draws=[],timers=new Map();let nextTimer=0;
  const ctx=new Proxy({}, {get:(o,k)=>o[k]||((...args)=>{if(k==='drawImage')draws.push(args);}),set:(o,k,v)=>(o[k]=v,true)});
  const canvas={width:0,height:0,clientWidth:160,clientHeight:480,getContext:()=>ctx};
  const initial={schema_version:2,analysis_id:'old',bins:192,total_frames:2000,tile_frames:1024,dt_ms:110/22050*1000,duration_ms:10000,levels:[{level:0,stride:1}]};
  const sandbox={module:{exports:{}},globalThis:null,AbortController,URL,performance:{now:()=>1000},
    document:{createElement:()=>({width:0,height:0,getContext:()=>({putImageData(){}})})},
    ImageData:class{constructor(data,w,h){this.data=data;this.width=w;this.height=h;}},
    setTimeout:(f)=>{const id=++nextTimer;timers.set(id,f);return id;},clearTimeout:id=>timers.delete(id),
    fetch:url=>{requests.push(String(url));return new Promise(resolve=>{finish=resolve;});},
    createImageBitmap:async()=>({close(){this.closed=true;}})};
  sandbox.globalThis=sandbox;vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../web/spectrum.js'),'utf8'),sandbox);
  const pane=sandbox.module.exports.create(canvas),projection={...R.geometry(160,480),travelMs:312.5};
  return {pane,initial,projection,requests,draws,timers,finish:()=>finish({ok:true,status:200,arrayBuffer:async()=>new Uint8Array(2*1024*192).buffer})};
}

test('a stale tile cannot repaint another source, and disposal stops paused retries',async()=>{
  const a=app();
  a.pane.setSource({initialManifest:a.initial,loadManifest:()=>new Promise(()=>{}),tileUrl:(id,l,i)=>`/${id}/${l}/${i}`});
  a.pane.render({displayTimeMs:0},a.projection);
  assert.ok(a.requests.some(u=>u.includes('/old/0/0')));
  assert.ok(a.timers.size,'paused missing tiles schedule a retry without moving audio');
  a.pane.suspend();assert.equal(a.timers.size,0);a.pane.resume();
  a.pane.setSource(null);a.finish();
  for(let i=0;i<8;i++)await Promise.resolve();
  assert.equal(a.draws.length,0);assert.equal(a.pane.cache.size,0);
  a.pane.dispose();assert.equal(a.timers.size,0);
});
