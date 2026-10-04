(function () {
  'use strict';
  const storageKey = 'startrail.appearance.v1';
  const themeNames = new Set(['light','peach','moon','sea','dark']);
  function normalizeAppearance(value, reducedMotion = false) {
    return {theme:themeNames.has(value?.theme) ? value.theme : 'light', trail:typeof value?.trail === 'boolean' ? value.trail : !reducedMotion};
  }
  // Sample by distance, retaining one continuous rainbow path with a fixed budget.
  function createTrailSystem(){
    const points=[];let pointer=null,hue=0;
    return {
      points,resetPointer(){pointer=null;},clear(){points.length=0;pointer=null;},
      add(x,y,time){
        if(!Number.isFinite(x+y+time))return;
        const p={x,y,time},gap=pointer?Math.hypot(x-pointer.x,y-pointer.y):0;
        if(!pointer||time-pointer.time>200){points.push({x,y,born:time,hue,break:true});pointer=p;}
        else{const count=Math.min(100,Math.max(1,Math.ceil(gap/4)));for(let i=1;i<=count;i++){hue=(hue+gap/count*1.2)%360;points.push({x:pointer.x+(x-pointer.x)*i/count,y:pointer.y+(y-pointer.y)*i/count,born:time,hue});}pointer=p;}
        if(points.length>256)points.splice(0,points.length-256);
      },live(time){while(points.length&&time-points[0].born>=520)points.shift();return points;}
    };
  }
  if(typeof module==='object'&&module.exports){module.exports={normalizeAppearance,createTrailSystem};return;}

  const root = document.documentElement, dialog = document.getElementById('appearance-dialog');
  const nav = document.getElementById('nav-settings'), canvas = document.getElementById('meteor-canvas');
  const trailSwitch = document.getElementById('cursor-trail'), ctx = canvas.getContext('2d');
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let saved; try { saved = JSON.parse(localStorage.getItem(storageKey)||'null'); } catch {}
  let preferences = normalizeAppearance(saved,motion.matches), frame = null;
  const meteors = createTrailSystem();
  function resetTrail() {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null; meteors.clear();
    ctx.clearRect(0,0,canvas.width,canvas.height);
  }
  function resizeTrail() {
    resetTrail();
    const scale = Math.min(window.devicePixelRatio||1,2);
    canvas.width = Math.round(innerWidth*scale); canvas.height = Math.round(innerHeight*scale);
    ctx.setTransform(scale,0,0,scale,0,0);
  }
  function animate(time){
    const points=meteors.live(time);ctx.clearRect(0,0,innerWidth,innerHeight);
    const dark=preferences.theme==='dark',alpha=dark?.8:.62;ctx.save();
    const field=document.getElementById('playfield').getBoundingClientRect();ctx.beginPath();ctx.rect(0,0,innerWidth,innerHeight);ctx.rect(field.left,field.top,field.width,field.height);ctx.clip('evenodd');
    for(let i=1;i<points.length;i++){
      const a=points[i-1],b=points[i];if(b.break)continue;
      const fade=Math.pow(Math.max(0,1-(time-b.born)/520),1.5),brightness=dark?72:43,g=ctx.createLinearGradient(a.x,a.y,b.x,b.y);
      g.addColorStop(0,`hsla(${a.hue},85%,${brightness}%,${fade*alpha})`);g.addColorStop(1,`hsla(${b.hue},85%,${brightness}%,${fade*alpha})`);
      ctx.strokeStyle=g;ctx.lineWidth=2.4*fade+.3;ctx.shadowBlur=dark?5:2;ctx.shadowColor=`hsl(${b.hue},85%,${brightness}%)`;ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
    }
    ctx.restore();
    if(points.length)frame=requestAnimationFrame(animate);else{frame=null;meteors.resetPointer();}
  }
  function apply(save = true) {
    root.dataset.theme = preferences.theme;
    for (const button of document.querySelectorAll('[data-theme-option]')) button.setAttribute('aria-pressed',String(button.dataset.themeOption === preferences.theme));
    trailSwitch.checked = preferences.trail;
    canvas.hidden = !preferences.trail || motion.matches;
    if (canvas.hidden) resetTrail();
    document.getElementById('trail-hint').textContent = motion.matches ? '系统已开启减少动态效果，流星暂时停止显示。' : '移动鼠标时出现连续七彩尾迹，停止后消失。';
    if (save) try { localStorage.setItem(storageKey,JSON.stringify(preferences)); } catch {}
    window.dispatchEvent(new Event('appearancechange'));
  }
  for (const button of document.querySelectorAll('[data-theme-option]')) button.onclick = () => { preferences.theme = button.dataset.themeOption; apply(); };
  trailSwitch.onchange = () => { preferences.trail = trailSwitch.checked; apply(); };
  document.getElementById('appearance-reset').onclick = () => { preferences = normalizeAppearance(null,motion.matches); apply(); };
  nav.onclick = () => { if (!dialog.open) { resetTrail(); dialog.showModal(); nav.setAttribute('aria-expanded','true'); } };
  document.getElementById('appearance-close').onclick = () => dialog.close();
  dialog.addEventListener('close',()=>{ nav.setAttribute('aria-expanded','false'); nav.focus(); });
  dialog.addEventListener('click',event=>{
    const rect = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) dialog.close();
  });
  window.addEventListener('pointermove',event=>{
    if(!preferences.trail||motion.matches||dialog.open||document.hidden||event.pointerType!=='mouse'||window.ChartArcade?.isActive()){meteors.resetPointer();return;}
    if(event.target.closest?.('#playfield,#seek-track,input,select,textarea')){meteors.resetPointer();return;}
    meteors.add(event.clientX,event.clientY,performance.now());if(frame===null)frame=requestAnimationFrame(animate);
  },{passive:true});
  window.addEventListener('resize',resizeTrail);
  window.addEventListener('blur',resetTrail); window.addEventListener('pagehide',resetTrail);
  document.addEventListener('mouseleave',resetTrail);
  document.addEventListener('visibilitychange',()=>{ if (document.hidden) resetTrail(); });
  motion.addEventListener('change',()=>apply(false));
  resizeTrail(); apply(false);
})();
