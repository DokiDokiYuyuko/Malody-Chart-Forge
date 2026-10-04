(() => {
  const $=id=>document.getElementById(id),canvas=$('field'),ctx=canvas.getContext('2d');
  const colors=['#62d6d1','#83a7ff','#ffd166','#ef83bf'],keys=['D','F','J','K'];
  const R=window.PlayfieldRenderer,titles={float:'悬浮光片',mechanical:'精密机械',minimal:'极简光轨'};
  const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
  let skin='float',hitEffects='standard',speed=6,paused=reduced,elapsed=0,previous=0,combo=0,judgeTimer;
  const pressed=new Set(),effects=R.createEffects();
  const notes=R.prepareNotes(Array.from({length:56},(_,i)=>({start:600+i*158,lane:(i*3+Math.floor(i/4))%4,end:i%11===3?1550+i*158:null})));
  function size(){const r=canvas.getBoundingClientRect(),d=Math.min(devicePixelRatio||1,2);canvas.width=Math.round(r.width*d);canvas.height=Math.round(r.height*d);ctx.setTransform(d,0,0,d,0,0);}
  function draw(time){const now=elapsed%10000;R.render(ctx,canvas.clientWidth,canvas.clientHeight,{skin,speed,now,pressed,labels:keys,notes:notes.map(n=>({...n,holding:n.end!==null&&n.start<=now&&n.end>now&&pressed.has(n.lane)})),events:effects.live(time),effectTime:time,hitEffects,reduced});}
  function hit(lane){if(pressed.has(lane))return;pressed.add(lane);effects.emit(lane,'hit',performance.now());$('judge').textContent='Perfect';$('combo').textContent=++combo;clearTimeout(judgeTimer);judgeTimer=setTimeout(()=>$('judge').textContent='',650);if(!reduced){$('judge').getAnimations().forEach(a=>a.cancel());$('judge').animate([{transform:'scale(.88)',opacity:.4},{transform:'scale(1.05)',opacity:1,offset:.5},{transform:'scale(1)',opacity:1}],{duration:200});$('combo').animate([{transform:'scale(1.1)'},{transform:'scale(1)'}],{duration:160});}}
  function speedSync(){speed=Number($('speed').value);$('speed-value').innerHTML=speed.toFixed(1)+'<span>×</span>';$('speed').style.setProperty('--fill',((speed-1)/15*100)+'%');document.querySelectorAll('[data-speed]').forEach(b=>b.setAttribute('aria-pressed',String(Number(b.dataset.speed)===speed)));}
  $('speed').addEventListener('input',speedSync);document.querySelectorAll('[data-speed]').forEach(button=>button.onclick=()=>{$('speed').value=button.dataset.speed;speedSync();});speedSync();
  document.querySelectorAll('[data-skin]').forEach(button=>button.onclick=()=>{skin=button.dataset.skin;$('skin-title').textContent=titles[skin];document.querySelectorAll('[data-skin]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));});
  document.querySelectorAll('[data-effect]').forEach(button=>button.onclick=()=>{hitEffects=button.dataset.effect;document.querySelectorAll('[data-effect]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));});
  function pauseSync(){$('pause').textContent=paused?'继续下落':'暂停下落';$('pause').setAttribute('aria-pressed',String(paused));}pauseSync();$('pause').onclick=()=>{paused=!paused;pauseSync();};
  window.addEventListener('keydown',event=>{if(event.target.matches('input,select,textarea'))return;const lane=['KeyD','KeyF','KeyJ','KeyK'].indexOf(event.code);if(lane<0)return;event.preventDefault();if(!event.repeat)hit(lane);});
  window.addEventListener('keyup',event=>{const lane=['KeyD','KeyF','KeyJ','KeyK'].indexOf(event.code);pressed.delete(lane);});
  canvas.addEventListener('pointerdown',event=>{canvas.setPointerCapture(event.pointerId);const r=canvas.getBoundingClientRect();hit(Math.min(3,Math.max(0,Math.floor((event.clientX-r.left)/r.width*4))));});canvas.addEventListener('pointerup',()=>pressed.clear());canvas.addEventListener('pointercancel',()=>pressed.clear());window.addEventListener('blur',()=>pressed.clear());
  const trail=$('trail'),tc=trail.getContext('2d'),points=[];let pointer=null,distance=0;
  function trailSize(){const d=Math.min(devicePixelRatio||1,2);trail.width=innerWidth*d;trail.height=innerHeight*d;tc.setTransform(d,0,0,d,0,0);points.length=0;pointer=null;}
  window.addEventListener('pointermove',event=>{
    if(reduced||!$('trail-enabled').checked||event.pointerType!=='mouse'||event.target.closest('#stage,input,select')){pointer=null;return;}
    const p={x:event.clientX,y:event.clientY},now=performance.now();
    if(pointer){const dx=p.x-pointer.x,dy=p.y-pointer.y,length=Math.hypot(dx,dy),n=Math.min(80,Math.ceil(length/4));for(let i=1;i<=n;i++){points.push({x:pointer.x+dx*i/n,y:pointer.y+dy*i/n,born:now,hue:(distance+length*i/n)*1.25%360,break:i===1&&length>400});}distance+=length;if(points.length>360)points.splice(0,points.length-360);}
    pointer=p;
  },{passive:true});
  function trailDraw(time){tc.clearRect(0,0,innerWidth,innerHeight);while(points.length&&time-points[0].born>520)points.shift();if(!$('trail-enabled').checked){points.length=0;return;}
    // Keep older trail segments out of the playfield as well as new emissions.
    const stageBox=$('stage').getBoundingClientRect();tc.save();tc.beginPath();tc.rect(0,0,innerWidth,innerHeight);tc.rect(stageBox.left,stageBox.top,stageBox.width,stageBox.height);tc.clip('evenodd');
    for(let i=1;i<points.length;i++){const a=points[i-1],b=points[i];if(b.break)continue;const fade=Math.pow(Math.max(0,1-(time-b.born)/520),1.5),g=tc.createLinearGradient(a.x,a.y,b.x,b.y);g.addColorStop(0,`hsla(${a.hue},85%,72%,${fade*.7})`);g.addColorStop(1,`hsla(${b.hue},85%,72%,${fade*.7})`);tc.strokeStyle=g;tc.lineWidth=2.5*fade+.3;tc.shadowColor=`hsl(${b.hue},85%,68%)`;tc.shadowBlur=5;tc.beginPath();tc.moveTo(a.x,a.y);tc.lineTo(b.x,b.y);tc.stroke();}tc.shadowBlur=0;
    tc.restore();
  }
  function frame(time){const dt=previous?Math.min(80,time-previous):0;previous=time;if(!paused&&!document.hidden)elapsed+=dt;draw(time);trailDraw(time);requestAnimationFrame(frame);}
  new ResizeObserver(size).observe(canvas);window.addEventListener('resize',trailSize);size();trailSize();requestAnimationFrame(frame);
})();
