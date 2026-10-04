const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const {normalize}=require('../web/keysounds.js');

test('invalid saved preferences fall back safely and volume is bounded',()=>{
  assert.deepEqual(normalize(null),{enabled:true,style:'click',volume:30});
  assert.deepEqual(normalize({enabled:false,style:'invalid',volume:500}),{enabled:false,style:'click',volume:100});
  assert.equal(normalize({volume:-20}).volume,0);
});

test('decoded samples are cached; simultaneous keys use independent sources; disabling mutes input',async()=>{
  const elements=new Map();let fetches=0,starts=0,saved=null;
  const element=id=>{if(elements.has(id))return elements.get(id);const events=new Map();const el={value:'',checked:false,textContent:'',addEventListener:(name,handler)=>events.set(name,handler),replaceChildren(){},trigger:name=>events.get(name)()};elements.set(id,el);return el;};
  const gain=()=>({gain:{setValueAtTime(){},linearRampToValueAtTime(){}},connect(){},disconnect(){}});
  class AudioContext{
    constructor(){this.state='running';this.currentTime=0;this.destination={};}
    async resume(){} async decodeAudioData(){return {duration:.1};}
    createGain(){return gain();}
    createDynamicsCompressor(){return {threshold:{},knee:{},ratio:{},connect(){}};}
    createBufferSource(){return {connect(){},start(){starts++;},stop(){},disconnect(){}};}
  }
  const context={window:{AudioContext},document:{getElementById:element,createElement:()=>({})},localStorage:{getItem:()=>null,setItem:(key,value)=>saved=JSON.parse(value)},fetch:async()=>{fetches++;return {ok:true,arrayBuffer:async()=>new ArrayBuffer(0)};}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../web/keysounds.js'),'utf8'),context);
  const audio=context.window.KeySounds;await audio.prepare();await audio.prepare();assert.equal(fetches,1);
  for(let i=0;i<4;i++)assert.equal(audio.play(),true);assert.equal(starts,4,'chords are four independent sources');
  element('keysound-volume').value='0';element('keysound-volume').trigger('input');assert.equal(audio.play(),false);
  element('keysound-volume').value='40';element('keysound-volume').trigger('input');assert.equal(audio.play(),true);
  element('keysound-enabled').checked=false;element('keysound-enabled').trigger('change');assert.equal(audio.play(),false);assert.equal(saved.enabled,false);
});
