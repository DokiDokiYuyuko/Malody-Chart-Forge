(function () {
  'use strict';
  const styles = [
    ['click','清脆点击','click_001.ogg'], ['soft','轻柔点按','click_005.ogg'],
    ['switch','机械开关','switch_001.ogg'], ['tick','短促滴答','tick_001.ogg'],
    ['pluck','清亮拨弦','pluck_001.ogg'], ['glass','玻璃轻响','glass_001.ogg']
  ];
  function normalize(value) {
    const volume=Number(value?.volume);
    return {enabled:typeof value?.enabled==='boolean'?value.enabled:true,
      style:styles.some(item=>item[0]===value?.style)?value.style:'click',
      volume:Number.isFinite(volume)?Math.min(100,Math.max(0,volume)):30};
  }
  if(typeof module==='object'&&module.exports){module.exports={normalize,styles};return;}
  const $=id=>document.getElementById(id),storageKey='startrail.keysounds.v1';
  let saved;try{saved=JSON.parse(localStorage.getItem(storageKey)||'null');}catch{}
  let prefs=normalize(saved),context=null,output=null;
  const buffers=new Map(),pending=new Map();
  function ensureContext(){
    if(!context){
      const Audio=window.AudioContext||window.webkitAudioContext;
      if(!Audio)throw new Error('此浏览器不支持按键音。');
      context=new Audio({latencyHint:'interactive'});output=context.createDynamicsCompressor();
      output.threshold.value=-6;output.knee.value=12;output.ratio.value=6;output.connect(context.destination);
    }
    return context;
  }
  async function bufferFor(style){
    if(buffers.has(style))return buffers.get(style);
    if(pending.has(style))return pending.get(style);
    const promise=(async()=>{
      const file=styles.find(item=>item[0]===style)?.[2]||styles[0][2];
      const response=await fetch(`/static/assets/keysounds/${file}`);
      if(!response.ok)throw new Error('按键音读取失败，请刷新页面重试。');
      const buffer=await ensureContext().decodeAudioData(await response.arrayBuffer());
      buffers.set(style,buffer);return buffer;
    })();
    pending.set(style,promise);
    try{return await promise;}finally{pending.delete(style);}
  }
  async function prepare(){
    if(!prefs.enabled||!prefs.volume)return;
    try{const ctx=ensureContext();await ctx.resume();await bufferFor(prefs.style);$('keysound-status').textContent='';}
    catch(error){$('keysound-status').textContent=error.message;}
  }
  function play(){
    if(!prefs.enabled||!prefs.volume||!context||context.state!=='running')return false;
    const buffer=buffers.get(prefs.style);if(!buffer)return false;
    const source=context.createBufferSource(),gain=context.createGain();source.buffer=buffer;
    // A fresh source for each key permits chords without restarting the previous sound.
    // Short envelopes keep dense charts clear and avoid clicks at the clipped tail.
    const now=context.currentTime,duration=Math.min(buffer.duration,.15);
    gain.gain.setValueAtTime(prefs.volume/100*.55,now);
    gain.gain.setValueAtTime(prefs.volume/100*.55,now+Math.max(0,duration-.012));
    gain.gain.linearRampToValueAtTime(0,now+duration);
    source.connect(gain);gain.connect(output);source.start(now);source.stop(now+duration);
    source.onended=()=>{source.disconnect();gain.disconnect();};return true;
  }
  function sync(){
    $('keysound-enabled').checked=prefs.enabled;$('keysound-style').value=prefs.style;
    $('keysound-volume').value=prefs.volume;$('keysound-volume-value').textContent=`${prefs.volume}%`;
    $('keysound-style').disabled=!prefs.enabled;$('keysound-volume').disabled=!prefs.enabled;$('keysound-preview').disabled=!prefs.enabled;
  }
  function save(){try{localStorage.setItem(storageKey,JSON.stringify(prefs));}catch{}sync();}
  $('keysound-style').replaceChildren(...styles.map(([key,label])=>{const option=document.createElement('option');option.value=key;option.textContent=label;return option;}));
  $('keysound-enabled').addEventListener('change',()=>{prefs.enabled=$('keysound-enabled').checked;save();if(prefs.enabled)prepare();});
  $('keysound-style').addEventListener('change',()=>{prefs.style=$('keysound-style').value;save();prepare();});
  $('keysound-volume').addEventListener('input',()=>{prefs.volume=Number($('keysound-volume').value);save();});
  $('keysound-preview').addEventListener('click',async()=>{await prepare();play();});
  sync();window.KeySounds={prepare,play};
})();
