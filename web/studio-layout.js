/* Shared desk geometry; preserve the existing controls and their event identities. */
(() => {
  const $=id=>document.getElementById(id),desk=$('studio-view');
  const topbar=document.querySelector('.topbar');
  const syncHeader=()=>document.documentElement.style.setProperty('--library-topbar-height',`${topbar.getBoundingClientRect().height}px`);
  new ResizeObserver(syncHeader).observe(topbar);syncHeader();
  const make=cls=>{const el=document.createElement('div');el.className=cls;return el;};
  const heading=desk.querySelector('.workspace-heading'),toolbar=make('simple-header');
  const label=document.createElement('strong');label.textContent='简易制谱台';toolbar.append(label);
  toolbar.append(heading.querySelector('.workspace-preview-controls'));
  const panelToggle=toolbar.querySelector('#preview-focus');panelToggle.textContent='收起面板';toolbar.append(panelToggle);
  const toggle=document.createElement('button');toggle.id='simple-spectrum-toggle';toggle.textContent='频谱';toggle.setAttribute('aria-pressed','true');toolbar.append(toggle);
  const body=make('simple-body'),left=make('simple-left'),transport=make('simple-transport'),actions=make('simple-transport-actions');
  const listen=$('preview-toggle');
  for(const id of ['preview-toggle','arcade-autoplay','arcade-open'])actions.append($(id));
  listen.classList.add('simple-listen');
  listen.querySelector('.sr-only').textContent='试听';
  actions.querySelector('#arcade-open').textContent='试玩';
  actions.querySelector('#arcade-open').setAttribute('aria-label','DFJK 现场试玩');
  transport.append(actions,$('seek-area'));left.append(transport,desk.querySelector('.controls'));
  const stage=make('simple-stage'),spectrum=make('simple-spectrum-column'),canvas=document.createElement('canvas');canvas.id='simple-spectrum';canvas.setAttribute('aria-label','与四轨谱面对齐的频谱');spectrum.append(canvas);
  stage.append(spectrum,$('playfield'));
  const results=desk.querySelector('.results-panel');results.querySelector('.result-actions').prepend($('audio'));
  body.append(left,stage,results);heading.append($('progress-area'),$('job-error'));
  desk.replaceChildren(toolbar,heading,body);
  toggle.onclick=()=>{const hidden=stage.classList.toggle('simple-no-spectrum');toggle.setAttribute('aria-pressed',String(!hidden));window.dispatchEvent(new Event('resize'));};
})();
