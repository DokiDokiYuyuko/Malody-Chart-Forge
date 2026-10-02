(function () {
  'use strict';
  const storageKey = 'startrail.appearance.v1';
  const themeNames = new Set(['light','peach','moon','sea','dark']);
  const meteorPalette = {
    light:{mint:'#138c9c',pink:'#b94b95',glow:3,alpha:.55},
    peach:{mint:'#bb6c77',pink:'#cf7858',glow:4,alpha:.58},
    moon:{mint:'#7165bd',pink:'#ba669f',glow:4,alpha:.6},
    sea:{mint:'#287f9a',pink:'#657ac0',glow:4,alpha:.58},
    dark:{mint:'#76ffd0',pink:'#ffa1d2',glow:8,alpha:.85}
  };
  function normalizeAppearance(value, reducedMotion = false) {
    return {theme:themeNames.has(value?.theme) ? value.theme : 'light', trail:typeof value?.trail === 'boolean' ? value.trail : !reducedMotion};
  }
  // A bounded particle system: no timer or animation work remains after the last meteor fades.
  function createMeteorSystem(random = Math.random) {
    const particles = [];
    return {
      particles,
      clear() { particles.length = 0; },
      emit(x,y,dx,dy) {
        const length = Math.hypot(dx,dy) || 1;
        particles.push({x,y,vx:dx/length*.8+(random()-.5)*.3,vy:dy/length*.8+.4,age:0,life:420+random()*220,pink:random()>.78,length:16+random()*16});
        if (particles.length > 64) particles.splice(0,particles.length-64);
      },
      step(elapsed) {
        const delta = Math.max(0,Math.min(48,Number(elapsed)||0));
        for (let i=particles.length-1;i>=0;i--) {
          const p = particles[i]; p.age += delta; p.x += p.vx*delta/16; p.y += p.vy*delta/16;
          if (p.age >= p.life) particles.splice(i,1);
        }
      }
    };
  }
  // The same engine is available to the lightweight Node regression checks.
  if (typeof module === 'object' && module.exports) { module.exports = {normalizeAppearance,createMeteorSystem}; return; }

  const root = document.documentElement, dialog = document.getElementById('appearance-dialog');
  const nav = document.getElementById('nav-settings'), canvas = document.getElementById('meteor-canvas');
  const trailSwitch = document.getElementById('cursor-trail'), ctx = canvas.getContext('2d');
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let saved; try { saved = JSON.parse(localStorage.getItem(storageKey)||'null'); } catch {}
  let preferences = normalizeAppearance(saved,motion.matches), frame = null, previousTime = 0, pointer = null;
  const meteors = createMeteorSystem();
  function resetTrail() {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null; previousTime = 0; pointer = null; meteors.clear();
    ctx.clearRect(0,0,canvas.width,canvas.height);
  }
  function resizeTrail() {
    resetTrail();
    const scale = Math.min(window.devicePixelRatio||1,2);
    canvas.width = Math.round(innerWidth*scale); canvas.height = Math.round(innerHeight*scale);
    ctx.setTransform(scale,0,0,scale,0,0);
  }
  function animate(time) {
    meteors.step(previousTime ? time-previousTime : 16); previousTime = time;
    ctx.clearRect(0,0,innerWidth,innerHeight);
    const palette = meteorPalette[preferences.theme] || meteorPalette.light;
    for (const p of meteors.particles) {
      const fade = 1-p.age/p.life, color = p.pink ? palette.pink : palette.mint;
      const magnitude = Math.hypot(p.vx,p.vy)||1;
      ctx.globalAlpha = fade*palette.alpha; ctx.strokeStyle = color; ctx.fillStyle = color;
      ctx.shadowColor = color; ctx.shadowBlur = palette.glow; ctx.lineWidth = 1.2;
      const tail = ctx.createLinearGradient(p.x,p.y,p.x-p.vx/magnitude*p.length,p.y-p.vy/magnitude*p.length);
      tail.addColorStop(0,color); tail.addColorStop(1,'transparent'); ctx.strokeStyle = tail;
      ctx.beginPath(); ctx.moveTo(p.x,p.y); ctx.lineTo(p.x-p.vx/magnitude*p.length,p.y-p.vy/magnitude*p.length); ctx.stroke();
      ctx.beginPath(); ctx.arc(p.x,p.y,1.3,0,Math.PI*2); ctx.fill();
    }
    ctx.globalAlpha = 1; ctx.shadowBlur = 0;
    if (meteors.particles.length) frame = requestAnimationFrame(animate);
    else { frame = null; previousTime = 0; }
  }
  function apply(save = true) {
    root.dataset.theme = preferences.theme;
    for (const button of document.querySelectorAll('[data-theme-option]')) button.setAttribute('aria-pressed',String(button.dataset.themeOption === preferences.theme));
    trailSwitch.checked = preferences.trail;
    canvas.hidden = !preferences.trail || motion.matches;
    if (canvas.hidden) resetTrail();
    document.getElementById('trail-hint').textContent = motion.matches ? '系统已开启减少动态效果，流星暂时停止显示。' : '移动鼠标时出现短暂流星，停止后消失。';
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
    if (!preferences.trail || motion.matches || dialog.open || document.hidden || event.pointerType !== 'mouse') return;
    // Sliders and the playfield stay clear while inspecting rhythm and dragging the timeline.
    if (event.target.closest?.('#playfield,#seek-track,input,select,textarea')) { pointer = null; return; }
    const point = {x:event.clientX,y:event.clientY,time:event.timeStamp};
    if (pointer) {
      const dx = point.x-pointer.x, dy = point.y-pointer.y;
      if (point.time-pointer.time < 12 || Math.hypot(dx,dy) < 4) return;
      const count = Math.min(3,Math.max(1,Math.floor(Math.hypot(dx,dy)/24)));
      for (let i=1;i<=count;i++) meteors.emit(pointer.x+dx*i/count,pointer.y+dy*i/count,dx,dy);
      if (frame === null) frame = requestAnimationFrame(animate);
    }
    pointer = point;
  },{passive:true});
  window.addEventListener('resize',resizeTrail);
  window.addEventListener('blur',resetTrail); window.addEventListener('pagehide',resetTrail);
  document.addEventListener('visibilitychange',()=>{ if (document.hidden) resetTrail(); });
  motion.addEventListener('change',()=>apply(false));
  resizeTrail(); apply(false);
})();
