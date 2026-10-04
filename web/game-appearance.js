(function(root){
  'use strict';
  const defaults={skin:'float',hitEffects:'standard'};
  function normalize(value){return {skin:['float','mechanical','minimal'].includes(value?.skin)?value.skin:defaults.skin,hitEffects:['off','light','standard'].includes(value?.hitEffects)?value.hitEffects:defaults.hitEffects};}
  function needsPatternSelector(count,width){return count>3||(count>1&&width<260);}
  if(typeof module==='object'&&module.exports){module.exports={normalize,needsPatternSelector};return;}
  const key='startrail.game-appearance.v1',$=id=>document.getElementById(id),renderer=root.PlayfieldRenderer;
  let saved;try{saved=JSON.parse(localStorage.getItem(key)||'null');}catch{}
  let prefs=normalize(saved),frame=null;const pressed=new Set(),effects=renderer.createEffects(),motion=matchMedia('(prefers-reduced-motion: reduce)');
  root.GameAppearance={get:()=>({...prefs}),needsPatternSelector};
  function apply(save=true){
    if(frame!==null)cancelAnimationFrame(frame);frame=null;
    for(const b of document.querySelectorAll('[data-game-skin]'))b.setAttribute('aria-pressed',String(b.dataset.gameSkin===prefs.skin));
    $('hit-effect-style').value=prefs.hitEffects;
    if(save)try{localStorage.setItem(key,JSON.stringify(prefs));}catch{}
    window.dispatchEvent(new Event('gameappearancechange'));drawSamples();drawTrial();
  }
  function drawSamples(){for(const canvas of document.querySelectorAll('[data-skin-sample]')){const ctx=canvas.getContext('2d');renderer.render(ctx,240,120,{skin:canvas.dataset.skinSample,speed:1,now:0,notes:renderer.prepareNotes([0,1,2,3].map(lane=>({lane,start:2900-lane*400,end:lane===1?4800:null}))),caption:''});}}
  function drawTrial(time=performance.now()){
    frame=null;const canvas=$('game-skin-trial');if(!$('appearance-dialog').open||!canvas)return;
    const box=canvas.getBoundingClientRect(),d=Math.min(devicePixelRatio||1,2);if(!box.width)return;
    canvas.width=Math.round(box.width*d);canvas.height=Math.round(box.height*d);const ctx=canvas.getContext('2d');ctx.setTransform(d,0,0,d,0,0);
    renderer.render(ctx,box.width,box.height,{...prefs,pressed,events:effects.live(time),effectTime:time,reduced:motion.matches,labels:['D','F','J','K'],notes:[{lane:1,start:1200,end:3000,holding:pressed.has(1)}],speed:1,caption:'DFJK 试按 · 展示外观，不计分'});
    if(effects.items.length&&!motion.matches)frame=requestAnimationFrame(drawTrial);
  }
  function press(lane){if(pressed.has(lane))return;pressed.add(lane);effects.emit(lane,'hit',performance.now());root.KeySounds?.play();if(frame===null)drawTrial();}
  function release(lane){pressed.delete(lane);effects.emit(lane,'tail',performance.now());if(frame===null)drawTrial();}
  function clear(){pressed.clear();effects.clear();if(frame!==null)cancelAnimationFrame(frame);frame=null;if($('appearance-dialog').open)drawTrial();}
  for(const button of document.querySelectorAll('[data-game-skin]'))button.addEventListener('click',()=>{prefs.skin=button.dataset.gameSkin;apply();});
  $('hit-effect-style').addEventListener('change',()=>{prefs.hitEffects=$('hit-effect-style').value;effects.clear();apply();});
  for(const button of document.querySelectorAll('[data-trial-key]')){
    const lane=Number(button.dataset.trialKey);
    button.addEventListener('pointerdown',event=>{button.setPointerCapture(event.pointerId);press(lane);});
    button.addEventListener('pointerup',()=>release(lane));button.addEventListener('pointercancel',()=>release(lane));
    button.addEventListener('click',event=>{if(event.detail===0){press(lane);release(lane);}});
  }
  window.addEventListener('keydown',event=>{if(!$('appearance-dialog').open||event.target.closest('input,select,textarea'))return;const lane=['KeyD','KeyF','KeyJ','KeyK'].indexOf(event.code);if(lane<0)return;event.preventDefault();if(!event.repeat)press(lane);});
  window.addEventListener('keyup',event=>{if(!$('appearance-dialog').open)return;const lane=['KeyD','KeyF','KeyJ','KeyK'].indexOf(event.code);if(lane>=0)release(lane);});
  $('appearance-dialog').addEventListener('close',clear);window.addEventListener('blur',clear);
  new MutationObserver(()=>{if($('appearance-dialog').open)drawTrial();else clear();}).observe($('appearance-dialog'),{attributes:true,attributeFilter:['open']});
  new ResizeObserver(()=>{if(frame===null)drawTrial();}).observe($('game-skin-trial'));
  $('appearance-reset').addEventListener('click',()=>{prefs=normalize(null);apply();});
  motion.addEventListener('change',()=>{clear();drawTrial();});apply(false);
})(typeof window==='object'?window:this);
