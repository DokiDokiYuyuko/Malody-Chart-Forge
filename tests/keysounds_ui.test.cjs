const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('the first trial key press initializes and plays its sound', async () => {
  let playCount = 0;
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, {value:'',checked:false,disabled:false,textContent:'',
      addEventListener(){},replaceChildren(){}});
    return elements.get(id);
  };
  const context = {
    state:'suspended',currentTime:0,destination:{},
    createDynamicsCompressor(){return {threshold:{},knee:{},ratio:{},connect(){}};},
    async resume(){this.state='running';},
    async decodeAudioData(){return {duration:.15};},
    createBufferSource(){return {connect(){},start(){playCount++;},stop(){},disconnect(){}};},
    createGain(){return {gain:{setValueAtTime(){},linearRampToValueAtTime(){}},connect(){},disconnect(){}};},
  };
  const window={AudioContext:function(){return context;}};
  const document={getElementById:get,createElement:()=>({})};
  const localStorage={getItem:()=>null,setItem(){}};
  const fetch=async()=>({ok:true,arrayBuffer:async()=>new ArrayBuffer(8)});
  vm.runInNewContext(fs.readFileSync('web/keysounds.js','utf8'),{window,document,localStorage,fetch,Map,Math,Number});
  assert.equal(window.KeySounds.play(),false);
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(context.state,'running');
  assert.equal(playCount,1);
});
