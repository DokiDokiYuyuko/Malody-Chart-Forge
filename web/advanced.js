/* Timeline workbench. Permanent components; no relocated legacy controls. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id),T=window.AdvancedTimeline,W=window.AdvancedWorkbenchState,F=window.AdvancedWorkflow,R=window.PlayfieldRenderer;
  const store=W.create(),s=store.state,SR=44100,resultAudio=window.AdvancedResultAudio.create();
  const modeNames={prepare:'音乐与方案',segment:'调整区域',generate:'生成进度',finish:'试玩与导出'};
  const kinds={imported_final:'导入谱面',model_raw:'模型生成',arranged:'音乐编排',silence:'静音区域',fast:'快速分层',rules:'规则修订',agent:'Agent 修订',fusion:'声部合并',fusion_candidate:'声部合并',split:'继承谱面',adaptive:'节奏适配',stem_raw:'声部原谱',quality:'音符修订'};
  const patterns={balanced:'Balanced',jackspeed:'Jack',stream:'Stream',speed:'Speed',jumpstream:'Jumpstream',handstream:'Handstream',chordjack:'Chordjack',stamina:'Stamina',technical:'Technical'};
  const nav=document.createElement('button');nav.id='nav-advanced';nav.textContent='高级制谱台';$('nav-library').before(nav);
  const view=document.createElement('main');view.id='advanced-view';view.className='advanced-workspace aw-workbench';view.hidden=true;
  view.innerHTML=`
    <header class="aw-header"><div class="aw-project"><span class="aw-brand">高级制谱台</span><select id="adv-projects" aria-label="选择高级项目"><option value="">选择项目</option></select><span id="adv-project-caption">从完整音乐开始</span></div><nav class="aw-modes" aria-label="高级制谱工作模式">${Object.entries(modeNames).map(([key,label],i)=>`<button id="aw-mode-${key}" data-mode="${key}" aria-pressed="${i===0}"><span>${i+1}</span>${label}</button>`).join('')}</nav><div class="aw-controls"><div class="aw-preview-toolbar"><select id="adv-variant" aria-label="选择排键与难度"></select><label class="aw-speed">流速<input id="adv-speed" type="range" min="1" max="16" step=".1" value="8"><output id="adv-speed-value">8.0×</output></label><button id="aw-spectrum-toggle" aria-pressed="true">频谱</button></div></div><button id="aw-new">导入音乐</button><button id="aw-panel-toggle" aria-expanded="true" aria-controls="aw-inspector">收起面板</button></header>
    <div id="adv-notice" class="aw-notice" role="status" aria-live="polite" hidden></div>
    <section class="aw-timeline" aria-label="完整原曲时间轴"><div class="aw-timeline-head"><strong id="adv-wave-title">完整原曲时间轴</strong><span id="aw-timeline-context">分析建议和实际片段分别显示</span><span id="aw-job-summary" role="status">尚未打开音乐</span><label>缩放<input id="adv-zoom" type="range" min="1" max="24" step="1" value="1"></label><input id="adv-pan" type="range" min="0" max="1" step=".001" value="0" aria-label="移动时间轴"></div><div id="aw-wave-stage"><canvas id="adv-wave" aria-label="点击定位，划段模式拖动选择范围"></canvas><div id="aw-boundaries"></div></div></section>
    <div class="aw-body"><section class="aw-transport" aria-label="音乐播放控制"><div class="aw-transport-actions"><button id="adv-play" class="aw-play-button">▶ 试听</button><button id="adv-autoplay">Autoplay</button><button id="adv-play-game">DFJK 试玩</button></div><div class="aw-transport-position"><span id="adv-current-time">00:00.000</span><input id="adv-seek" type="range" min="0" max="1" step=".001" value="0" aria-label="音乐播放时间"><span id="adv-total-time">00:00.000</span></div><div class="aw-transport-source"><span id="aw-playing-source">当前声音：原曲</span><details class="aw-play-options"><summary>选项</summary><div class="aw-play-options-body"><label class="aw-check"><input id="adv-loop" type="checkbox">循环选区</label><label class="aw-check"><input id="aw-match-volume" type="checkbox">音量匹配</label><p id="adv-preview-note">音乐与谱面共用时间轴</p></div></details></div><select id="adv-listen-source" aria-label="试听音轨"><option value="original">试听原曲</option></select><audio id="adv-audio" preload="auto"></audio></section><aside class="aw-segments"><header><h2>音乐片段</h2><span id="adv-segment-count">0 段</span></header><p class="aw-segment-help">点击片段查看谱面</p><div id="adv-segments"></div></aside>
    <section class="aw-preview">
      <div id="adv-spectrum-stage" class="aw-playfield"><div class="aw-spectrum-column"><canvas id="adv-spectrum" aria-label="与四轨谱面对齐的频谱，点击或拖动定位"></canvas></div><div id="adv-game-host"><div class="aw-chart-canvases"><canvas id="adv-chart-before" aria-label="当前四轨谱面"></canvas><canvas id="adv-chart-after" aria-label="比较谱面" hidden></canvas></div><div id="adv-preview-empty"><span class="aw-empty-tracks" aria-hidden="true">Ⅰ Ⅱ Ⅲ Ⅳ</span><strong>还没有谱面</strong><p>打开完整音乐，在时间轴上划段后生成。</p><button id="aw-empty-action">打开音乐</button></div></div></div>

    </section>
    <aside id="aw-inspector" class="aw-inspector"><header><div><h2 id="aw-panel-title">准备音乐</h2><p id="aw-panel-subtitle">导入完整音乐，再选择制谱输入。</p></div></header><div class="aw-comparison-bar" id="aw-comparison-bar" hidden><span>同位置比较</span><button id="aw-slot-a" aria-pressed="true">A 正在使用</button><button id="aw-slot-b" aria-pressed="false">B 新方案</button><label><input id="aw-parallel" type="checkbox">并排</label><button id="aw-end-compare">结束比较</button></div><div id="aw-delivery" class="aw-delivery" role="status" hidden></div><div class="aw-panel-scroll">
      <section id="aw-panel-prepare" class="aw-mode-panel"><div id="aw-import"><h3>打开完整音乐</h3><label class="aw-field">本地音乐或 MCZ<label class="aw-file-button" for="adv-file">选择文件</label><input id="adv-file" type="file" accept="audio/*,.mcz,.flac,.m4a,.opus,.webm,.aac" hidden></label><button id="adv-open-music-folder" type="button">打开音乐文件夹</button><label class="aw-field">音乐库与已有曲包<select id="adv-import-source"><option value="">选择已下载音乐</option></select></label><button id="adv-import-existing" class="aw-primary">导入所选音乐</button></div><div id="aw-audio-info"></div><section class="aw-block"><label class="aw-field">曲名<input id="aw-song-title" maxlength="120" required></label><label class="aw-field">音乐人<input id="aw-song-artist" maxlength="120"></label><label class="aw-field">谱师名字<input id="aw-song-creator" maxlength="120" value="Startrail" required></label><button id="aw-save-metadata">保存歌曲信息</button><label class="aw-check"><input id="aw-keep-tail" type="checkbox">保留静音片尾</label><p id="aw-trim-info" class="aw-hint"></p></section><section class="aw-block"><h3>制谱输入</h3><label class="aw-field">生成用哪路音乐<select id="aw-input-source"><option value="original">原曲</option></select></label><p class="aw-hint">试听切换只改变你听到的声音。</p></section><section class="aw-block"><h3>人声与伴奏分离</h3><label class="aw-field">分离模型<select id="aw-separation-model"></select></label><p id="aw-separation-availability" class="aw-hint"></p><button id="aw-separate" class="aw-primary">分离完整原曲</button><details><summary>分离参数与局部试听</summary><div id="aw-separation-params"></div><button id="aw-trial">试听当前选区分离效果</button><p class="aw-hint">局部结果只供试听，不作为整曲输入。</p></details><div id="aw-separation-tasks"></div><div id="aw-stem-versions"></div><div id="aw-trials"></div></section></section>
      <section id="aw-panel-segment" class="aw-mode-panel" hidden><section class="aw-block aw-analysis-block"><h3>用音乐分析辅助划段</h3><p class="aw-hint">先看时间轴上的建议，再决定如何划段。</p><button id="aw-analyze" class="aw-primary">分析节奏并预览划段</button><p id="aw-analysis-status" role="status"></p><div id="aw-draft-tools" hidden><strong id="aw-draft-summary"></strong><p class="aw-hint">拖动图上的边界调整；已有片段边界保留。</p><div id="aw-draft-rows"></div><button id="aw-refresh-draft">刷新并重新确认</button><button id="aw-discard-draft">取消建议</button></div></section><section class="aw-block"><h3>手动划段</h3><label class="aw-field">片段名称<input id="aw-segment-name" maxlength="80" placeholder="例如：想重做的段落"></label><div class="aw-field-pair"><label class="aw-field">开始<input id="adv-start" value="00:00.000" inputmode="decimal"></label><label class="aw-field">结束<input id="adv-end" value="00:00.000" inputmode="decimal"></label></div><div class="aw-inline-actions"><button id="adv-start-now">当前位置设为开始</button><button id="adv-end-now">设为结束</button></div><label class="aw-check"><input id="adv-snap" type="checkbox">节拍吸附</label><button id="adv-add">保存新片段</button><button id="aw-update-segment">更新当前片段</button><div class="aw-inline-actions"><button id="aw-split">在当前位置拆分</button><button id="adv-next">接着划下一段</button></div><button id="aw-undo">撤销上次划段</button></section><details class="aw-block"><summary>节拍参考与分析依据</summary><p class="aw-hint">BPM 是参考拍速。节奏变密并不一定是 BPM 变化。</p><div id="aw-tempo-points"></div><button id="aw-add-tempo">在当前位置添加 BPM</button><div id="aw-analysis-details"></div></details></section>
      <section id="aw-panel-generate" class="aw-mode-panel" hidden><label class="aw-field">生成范围<select id="aw-generation-scope"><option value="all">全部片段</option><option value="current">当前片段</option><option value="custom">指定片段</option></select></label><div id="aw-generation-selection" class="aw-generation-selection" hidden></div><div class="aw-generation-scope"><strong id="aw-generation-count">先选择音乐片段</strong><p id="aw-generation-estimate"></p></div><label class="aw-field">音乐输入<select id="aw-generation-source"></select></label><div class="aw-field-pair"><label class="aw-field">目标玩法<select id="aw-profile"><option value="keyboard">键盘 4K</option></select></label><label class="aw-field">生成模型<select id="aw-engine"><option value="v32">Mapperatorinator V32</option><option value="mug">MuG Diffusion</option></select></label></div><fieldset class="aw-options"><legend>排键方式</legend><div id="aw-patterns"></div></fieldset><fieldset class="aw-options"><legend>难度</legend><div id="aw-difficulties"></div></fieldset><button id="aw-regenerate">只重生成当前组合</button><p class="aw-hint">新结果作为候选保留，由你决定是否采用。</p><details class="aw-block"><summary>模型参数与分层策略</summary><label class="aw-field">设置保存范围<select id="aw-settings-scope"><option value="project">项目模板</option><option value="segment">仅当前片段</option></select></label><p id="aw-overrides-note" class="aw-hint"></p><button id="aw-clear-overrides">当前片段恢复继承</button><div id="aw-generation-params"></div><button id="aw-save-settings">保存设置，不生成</button></details><div id="aw-generation-tasks"></div></section>
      <section id="aw-panel-finish" class="aw-mode-panel" hidden><div class="aw-finish-tabs"><button id="aw-show-candidates" aria-pressed="true">选谱</button><button id="aw-show-export" aria-pressed="false">导出</button></div><div id="aw-candidate-area"><div class="aw-result-heading"><strong id="aw-result-title">当前片段</strong><button id="aw-result-history">历史结果</button></div><div id="aw-result-history-list" hidden></div><div id="adv-versions"></div><div id="aw-batch-results"></div><button id="aw-use-selected">采用本次勾选的成功结果</button><details class="aw-block"><summary>更多修谱工具</summary><button id="aw-rule">规则修订当前预览</button><button id="aw-fusion-tool">调整声部合并</button><button id="aw-agent-tool">Agent 分析与修订</button><div id="aw-tool-fusion" hidden></div><div id="aw-tool-agent" hidden><button id="adv-api-open">配置 Agent API</button><label class="aw-field">希望改善什么<textarea id="adv-goal">保留音乐节奏与排键意图，减少没有明确采音依据的拥挤音符。</textarea></label><label class="aw-check"><input id="adv-send-audio" type="checkbox">允许发送这段音频</label><p class="aw-hint">从播放位置分析最多 12 秒，保留上下文。</p><button id="adv-review">分析正在使用的版本</button><div id="aw-review-tasks"></div></div></details><details class="aw-block"><summary>原始声部与历史版本</summary><div id="aw-raw-versions"></div><button id="aw-layout-history">查看划段前的历史</button><div id="aw-old-layouts"></div></details><details class="aw-block"><summary>问题报告与版本评审</summary><div id="adv-quality"></div><select id="aw-review-rating" aria-label="评审当前预览版本"><option value="unreviewed">未评</option><option value="usable">可用</option><option value="needs_changes">待改</option></select><input id="aw-review-reason" placeholder="评审原因（可选）"><button id="aw-save-review">保存评审</button><button id="aw-add-example">加入认可示例库</button></details><div id="aw-evaluation" hidden><span>更喜欢哪一版？</span><button data-preference="before">A 更好</button><button data-preference="after">B 更好</button><button data-preference="equal">两版相当</button></div></div><div id="aw-export-area" hidden><h3>只导出你采用的谱面</h3><div id="adv-assemblies"></div><div id="aw-export-segments" class="aw-export-segments"></div><div id="aw-export-summary"></div><label class="aw-field">准备时间（秒）<input id="adv-preroll" type="number" min="0" max="3" step=".1" value="1.5"></label><button id="aw-leave-assembly" hidden>返回原曲工作台</button></div></section>
    </div><footer id="aw-next-action"><button id="aw-apply-segmentation" class="aw-primary" hidden>应用建议划段</button><button id="aw-generate" class="aw-primary" hidden>生成所选片段</button><button id="adv-assemble" class="aw-primary" hidden>导出 MCZ</button><button id="aw-next-mode">开始划段</button></footer></aside></div>
`;
  $('studio-view').after(view);
  // One music input and one target selection stay in the main workflow.
  const setup=$('aw-panel-prepare'),target=document.createElement('section');target.className='aw-block aw-targets';
  target.innerHTML='<h3>目标谱面</h3><label class="aw-field">目标难度<select id="aw-target-difficulty"></select></label><label class="aw-field">排键方式<select id="aw-target-pattern"></select></label><button id="aw-batch-settings">多个谱面</button>';
  const inputSection=$('aw-input-source').closest('section');target.append($('aw-input-source').closest('label'));inputSection.remove();
  const fusionBlock=document.createElement('div');fusionBlock.id='aw-fusion-controls';fusionBlock.hidden=true;
  fusionBlock.innerHTML='<label class="aw-field">合并冲突处理<select id="aw-fusion-mode"></select></label><p id="aw-fusion-hint" class="aw-hint"></p><label class="aw-field" id="aw-fusion-primary-field">挪不开时保留<select id="aw-fusion-primary"></select></label>';
  target.append(fusionBlock);
  fusionBlock.querySelector('#aw-fusion-mode').replaceChildren(...W.fusionModes.map(m=>new Option(m.label,m.value)));
  fusionBlock.querySelector('#aw-fusion-primary').replaceChildren(...W.fusionPrimaries.map(m=>new Option(m.label,m.value)));
  setup.append(target,$('aw-panel-segment').querySelector('.aw-analysis-block'));
  $('aw-generation-source').closest('label').hidden=true;
  $('aw-analyze').closest('.aw-analysis-block').querySelector('h3').textContent='区域方案';
  $('aw-next-action').prepend($('aw-analyze'));
  const regionControl=document.createElement('label');regionControl.className='aw-field';
  regionControl.innerHTML='区域划分<select id="aw-region-granularity"><option value="coarse">少一些</option><option value="balanced" selected>标准</option><option value="fine">细一些</option></select>';
  $('aw-analysis-status').before(regionControl);
  $('aw-analyze').textContent='开始处理';$('aw-analyze').hidden=true;
  $('aw-analysis-status').textContent='';
  const settings=document.createElement('dialog');settings.id='aw-settings-dialog';settings.className='aw-settings-dialog';
  settings.innerHTML='<header><h2>制谱设置</h2><button id="aw-settings-close" aria-label="关闭设置">关闭</button></header><div class="aw-settings-content"></div>';
  view.append(settings);
  const settingContent=settings.querySelector('.aw-settings-content');
  const batchTargets=document.createElement('section');batchTargets.id='aw-batch-targets';batchTargets.className='aw-block';batchTargets.innerHTML='<h3>多组合生成</h3>';
  batchTargets.append($('aw-difficulties').closest('fieldset'),$('aw-patterns').closest('fieldset'));settingContent.append(batchTargets);
  settingContent.append($('aw-song-title').closest('section'),$('aw-separate').closest('section'),$('aw-profile').closest('.aw-field-pair'),$('aw-save-settings').closest('details'));
  settingContent.append($('aw-generation-scope').closest('label'),$('aw-generation-selection'));
  $('aw-generation-count').closest('.aw-generation-scope').hidden=true;$('aw-regenerate').hidden=true;
  for(const detail of settingContent.querySelectorAll('details.aw-block')){detail.open=true;detail.classList.add('aw-settings-section');}
  const settingButton=document.createElement('button');settingButton.id='aw-settings-open';settingButton.textContent='设置';
  $('aw-new').before(settingButton);settingButton.onclick=()=>settings.showModal();$('aw-settings-close').onclick=()=>settings.close();
  $('aw-batch-settings').onclick=()=>{settings.showModal();batchTargets.scrollIntoView({block:'start'});};
  const importDialog=document.createElement('dialog');importDialog.id='aw-import-dialog';importDialog.className='aw-settings-dialog aw-import-dialog';importDialog.innerHTML='<header><h2>导入音乐</h2><button id="aw-import-close">关闭</button></header><div class="aw-settings-content"></div>';
  importDialog.querySelector('.aw-settings-content').append($('aw-import'));view.append(importDialog);$('aw-import-close').onclick=()=>importDialog.close();
  const compareAdopt=document.createElement('button');compareAdopt.id='aw-adopt-comparison';compareAdopt.textContent='采用新版';compareAdopt.className='aw-primary';const compareKeep=document.createElement('button');compareKeep.id='aw-keep-comparison';compareKeep.textContent='保留原版';$('aw-comparison-bar').append(compareAdopt,compareKeep);$('aw-end-compare').hidden=true;

  const audio=$('adv-audio'),spectrumCanvas=$('adv-spectrum'),pane=window.SpectrumPane?.create(spectrumCanvas),cache=new Map(),preparedCache=new WeakMap();
  let playbackIntent=false,pendingSource=null;
  let continuous=null,continuousEpoch=0;
  async function prepareContinuous(data){
    const epoch=++continuousEpoch,pid=s.project?.id,key=s.variant;
    continuous=null;if(!s.project||s.assembly)return;
    const choices=W.playbackChoices(s.project,key,data,s.resultBatch);
    const rows=await Promise.all(choices.map(async row=>{
      try{return {...row,data:row.revisionId?await getRevision(row.revisionId):null};}
      catch(error){return {...row,data:null,error:error.message};}
    }));
    if(epoch!==continuousEpoch||pid!==s.project?.id||key!==s.variant||s.assembly)return;
    continuous={pid,key,rows,events:rows.flatMap(row=>row.data?.events||[])};
    draw();
  }
  function continuousEnabled(){return continuous&&continuous.pid===s.project?.id&&continuous.key===s.variant&&
    !s.assembly&&!s.compare&&!activeTrial&&!pendingSource&&!$('adv-loop').checked&&!window.ChartArcade?.isActive();}
  function followPlayback(){
    if(!continuousEnabled()||audio.paused)return;
    const row=W.playbackPart(continuous.rows,Math.round(position()*SR));
    if(!row||row.segment.id===s.segmentId)return;
    ++s.sequence;s.segmentId=row.segment.id;s.preview=row.data;
    range=[row.segment.start_sample/SR,row.segment.end_sample/SR];
    resultAudio.select(row.data,{project:s.project.id,segment:s.segmentId,variant:s.variant});
    // Following the clock must never reload or seek the shared audio player.
    if(resultAudio.resolve(s.stems).source!==s.source)resultAudio.override(s.source);
    renderSegments();renderRange();renderVersions();renderGenerationCount();
    const list=$('adv-segments'),current=list.querySelector('.is-current');
    if(current){const a=list.getBoundingClientRect(),b=current.getBoundingClientRect();
      if(b.top<a.top)list.scrollTop+=b.top-a.top;
      else if(b.bottom>a.bottom)list.scrollTop+=b.bottom-a.bottom;}
    if(row.error)notice('这一段谱面加载失败：'+row.error,true);
  }
  function pauseAudio(){playbackIntent=false;audio.pause();}
  let visible=false,poll=0,polling=false,raf=0,projectEpoch=0,draftEpoch=0,audioEpoch=0,spectrumEpoch=0,noticeTimer=0,defaults=null,models=[],range=[0,0],cuts=[],draftIncluded=null,drag=null,analysisBusy=false,busy=new Set(),inputSource='original',generationDraft={},mugStrategyPreference='independent',sourceSignature='',versionSignature='',sourceMeta={},activeTrial=null,wholeEvents=[],wholeSignature='',reviewTask=null,boundarySignature='',pendingSubmission=null,enterPromise=null,historyOpen=false,resultEpoch=0,localCandidateIds=new Set(),historicalCandidateIds=new Set();
  const node=(tag,text,cls)=>{const el=document.createElement(tag);if(text!=null)el.textContent=text;if(cls)el.className=cls;return el;};
  const option=(label,value)=>new Option(label,value);
  let seekDragging=false, arcadeTransport=null, spectrumProjection=null, spectrumDrag=null;
  function action(text,fn,cls){const b=node('button',text,cls);b.type='button';b.onclick=()=>run(fn);return b;}
  function notice(message,error=false){clearTimeout(noticeTimer);const n=$('adv-notice');n.hidden=!message;n.textContent=message;n.dataset.error=String(error);if(!error)noticeTimer=setTimeout(()=>{n.hidden=true;},7000);}
  async function api(path,method='GET',body){if(method==='POST'&&/\/(generation-batches|confirm-and-generate|generate)$/.test(path)&&(body?.settings?.engine||generationDraft?.engine)==='v32'){const current=await fetch('/api/health').then(r=>r.json());if(!(current.nps_star_direct_version>=1))throw new Error('请重启本地制谱服务，加载 NPS 范围独立生成版本。');}const args={method};if(body instanceof FormData)args.body=body;else if(body!==undefined){args.headers={'Content-Type':'application/json'};args.body=JSON.stringify(body);}const response=await fetch('/api/advanced'+path,args);const data=await response.json();if(!response.ok)throw new Error(F.requestErrorMessage(path,response.status,data.detail));if(method==='POST'&&/\/(generation-batches|confirm-and-generate|generate)$/.test(path))window.notifyGenerationChanged?.();return data;}
  async function run(fn){try{return await fn();}catch(e){notice(e.message,true);return null;}}
  function base(){if(!s.project)throw new Error('请先导入完整音乐');return '/projects/'+s.project.id;}
  function segment(){return s.project?.segments.find(x=>x.id===s.segmentId);}
  function variants(){return s.assembly?s.assembly.charts:s.project?.variants||[];}
  function selectedData(){return s.compare&&s.slot==='b'?s.compare:s.preview;}
  function position(){if(arcadeTransport&&window.ChartArcade?.isActive())return (window.ChartArcade.position()+arcadeTransport.offset)/1000;return pendingSource?.at??(audio.currentTime+(activeTrial?.offset||0));}
  async function exclusive(id,fn){if(busy.has(id))return;busy.add(id);const b=$(id),label=b?.textContent;if(b){b.disabled=true;b.textContent='处理中…';}try{return await fn();}finally{busy.delete(id);if(b){b.disabled=false;b.textContent=label;}if(id==='aw-analyze')syncPrimaryAction();if(id==='adv-assemble')renderExport();}}
  function bind(id,fn){$(id).onclick=()=>run(()=>exclusive(id,fn));}
  function changeMode(mode){if(s.submittedBatch&&mode!==s.mode)s.submittedBatch.autoAllowed=false;s.mode=mode;view.dataset.mode=mode;for(const key of W.modes){$('aw-panel-'+key).hidden=key!==mode;$('aw-mode-'+key).setAttribute('aria-pressed',String(key===mode));}$('aw-panel-title').textContent=modeNames[mode];$('aw-panel-subtitle').textContent={prepare:'先听音乐，选择后续生成的输入。',segment:'分析给出建议，应用后才成为实际片段。',generate:'选择生成范围，一次生成多个组合。',finish:'比较新方案，采用后再导出。'}[mode];syncPrimaryAction();$('aw-next-mode').textContent={prepare:'开始划段',segment:'为片段生成谱面',generate:'比较生成结果'}[mode]||'';if(mode==='finish')renderVersions();if(mode==='generate')renderGenerationCount();renderSegments();drawTimeline();}
  function draftChanges(){return s.draft&&JSON.stringify(s.draft.segments.map(r=>[r.start_sample,r.end_sample,r.included]))!==JSON.stringify(s.project.segments.map(r=>[r.start_sample,r.end_sample,r.included]));}
  function syncPrimaryAction(){const preparing=['prepare','segment'].includes(s.mode);$('aw-analyze').hidden=!preparing||!s.project||!!s.draft;$('aw-analyze').disabled=analysisBusy;$('aw-analyze').textContent=analysisBusy?'正在处理…':'开始处理';$('adv-assemble').hidden=s.mode!=='finish';$('aw-apply-segmentation').hidden=!preparing||!s.draft;$('aw-generate').hidden=true;$('aw-next-mode').hidden=s.mode==='finish'||s.mode==='generate'||s.mode==='prepare'||(s.mode==='segment'&&!!draftChanges());}
  for(const mode of W.modes)$('aw-mode-'+mode).onclick=()=>changeMode(mode);
  $('aw-next-mode').onclick=()=>changeMode(W.modes[Math.min(3,W.modes.indexOf(s.mode)+1)]);
  $('aw-panel-toggle').onclick=()=>{const folded=view.classList.toggle('aw-panel-folded');$('aw-panel-toggle').setAttribute('aria-expanded',String(!folded));$('aw-panel-toggle').textContent=folded?'展开面板':'收起面板';draw();};
  $('aw-new').onclick=()=>importDialog.showModal();
  async function listProjects(){const data=await api('/projects');$('adv-projects').replaceChildren(option('选择项目',''),...data.projects.map(p=>option(p.title,p.id)));$('adv-projects').value=s.project?.id||'';}
  async function importSources(){const [history,music,registry]=await Promise.all([fetch('/api/history?page_size=100').then(r=>r.json()),fetch('/api/music').then(r=>r.json()),fetch('/api/music-assets').then(r=>r.json())]);const options=[option('选择已下载音乐','')];for(const m of registry.assets||[])options.push(option('音乐 · '+m.title,'asset:'+m.id));for(const m of (Array.isArray(music)?music:music.items||music.tracks||[]))if(m.status==='ready'&&!(registry.assets||[]).some(a=>a.video_id===(m.video_id||m.id)))options.push(option('音乐 · '+m.title,'youtube:'+(m.id||m.video_id)));for(const j of history.items||[])if(j.status==='completed')options.push(option('含谱曲包 · '+j.title,'job:'+(j.job_id||j.id)));$('adv-import-source').replaceChildren(...options);}

  function setProjectLoading(value){view.setAttribute('aria-busy',String(value));for(const el of view.querySelectorAll('.aw-body,.aw-timeline,.aw-transport'))el.inert=value;}
  async function openProject(id){importDialog.close();const epoch=++projectEpoch;++audioEpoch;++spectrumEpoch;pendingSource=null;resultAudio.reset();++draftEpoch;++s.sequence;window.ChartArcade?.close();pauseAudio();setProjectLoading(true);notice('正在打开完整音乐…');try{const project=await api('/projects/'+id);if(epoch!==projectEpoch)return;s.project=project;s.segmentId=project.segments[0]?.id||null;s.variant=project.variants[0]?.key||null;s.selected=new Set();s.generationScope='all';s.resultBatch=null;s.adoption=new Set();s.submittedBatch=null;historyOpen=false;localCandidateIds.clear();historicalCandidateIds.clear();resultAudio.reset();++resultEpoch;$('aw-generation-scope').value='all';$('aw-delivery').hidden=true;$('aw-generation-tasks').replaceChildren();$('aw-result-history-list').hidden=true;s.preview=s.compare=s.assembly=s.draft=s.plan=null;s.slot='a';s.parallel=false;s.tasks=[];s.stems=[];activeTrial=null;reviewTask=null;cache.clear();sourceSignature=versionSignature=wholeSignature='';pendingSubmission=null;boundarySignature='';wholeEvents=[];cuts=[];cutsEdited=false;regionChoices.clear();inputSource=project.workflow?.source_id==='vocals_accompaniment'?(project.workflow.stem_set_id?'dual:'+project.workflow.stem_set_id:'vocals_accompaniment'):project.workflow?.source_id||localStorage.getItem('startrail.advanced-source.'+id)||'original';analysisBusy=false;s.musicAnalysis=null;schemePending=false;++schemeEpoch;clearTimeout(schemeTimer);generationDraft=W.normalizeFusion(structuredClone(project.settings));range=segment()?[segment().start_sample/SR,segment().end_sample/SR]:[0,project.duration];localStorage.setItem('startrail.advanced-project',id);$('adv-projects').value=id;await setSource('original',0,false);if(epoch!==projectEpoch)return;const analysis=await api('/projects/'+id+'/section-plans');if(epoch!==projectEpoch)return;s.plan=analysis.plans[0]||null;if(s.plan){renderAnalysis();$('aw-analysis-status').textContent='已保存分析 · '+s.plan.sections.length+' 个建议区间';}else{$('aw-analysis-status').textContent='';$('aw-analysis-details').replaceChildren();}renderDraft();initGeneration();renderProject();await pollTasks();if(epoch!==projectEpoch)return;await loadSelected(segment()?.active?.[s.variant]);if(epoch!==projectEpoch)return;changeMode(s.mode);await listProjects();if(epoch!==projectEpoch)return;notice('已打开完整音乐 · '+T.time(project.duration));$('aw-analysis-status').textContent='待处理';syncPrimaryAction();}finally{if(epoch===projectEpoch)setProjectLoading(false);}}
  $('adv-projects').onchange=()=>run(()=>openProject($('adv-projects').value));
  bind('adv-import-existing',async()=>{const raw=$('adv-import-source').value;if(!raw)throw new Error('请选择已下载音乐或曲包');const [type,id]=raw.split(':');await createProjectWithCreator('/projects/from-source',{type,id});changeMode('prepare');});
  $('adv-open-music-folder').onclick=()=>run(async()=>{const response=await fetch('/api/music-assets/open-folder',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(!response.ok){const data=await response.json();throw new Error(data.detail||'无法打开音乐文件夹');}});
  $('adv-file').onchange=()=>run(()=>exclusive('adv-import-existing',async()=>{const file=$('adv-file').files[0];if(!file)return;const form=new FormData();form.append('file',file);form.append('title',file.name.replace(/\.[^.]+$/, ''));notice('正在导入完整音乐…');await createProjectWithCreator('/projects/upload',form);changeMode('prepare');$('adv-file').value='';}));
  // Metadata drafts belong to one project opening; polling only updates untouched fields.
  let metadataDraft=null,metadataSaveQueue=Promise.resolve();
  function metadataState(){
    const p=s.project;
    if(!metadataDraft||metadataDraft.pid!==p?.id||metadataDraft.epoch!==projectEpoch)metadataDraft={pid:p?.id,epoch:projectEpoch,title:p?.title||'',artist:p?.artist||'',creator:p?.creator??'Startrail',dirty:{title:false,artist:false,creator:false}};
    return metadataDraft;
  }
  function renderMetadata(){
    const draft=metadataState();
    for(const field of ['title','artist','creator']){if(!draft.dirty[field])draft[field]=field==='creator'?(s.project?.creator??'Startrail'):(s.project?.[field]||'');$('aw-song-'+field).value=draft[field];}
  }
  for(const field of ['title','artist','creator'])$('aw-song-'+field).oninput=()=>{const draft=metadataState();draft[field]=$('aw-song-'+field).value;draft.dirty[field]=true;};
  function captureMetadata(force=false){
    if(!s.project)throw new Error('先打开音乐');
    const draft=metadataState(),values={},fields=[];
    for(const field of ['title','artist','creator']){const value=$('aw-song-'+field).value;if(value!==draft[field])draft.dirty[field]=true;draft[field]=value;values[field]=value;if(force||draft.dirty[field]||(field==='creator'&&s.project.creator==null))fields.push(field);}
    if(!values.creator.trim()||values.creator.trim().length>120)throw new Error('谱师名字需填写 1–120 个字符。');
    return {pid:draft.pid,epoch:draft.epoch,values,fields};
  }
  function assertMetadataContext(snapshot){
    if(snapshot.pid!==s.project?.id||snapshot.epoch!==projectEpoch)throw new Error('项目已切换，请重新确认歌曲信息后提交');
  }
  async function saveMetadata(snapshot=captureMetadata()){
    const save=metadataSaveQueue.catch(()=>{}).then(async()=>{
      assertMetadataContext(snapshot);
      const fields=snapshot.fields.filter(field=>(field==='creator'?snapshot.values[field].trim():snapshot.values[field])!==s.project[field]);
      if(!fields.length){const draft=metadataState();for(const field of snapshot.fields)if(draft[field]===snapshot.values[field])draft.dirty[field]=false;return false;}
      const payload={expected_revision:s.project.revision};for(const field of fields)payload[field]=field==='creator'?snapshot.values[field].trim():snapshot.values[field];
      const project=await api('/projects/'+snapshot.pid,'PATCH',payload);
      assertMetadataContext(snapshot);
      if(project.revision<s.project.revision)throw new Error('歌曲信息保存期间项目已更新，请确认后重新保存');
      const draft=metadataState();
      for(const field of snapshot.fields)if(draft[field]===snapshot.values[field])draft.dirty[field]=false;
      await acceptProject(project,false);assertMetadataContext(snapshot);return true;
    });
    metadataSaveQueue=save;return save;
  }
  async function createProjectWithCreator(path,body){
    const epoch=projectEpoch,creator=$('aw-song-creator').value.trim();
    if(!creator||creator.length>120)throw new Error('谱师名字需填写 1–120 个字符。');
    if(body instanceof FormData)body.set('creator',creator);else body={...body,creator};
    const project=await api(path,'POST',body);
    if(epoch!==projectEpoch)throw new Error('项目已切换，请重新确认歌曲信息后提交');
    // Carry edits made while creation was pending across the new project's first render.
    const latest=$('aw-song-creator').value;
    await openProject(project.id);
    if(projectEpoch!==epoch+1||s.project?.id!==project.id)throw new Error('项目已切换，请重新确认歌曲信息后提交');
    const draft=metadataState();
    if(!draft.dirty.creator){draft.creator=latest;draft.dirty.creator=true;renderMetadata();}
    const metadata=captureMetadata();await saveMetadata(metadata);assertMetadataContext(metadata);
  }
  function renderProject(){const p=s.project;view.classList.toggle('aw-has-project',!!p);renderMetadata();$('aw-keep-tail').checked=p?.tail_trim?.enabled===false;$('aw-keep-tail').disabled=!p;$('aw-trim-info').textContent=p?.tail_trim?.cutoff_sample<p?.samples?'静音片尾从 '+T.time(p.tail_trim.cutoff_sample/SR)+' 开始，'+(p.tail_trim.enabled?'不纳入成品。':'保留在成品中。'):'未发现需要排除的静音片尾。';$('adv-project-caption').textContent=p?p.title:'从完整音乐开始';$('adv-project-caption').title=p?.title||'';$('adv-wave-title').textContent=p?.title||'完整原曲时间轴';$('aw-audio-info').replaceChildren();if(p){$('aw-audio-info').append(node('strong',T.time(p.duration)+' · 完整原曲'),node('p',p.artist,'aw-hint'));}$('adv-variant').replaceChildren(...variants().map(v=>option((patterns[v.pattern]||v.pattern)+' · '+v.difficulty,v.key)));$('adv-variant').value=s.variant||'';renderSegments();renderVersions();renderExport();renderTempo();renderRange();draw();}
  function renderSegments(){
    const p=s.project,host=$('adv-segments'),scrollTop=host.scrollTop;$('adv-segment-count').textContent=(p?.segments.length||0)+' 段';const signature=JSON.stringify([p?.segments,s.segmentId,s.variant,s.tasks.map(t=>[t.id,t.status,t.phase,t.progress]),s.resultBatch?.results,busy.has('generation-submission'),busy.has('aw-regenerate')]);if(host.dataset.signature===signature)return;host.dataset.signature=signature;host.replaceChildren();
    if(!p?.segments.length){host.append(node('p','还没有实际片段。分析后应用建议，或在时间轴拖动划段。','aw-hint'));return;}
    for(const part of p.segments){
      const row=node('article',null,'aw-segment'+(part.id===s.segmentId?' is-current':''));row.dataset.segment=part.id;
      const button=action(part.name,()=>chooseSegment(part.id),'aw-segment-link');button.setAttribute('aria-current',part.id===s.segmentId?'true':'false');
      const adopted=Object.keys(part.active||{}).length;
      const active=s.tasks.find(t=>t.segment_id===part.id&&['queued','running','paused'].includes(t.status));
      const delivered=s.resultBatch?.results.some(r=>r.segment_id===part.id&&r.primary);
      const latest=W.taskFor(part,s.tasks),failed=latest&&['failed','interrupted'].includes(latest.status);
      const excluded=part.start_sample>=W.contentEnd(p);
      const status=excluded?'静音尾段 · 已排除':active?F.taskPresentation(active).status:failed?'生成失败 · 可重试':adopted?'已采用 '+adopted+'/'+p.variants.length:delivered?'本次新方案待选择':'尚未采用谱面';
      row.append(button,node('p',T.time(part.start_sample/SR)+'–'+T.time(part.end_sample/SR),'aw-segment-time'),node('span',status,'aw-status'));
      if(!excluded&&s.variant){const regenerate=action('重做这一区域',()=>regenerateCurrent(part.id,s.variant),'aw-segment-regenerate');regenerate.setAttribute('aria-label','重新生成'+part.name+' · 当前组合');regenerate.title='按当前组合和生成设置重新生成'+part.name;regenerate.disabled=!!active||busy.has('generation-submission')||busy.has('aw-regenerate');row.append(regenerate);}
      if(active)row.dataset.status=active.status;else if(failed)row.dataset.status='failed';host.append(row);
    }
    host.scrollTop=scrollTop;
  }



  async function chooseSegment(id){
    if(s.submittedBatch)s.submittedBatch.autoAllowed=false;
    const part=s.project?.segments.find(x=>x.id===id);if(!part)return;
    historyOpen=false;$('aw-result-history-list').hidden=true;
    window.ChartArcade?.close();s.segmentId=id;s.assembly=null;range=[part.start_sample/SR,part.end_sample/SR];
    const candidate=s.resultBatch?.results.find(r=>r.segment_id===id&&r.variant===s.variant&&r.primary&&r.adoptable);
    await loadSelected(candidate?.revision_id||part.active?.[s.variant],range[0]);
    renderRange();renderSegments();renderGenerationCount();
    if($('aw-settings-scope').value==='segment')initGeneration();draw();
  }
  function prepare(events){if(!events)return [];if(!preparedCache.has(events))preparedCache.set(events,R.prepareNotes(events.map(e=>({id:e.id,start:e.start_ms,lane:e.lane,end:e.end_ms??null}))));return preparedCache.get(events);}
  async function getRevision(id){const key=s.project.id+':'+id;if(!cache.has(key)){const p=s.project.id;const r=await api('/projects/'+p+'/revisions/'+id);cache.set(key,r);}return cache.get(key);}
  async function loadSelected(id,at=position()){
    continuous=null;++continuousEpoch;
    window.ChartArcade?.close();pauseAudio();++audioEpoch;++spectrumEpoch;pendingSource=null;activeTrial=null;
    s.assembly=null;$('aw-leave-assembly').hidden=true;$('aw-timeline-context').textContent='分析建议和实际片段分别显示';
    const context=store.context(),token=resultAudio.begin({...context,revisionId:id||null,slot:'a'});
    s.preview=s.compare=null;s.slot='a';s.parallel=false;renderComparison();draw();
    const data=id?await getRevision(id):null;
    if(!store.matches(context)||!resultAudio.commit(token,data))return;
    s.preview=data;await setSource(resultAudio.resolve(s.stems).source,at,false);
    if(!store.matches(context)||!resultAudio.matches(token))return;
    renderVersions();await prepareContinuous(data);draw();
  }
  $('adv-variant').onchange=()=>run(async()=>{
    if(s.submittedBatch)s.submittedBatch.autoAllowed=false;s.variant=$('adv-variant').value;
    historyOpen=false;$('aw-result-history-list').hidden=true;
    if(s.assembly){await loadAssembly(s.assembly.id);return;}
    const row=s.resultBatch?.results.find(r=>r.segment_id===s.segmentId&&r.variant===s.variant&&r.primary&&r.adoptable);
    await loadSelected(row?.revision_id||segment()?.active?.[s.variant]);renderSegments();
  });
  async function acceptProject(project,select=true){if(project.id!==s.project?.id||project.revision<s.project.revision)return;s.project=project;if(!segment())s.segmentId=project.segments[0]?.id||null;if(!project.variants.some(v=>v.key===s.variant))s.variant=project.variants[0]?.key||null;s.selected=new Set([...s.selected].filter(id=>project.segments.some(x=>x.id===id)));renderProject();if(select)await loadSelected(segment()?.active?.[s.variant]);await syncWhole();}
  function candidateCard(v,adopted=false){
    const part=segment(),row=node('article',null,'aw-candidate'+(adopted?' is-adopted':''));row.dataset.revision=v.id;
    const head=node('header');head.append(node('strong',adopted?'正在使用':historicalCandidateIds.has(v.id)?'历史方案':'新方案'),node('span',(s.project.workflow_candidate_ids||[]).includes(v.id)?'工作流修复':(s.project.density_candidate_ids||[]).includes(v.id)?'密度补生成':kinds[v.kind]||'生成方案','aw-status'));
    if(selectedData()?.revision?.id===v.id){head.append(node('span','正在查看','aw-result-state'));row.classList.add('is-viewing');}row.append(head);
    const buttons=node('div',null,'aw-candidate-actions');buttons.append(action('试听 / 试玩',async()=>{await loadSelected(v.id);notice('正在预览这版；使用底部试听或试玩按钮。');}));
    if(!adopted){
      if(part.active?.[s.variant])buttons.append(action('与当前比较',()=>compareWith(v.id)));
      buttons.append(action('使用这一版',async()=>{await acceptProject(await api(base()+'/segments/'+part.id+'/select','POST',{variant:s.variant,revision_id:v.id,expected_revision:s.project.revision}));notice('已采用这版，导出将使用它。');},'aw-primary'));
    }
    row.append(buttons);
    row.append(node('p',window.MalodyDensity?.label(v.density_validation)||'密度未评估','aw-density-summary'));
    if(v.kind!=='stem_raw'&&v.kind!=='quality')buttons.append(action('修订音符',async()=>{
      const out=await api(base()+'/quality-candidates','POST',{revision_ids:[v.id],expected_revision:s.project.revision,request_id:crypto.randomUUID()});
      localCandidateIds.add(out.revisions[0].id);
      await acceptProject(await api(base()),false);await compareWith(out.revisions[0].id);
    }));
    const detail=node('details');detail.append(node('summary','统计与生成来源'),node('p',(v.engine==='mug'?'MuG':'V32')+' · '+v.stats.notes+' 音符 · '+Number(v.stats.segment_nps??v.stats.average_nps??0).toFixed(1)+'/秒'),node('p','峰值 '+v.stats.peak_nps+'/秒'+(v.density_validation?' · 上限 '+v.density_validation.peak_cap:'')+' · '+new Date(v.created).toLocaleString('zh-CN')));
    if(v.source_role==='vocals'||v.source_role==='accompaniment')detail.append(node('p',v.source_role==='vocals'?'来源：人声原谱':'来源：伴奏原谱'));
    const fusionText=W.fusionSummaryText(v.fusion_summary);if(fusionText)detail.append(node('p',fusionText));
    detail.append(node('p','版本 '+v.id.slice(0,8)));row.append(detail);return row;
  }
  function renderVersions(){
    const host=$('adv-versions'),raw=$('aw-raw-versions'),part=segment();host.replaceChildren();raw.replaceChildren();renderBatchResults();
    if(!part){host.append(node('p','先划出音乐片段，再生成和选择谱面。','aw-hint'));return;}
    if(part.start_sample>=W.contentEnd(s.project)){host.append(node('p','静音尾段已排除，无需生成；原范围和失败记录仍保留。','aw-hint'));return;}
    const recovery=W.recoveryFor(part,s.variant,s.tasks);if(recovery.kind==='retry')host.append(action('重试失败任务',()=>retryTask(recovery.task),'aw-primary'));
    if(!part.active?.[s.variant]&&recovery.kind==='choose')localCandidateIds.add(recovery.version.id);
    const versions=part.versions[s.variant]||[],active=part.active?.[s.variant],adopted=versions.find(v=>v.id===active);
    if(adopted)host.append(candidateCard(adopted,true));
    const batchIds=new Set((s.resultBatch?.results||[]).filter(r=>r.segment_id===part.id&&r.variant===s.variant&&r.primary&&r.current_range).map(r=>r.revision_id));
    const qualityIds=new Set([...(s.project.quality_candidate_ids||[]),...(s.project.density_candidate_ids||[])]);
    const next=versions.filter(v=>v.id!==active&&(batchIds.has(v.id)||localCandidateIds.has(v.id)||historicalCandidateIds.has(v.id)||qualityIds.has(v.id))&&v.range?.[0]===part.start_sample&&v.range?.[1]===part.end_sample).reverse();
    for(const v of next)host.append(candidateCard(v));
    if(!next.length&&!adopted)host.append(node('p',s.resultBatch?'本次此组合没有可用的新方案，请查看结果与失败原因。':'重做这一区域可得到新方案。','aw-hint'));
    for(const row of s.resultBatch?.results||[])if(row.segment_id===part.id&&row.variant===s.variant&&!row.primary){const v=versions.find(v=>v.id===row.revision_id);if(v)raw.append(candidateCard(v,v.id===active));}
    renderQuality();renderComparison();
  }
  async function compareWith(id){
    const part=segment(),current=part.active?.[s.variant];if(!current||id===current)throw new Error('需要两个不同版本才能比较');
    const at=position();window.ChartArcade?.close();pauseAudio();++audioEpoch;pendingSource=null;
    const context=store.context(),[a,b]=await Promise.all([getRevision(current),getRevision(id)]);if(!store.matches(context))return;
    if(JSON.stringify(a.range)!==JSON.stringify(b.range))throw new Error('比较须使用相同片段范围');
    s.preview=a;s.compare=b;s.diff=b.revision?.provenance?.diff||W.comparisonDiff(a.events,b.events);s.slot='a';s.assembly=null;
    resultAudio.select(a,{...context,slot:'a'});await setSource(resultAudio.resolve(s.stems).source,at,false);
    if(!store.matches(context))return;renderComparison();draw();
  }
  async function selectSlot(slot){const at=position();pauseAudio();s.slot=slot;const context=store.context(),token=resultAudio.select(selectedData(),{...context,slot});await setSource(resultAudio.resolve(s.stems).source,at,false);if(!store.matches(context)||!resultAudio.matches(token))return;renderComparison();renderQuality();draw();}
  function renderComparison(){$('aw-comparison-bar').hidden=!s.compare;$('aw-evaluation').hidden=!s.compare;$('aw-slot-a').setAttribute('aria-pressed',String(s.slot==='a'));$('aw-slot-b').setAttribute('aria-pressed',String(s.slot==='b'));$('aw-parallel').checked=s.parallel;$('adv-chart-after').hidden=!(s.compare&&s.parallel);$('adv-game-host').classList.toggle('aw-parallel',!!(s.compare&&s.parallel));}
  for(const slot of ['a','b'])$('aw-slot-'+slot).onclick=()=>run(()=>selectSlot(slot));
  $('aw-parallel').onchange=()=>{s.parallel=$('aw-parallel').checked;renderComparison();draw();};
  $('aw-end-compare').onclick=()=>run(async()=>{s.compare=null;await selectSlot('a');await prepareContinuous(s.preview);});
  bind('aw-adopt-comparison',async()=>{const part=segment(),revision=s.compare?.revision;if(!part||!revision)throw new Error('请选择要采用的新方案');await acceptProject(await api(base()+'/segments/'+part.id+'/select','POST',{variant:s.variant,revision_id:revision.id,expected_revision:s.project.revision}));notice('已采用新版。');});
  bind('aw-keep-comparison',async()=>{const current=segment()?.active?.[s.variant];if(!current)throw new Error('当前区域还没有采用版');await loadSelected(current);notice('已保留原版。');});
  function renderQuality(){const data=selectedData(),host=$('adv-quality');host.replaceChildren();if(!data)return;if(s.compare){host.append(node('p',(s.diff?.length||0)+' 处差异 · ＋ 新增 / − 删除 / ↔ 调整','aw-hint'));for(const change of s.diff||[]){const e=change.after||change.before;host.append(action(({add:'新增',delete:'删除',move:'移时',lane:'换轨',hold:'长条调整'}[change.type]||'修改')+' · '+T.time(e.start_ms/1000),()=>seek(e.start_ms/1000)));}}for(const warning of data.alerts||[])host.append(action(warning.message,()=>seek(warning.start_ms/1000)));if(!data.alerts?.length)host.append(node('p','当前检查未发现问题，仍建议试听和试玩。','aw-hint'));const v=segment()?.versions[s.variant]?.find(x=>x.id===data.revision?.id);$('aw-review-rating').value=v?.review?.status||'unreviewed';$('aw-review-reason').value=v?.review?.reason||'';}
  bind('aw-save-review',async()=>{const r=selectedData()?.revision;if(!r)throw new Error('请先预览一个谱面版本');await acceptProject(await api(base()+'/revisions/'+r.id+'/feedback','POST',{status:$('aw-review-rating').value,reason:$('aw-review-reason').value}),false);notice('已保存这版的评审。');});
  bind('aw-add-example',async()=>{const r=selectedData()?.revision;if(!r)throw new Error('请先预览已认可版本');await acceptProject(await api(base()+'/examples','POST',{revision_id:r.id}),false);notice('已加入认可示例库。');});
  bind('aw-use-selected',async()=>{
    const pid=s.project.id,bid=s.resultBatch.id;
    const fresh=await api(base()+'/generation-batches/'+bid);if(pid!==s.project?.id||bid!==s.resultBatch?.id)return;
    s.resultBatch=fresh;
    const choices=W.batchChoices(s.project,fresh,s.adoption);renderVersions();if(!choices.length)throw new Error('所选结果已变更或正在重试，请重新选择本批可采用的成功结果');
    const p=await api(base()+'/select-many','POST',{choices,expected_revision:s.project.revision});if(pid!==s.project?.id)return;
    await acceptProject(p,false);if(s.resultBatch?.id===bid)renderVersions();
    notice('已采用 '+choices.length+' 个确切版本。点击「导出」查看成品。');
  });
  for(const b of view.querySelectorAll('[data-preference]'))b.onclick=()=>run(async()=>{await api(base()+'/evaluations','POST',{before:s.preview.revision.id,after:s.compare.revision.id,preference:b.dataset.preference});notice('已记录这组比较。');});
  function renderRange(){$('adv-start').value=T.time(range[0]);$('adv-end').value=T.time(range[1]);$('aw-segment-name').value=segment()?.name||'';drawTimeline();}
  function inputRange(){return T.parseRange($('adv-start').value,$('adv-end').value,range,s.project.duration);}
  function setRange(a,b){range=[Math.max(0,a),Math.min(s.project.duration,b)];$('adv-start').value=T.time(range[0]);$('adv-end').value=T.time(range[1]);drawTimeline();}
  $('adv-start').onchange=$('adv-end').onchange=()=>run(async()=>{range=inputRange();drawTimeline();});
  $('adv-start-now').onclick=()=>setRange(position(),range[1]);$('adv-end-now').onclick=()=>setRange(range[0],position());
  bind('adv-add',async()=>{const [a,b]=inputRange();await acceptProject(await api(base()+'/segments','POST',{start_sample:Math.round(a*SR),end_sample:Math.round(b*SR),name:$('aw-segment-name').value,expected_revision:s.project.revision}));s.segmentId=s.project.segments.find(x=>x.start_sample===Math.round(a*SR))?.id;await chooseSegment(s.segmentId);s.selected.add(s.segmentId);renderSegments();notice('已保存实际片段。');});
  bind('aw-update-segment',async()=>{const part=segment();if(!part)throw new Error('先选择一个实际片段');const [a,b]=inputRange();await acceptProject(await api(base()+'/segments/'+part.id,'PATCH',{start_sample:Math.round(a*SR),end_sample:Math.round(b*SR),name:$('aw-segment-name').value,expected_revision:s.project.revision}));notice('片段已更新，旧版本仍保留在历史中。');});
  bind('aw-split',async()=>{if(!segment())throw new Error('先选择一个实际片段');await acceptProject(await api(base()+'/segments/'+s.segmentId+'/split','POST',{cuts:[Math.round(position()*SR)],expected_revision:s.project.revision}));notice('已在播放位置拆分。');});
  $('adv-next').onclick=()=>{if(!s.project)return;const end=range[1];setRange(end,Math.min(s.project.duration,end+16));$('aw-segment-name').value='';};
  bind('aw-undo',async()=>{await acceptProject(await api(base()+'/undo','POST',{expected_revision:s.project.revision}));s.draft=null;renderDraft();notice('已撤销整次划段操作。');});
  let cutsEdited=false,schemeTimer=0,schemeEpoch=0,schemePending=false,schemePromise=null,confirmedRequest=null;
  const regionChoices=new Map();
  function sourceIntentOptions(){const options={source_id:inputSource.startsWith('dual:')?'vocals_accompaniment':inputSource};if(inputSource.startsWith('dual:'))options.stem_set_id=inputSource.slice(5);else if(inputSource!=='original')options.stem_set_id=s.stems.find(set=>set.stems.some(row=>row.source_id===inputSource))?.id;return options;}
  function sourceOptions(){const intent=sourceIntentOptions();return s.musicAnalysis?.source===JSON.stringify(intent)&&s.musicAnalysis.resolved_source?{...s.musicAnalysis.resolved_source}:intent;}
  function currentSchemeIntent(){return W.schemeIntent(s.project,generationDraft,inputSource,checked('aw-patterns'),checked('aw-difficulties'),$('aw-profile').value);}
  function canReuseMusicAnalysis(){return s.musicAnalysis?.source===JSON.stringify(sourceIntentOptions())&&['ready','ambiguous'].includes(s.musicAnalysis?.status);}
  function scheduleScheme(){if(!s.project)return;schemePending=true;confirmedRequest=null;s.draft=null;++schemeEpoch;renderDraft();clearTimeout(schemeTimer);if(canReuseMusicAnalysis()){$('aw-analysis-status').textContent='正在更新方案…';schemeTimer=setTimeout(()=>run(refreshScheme),350);}else $('aw-analysis-status').textContent=analysisBusy?'正在处理音乐…':'待处理';}
  async function refreshScheme(){
    if(schemePromise)return schemePromise;
    schemePromise=(async()=>{while(schemePending&&s.project){schemePending=false;const analysis=s.musicAnalysis;
      if(!canReuseMusicAnalysis()){syncPrimaryAction();break;}
      await previewDraft();}})();try{await schemePromise;}finally{schemePromise=null;}
  }
  async function previewDraft(){
    if(!s.project||!canReuseMusicAnalysis())return;
    const context={pid:s.project.id,epoch:++draftEpoch,scheme:schemeEpoch,intent:currentSchemeIntent()},body={...sourceOptions(),settings:structuredClone(generationDraft),patterns:checked('aw-patterns'),difficulties:checked('aw-difficulties'),profile:$('aw-profile').value,evidence_id:s.musicAnalysis.evidence_id,timing_map_id:s.musicAnalysis.timing_map_id,expected_revision:s.project.revision};
    if(!body.patterns.length||!body.difficulties.length){$('aw-analysis-status').textContent='请选择至少一种排键和一个难度';return;}
    if(cuts.length||cutsEdited)body.cuts=cuts;if(draftIncluded)body.included=draftIncluded;
    if(regionChoices.size){const parents=s.project.segments.length?s.project.segments:[{start_sample:0,end_sample:W.contentEnd(s.project)}],regions=[];for(const parent of parents){const bounds=[parent.start_sample,...cuts.filter(c=>parent.start_sample<c&&c<parent.end_sample),parent.end_sample];for(let i=0;i<bounds.length-1;i++){const key=bounds[i]+':'+bounds[i+1];regions.push(regionChoices.has(key)?{intensity:regionChoices.get(key)}:{});}}body.regions=regions;}
    const draft=await api(base()+'/segmentation-preview','POST',body);
    if(context.pid!==s.project?.id||context.epoch!==draftEpoch||context.scheme!==schemeEpoch||context.intent!==currentSchemeIntent())return;
    s.draft=draft;s.draftIntent=context.intent;confirmedRequest=null;
    cuts=draft.segments.slice(0,-1).map(row=>row.end_sample);
    $('aw-analysis-status').textContent='区域方案已准备，请试听确认。';renderDraft();drawTimeline();
  }
  async function startMusicAnalysis(){
    if(!defaults?.capabilities?.music_workflow){$('aw-analysis-status').textContent='当前服务尚未加载音乐方案功能，请使用启动器启动更新后的服务。';$('aw-analyze').hidden=true;return;}
    if(!s.project||analysisBusy)return;const metadata=captureMetadata();await saveMetadata(metadata);assertMetadataContext(metadata);if(analysisBusy)return;const pid=s.project.id,source=JSON.stringify(sourceIntentOptions());analysisBusy=true;s.draft=null;s.musicAnalysis=null;renderDraft();$('aw-analysis-status').textContent='正在准备音乐与节奏方案…';
    try{const task=await api(base()+'/music-analysis','POST',{expected_revision:s.project.revision,...sourceIntentOptions()});if(pid!==s.project?.id)return;s.musicAnalysis={...task,source};if(['ready','ambiguous'].includes(task.status)){const result=await api(base()+'/music-analysis/'+(task.analysis_id||task.id));if(pid!==s.project?.id)return;s.musicAnalysis={...task,...result,source};analysisBusy=false;updateTimingAnchors();if(canReuseMusicAnalysis())await previewDraft();else{$('aw-analysis-status').textContent='音乐输入已改变，待处理';syncPrimaryAction();}}else await pollMusicAnalysis();}
    catch(error){if(pid===s.project?.id){analysisBusy=false;syncPrimaryAction();$('aw-analysis-status').textContent='分析失败：'+error.message;}throw error;}
  }
  async function pollMusicAnalysis(){
    const analysis=s.musicAnalysis;if(!analysis||!['queued','running','pending'].includes(analysis.status))return;const pid=s.project.id;
    const data=await api(base()+'/music-analysis/'+(analysis.analysis_id||analysis.id));if(pid!==s.project?.id||analysis!==s.musicAnalysis)return;
    s.musicAnalysis={...analysis,...data,source:analysis.source};
    if(['ready','ambiguous'].includes(data.status)){analysisBusy=false;updateTimingAnchors();if(!canReuseMusicAnalysis()){$('aw-analysis-status').textContent='音乐输入已改变，待处理';syncPrimaryAction();return;}schemePending=false;await previewDraft();}
    else if(['failed','interrupted','cancelled','paused'].includes(data.status)){analysisBusy=false;syncPrimaryAction();$('aw-analysis-status').textContent='分析未完成：'+(data.error||'分析已中断');}
    else $('aw-analysis-status').textContent=data.message||'正在分析音乐节奏…';
  }
  bind('aw-refresh-draft',()=>{scheduleScheme();return refreshScheme();});
  bind('aw-analyze',startMusicAnalysis);
  function changeRegionPolicy(){cuts=[];cutsEdited=false;draftIncluded=null;regionChoices.clear();scheduleScheme();}
  $('aw-region-granularity').onchange=()=>{generationDraft.region_granularity=$('aw-region-granularity').value;changeRegionPolicy();};
  function renderDraft(){
    syncPrimaryAction();const draft=s.draft;$('aw-draft-tools').hidden=!draft;
    $('aw-apply-segmentation').textContent='确认方案并生成';$('aw-apply-segmentation').disabled=!draft||draft.project_revision!==s.project?.revision||s.draftIntent!==currentSchemeIntent();
    if(!draft){$('aw-boundaries').replaceChildren();drawTimeline();return;}
    $('aw-draft-summary').textContent=draft.segments.filter(row=>row.included).length+' 个音乐区域 · '+checked('aw-patterns').length*checked('aw-difficulties').length+' 个目标组合';
    const host=$('aw-draft-rows');host.replaceChildren();
    for(let i=0;i<draft.segments.length;i++){const row=draft.segments[i],region=draft.regions?.find(r=>r.start_sample===row.start_sample&&r.end_sample===row.end_sample)||draft.regions?.find(r=>r.start_sample<row.end_sample&&r.end_sample>row.start_sample)||{},tempo=F.regionTempo(row,draft),card=node('article',null,'aw-scheme-region'),label=node('label',null,'aw-check'),check=node('input');check.type='checkbox';check.checked=row.included;
      check.onchange=()=>{draftIncluded=draft.segments.map((r,index)=>index===i?check.checked:r.included);scheduleScheme();};
      const intensity=node('select',null,'aw-intensity');intensity.setAttribute('aria-label','区域 '+(i+1)+' 强弱');intensity.append(...['calm','regular','dense'].map(value=>option(W.intensityLabel(value),value)));intensity.value=region.intensity||'regular';intensity.onchange=()=>{regionChoices.set(row.start_sample+':'+row.end_sample,intensity.value);scheduleScheme();};
      const heading=node('span',null,'aw-region-heading');heading.append(node('strong',T.time(row.start_sample/SR)+'–'+T.time(row.end_sample/SR)));
      if(tempo){const bpm=node('span',Math.round(tempo.bpm)+' BPM','aw-region-bpm');bpm.title=F.regionTempoTitle(tempo);heading.append(bpm);if(tempo.multi)heading.append(node('span','多速度段','aw-region-multi'));}
      label.append(check,heading);card.append(label,intensity);if(i<draft.segments.length-1&&!draft.preserved_boundaries.includes(row.end_sample))card.append(action('合并下一区域',()=>{cuts=cuts.filter(c=>c!==row.end_sample);cutsEdited=true;draftIncluded=null;scheduleScheme();},'aw-region-merge'));host.append(card);
    }
    renderBoundaryHandles();
  }
  bind('aw-apply-segmentation',async()=>{
    const metadata=captureMetadata();
    if(!s.draft||s.draftIntent!==currentSchemeIntent()||s.draft.project_revision!==s.project.revision)throw new Error('音乐或目标已变化，请等待更新后的方案再确认');
    const changed=await saveMetadata(metadata);assertMetadataContext(metadata);
    if(changed){confirmedRequest=null;await previewDraft();assertMetadataContext(metadata);}
    if(!s.draft||s.draftIntent!==currentSchemeIntent()||s.draft.project_revision!==s.project.revision)throw new Error('音乐或目标已变化，请等待更新后的方案再确认');
    const pid=s.project.id,draft=s.draft;confirmedRequest=confirmedRequest||{request_id:crypto.randomUUID().replaceAll('-',''),expected_revision:s.project.revision,draft_id:draft.id,arrangement_plan_id:draft.arrangement_plan_id};
    const response=await api(base()+'/confirm-and-generate','POST',confirmedRequest);if(pid!==s.project?.id)return;
    await acceptProject(response.project,false);s.draft=null;confirmedRequest=null;renderDraft();changeMode('generate');
    s.submittedBatch={id:response.batch.id||response.batch.batch_id,projectId:pid,segmentId:s.segmentId,variant:s.variant,autoAllowed:visible,delivered:false,submitting:false};s.resultBatch=null;s.adoption.clear();renderDelivery();await pollTasks();
  });
  $('aw-discard-draft').textContent='放弃此方案';$('aw-discard-draft').onclick=()=>{s.draft=null;cuts=[];draftIncluded=null;renderDraft();$('aw-analyze').hidden=false;};
  function renderAnalysis(){const host=$('aw-analysis-details');host.replaceChildren();if(!s.plan)return;host.append(node('p',F.rhythmPlanSummary(s.plan,s.project.tempo),'aw-hint'));for(const part of s.plan.sections){const info=F.rhythmSectionPresentation(part,s.variant,s.project.tempo),detail=node('details');detail.append(node('summary',T.time(info.startSeconds)+'–'+T.time(info.endSeconds)+' · '+info.activityLabel),node('p',info.candidateText),node('p',info.conditionText));if(info.ambiguityText)detail.append(node('p',info.ambiguityText));host.append(detail);}}
  function renderTempo(){const host=$('aw-tempo-points');host.replaceChildren();if(!s.project)return;for(const [index,p]of T.tempoPoints(s.project.tempo).entries())host.append(action(T.time(p[0]/1000)+' · '+p[1]+' BPM'+(s.project.tempo.uncertain?' 待确认':''),()=>editTempo(index)));}
  async function editTempo(index){const host=$('aw-tempo-points'),points=structuredClone(T.tempoPoints(s.project.tempo)),point=index==null?[position()*1000,s.project.tempo.bpm]:points[index],row=node('div',null,'aw-tempo-edit'),time=node('input'),bpm=node('input');time.value=T.time(point[0]/1000);time.setAttribute('aria-label','BPM 时间');bpm.type='number';bpm.min=20;bpm.max=600;bpm.step='.001';bpm.value=point[1];bpm.setAttribute('aria-label','BPM 值');row.append(time,bpm,action('保存参考',async()=>{const updated=[T.parseTime(time.value)*1000,Number(bpm.value)];if(index==null)points.push(updated);else points[index]=updated;points.sort((a,b)=>a[0]-b[0]);await acceptProject(await api(base(),'PATCH',{tempo:T.tempoEdit(s.project.tempo,points,index),expected_revision:s.project.revision}),false);notice('已更新节拍参考，已有音符没有移动。');}),action('取消',renderTempo));host.replaceChildren(row);}
  $('aw-add-tempo').onclick=()=>run(()=>editTempo(null));
  bind('aw-save-metadata',async()=>{const metadata=captureMetadata(true),changed=await saveMetadata(metadata);if(changed&&s.draft)await previewDraft();assertMetadataContext(metadata);await listProjects();notice('歌曲信息已保存，后续导出使用新信息。');});
  $('aw-keep-tail').onchange=()=>run(async()=>{try{await acceptProject(await api(base(),'PATCH',{tail_trim_enabled:!$('aw-keep-tail').checked,expected_revision:s.project.revision}),false);renderExport();}catch(e){$('aw-keep-tail').checked=s.project.tail_trim.enabled===false;throw e;}});
  function windowRange(){const duration=s.project?.duration||1,zoom=Number($('adv-zoom').value),span=duration/zoom,start=Number($('adv-pan').value)*(duration-span);return [start,start+span];}
  function canvasSize(canvas){const dpr=window.devicePixelRatio||1,w=canvas.clientWidth,h=canvas.clientHeight;if(!w||!h)return null;const pw=Math.round(w*dpr),ph=Math.round(h*dpr);if(canvas.width!==pw||canvas.height!==ph){canvas.width=pw;canvas.height=ph;}const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);return {ctx,w,h};}
  function drawTimeline(){const z=canvasSize($('adv-wave'));if(!z)return;const {ctx,w,h}=z,[lo,hi]=windowRange(),x=t=>(t-lo)/(hi-lo)*w,p=s.project,theme=getComputedStyle(view),accent=theme.getPropertyValue('--theme-accent').trim()||'#59b7c7',muted=theme.getPropertyValue('--theme-muted').trim()||'#6c8b9e';ctx.clearRect(0,0,w,h);if(!p)return;const rows=s.draft?.segments||p.segments;for(const [i,row]of rows.entries()){const a=x(row.start_sample/SR),b=x(row.end_sample/SR);ctx.fillStyle=row.included?(i%2?accent+'38':accent+'22'):'#73809320';ctx.fillRect(a,0,b-a,23);ctx.fillStyle=theme.getPropertyValue('--theme-text')||'#244c61';ctx.font='13px Microsoft YaHei UI';if(b-a>65)ctx.fillText((s.draft?'建议 ':'')+(i+1)+(s.draft?.regions?.[i]?.intensity?' · '+W.intensityLabel(s.draft.regions[i].intensity):''),a+7,16);ctx.fillStyle=accent;ctx.fillRect(a,0,1,23);}for(const part of s.plan?.sections||[]){const a=x(part.core[0]/SR),b=x(part.core[1]/SR),activity=part.rhythm_activity||0;ctx.fillStyle=activity>.25?'#d687ac88':activity<-.25?accent+'44':accent+'77';ctx.fillRect(a,h-28,b-a-1,10);}const top=(rows.length?24:0)+20,bottom=18+(s.plan?.sections?.length?12:0),a=x(range[0]),b=x(range[1]);ctx.fillStyle=accent+'14';ctx.fillRect(a,top,b-a,h-top-bottom);ctx.strokeStyle=accent;ctx.strokeRect(a+.5,top+.5,Math.max(0,b-a-1),h-top-bottom-1);ctx.fillStyle=accent;const peaks=p.waveform||[],mid=(top+h-bottom)/2,amp=Math.max(1,(h-top-bottom)/2)*.9,gain=1/Math.max(...peaks,1e-12);for(let px=0;px<w;px+=2){const t=lo+px/w*(hi-lo),v=peaks[Math.min(peaks.length-1,Math.floor(t/p.duration*peaks.length))]||0;ctx.fillRect(px,mid-v*gain*amp,1.3,Math.max(1,v*gain*amp*2));}ctx.fillStyle=muted;ctx.font='13px Bahnschrift,Microsoft YaHei UI';const step=Math.max(1,Math.ceil((hi-lo)/(w/100)/5)*5);for(let t=Math.ceil(lo/step)*step;t<=hi;t+=step)ctx.fillText(T.time(t).slice(0,5),x(t)+3,h-3);const displayedTempo=T.displayTempo(p.tempo,s.musicAnalysis?.timing_map),tempo=displayedTempo.points;if(!tempo.length)ctx.fillText(displayedTempo.label||'',4,(rows.length?24:0)+15);for(const marker of T.clusters(tempo,lo,hi,w)){const index=marker.indices[0],[ms,bpm]=tempo[index],uncertain=displayedTempo.points_metadata?.[index]?.confirmed===false||(!displayedTempo.points_metadata?.[index]&&displayedTempo.uncertain);ctx.fillStyle='#ce8fa8';ctx.fillRect(marker.x,rows.length?24:0,1,top-(rows.length?24:0));ctx.fillStyle=muted;ctx.fillText(marker.indices.length>1?marker.indices.length+' 个变化':Math.round(bpm*100)/100+' BPM'+(uncertain?' ?':''),Math.min(marker.x+3,w-115),(rows.length?24:0)+15);}if(p.tail_trim?.enabled&&p.tail_trim.cutoff_sample<p.samples){const cut=x(p.tail_trim.cutoff_sample/SR);ctx.fillStyle='#73809325';ctx.fillRect(Math.max(0,cut),0,w-Math.max(0,cut),h);if(cut<w){ctx.fillStyle=muted;ctx.fillText('静音尾段 · 不纳入成品',Math.max(4,Math.min(cut+4,w-155)),h-18);}}ctx.fillStyle='#e3af59';ctx.fillRect(x(s.assembly?(window.SpectrumPane.playbackToSource(position()*SR,s.assembly.mapping)?.sourceSample??0)/SR:position()),0,2,h);renderBoundaryHandles();}
  function renderBoundaryHandles(){if(drag?.type==='cut')return;const signature=JSON.stringify([s.draft?.id,s.mode,cuts,windowRange()]);if(signature===boundarySignature)return;boundarySignature=signature;const host=$('aw-boundaries');host.replaceChildren();if(!s.draft||!['prepare','segment'].includes(s.mode))return;const [lo,hi]=windowRange();for(let i=0;i<cuts.length;i++){const cut=cuts[i],time=cut/SR;if(time<=lo||time>=hi||s.draft.preserved_boundaries.includes(cut))continue;const button=node('button',null,'aw-boundary');button.style.left=(time-lo)/(hi-lo)*100+'%';button.setAttribute('aria-label','移动建议边界 '+T.time(time));button.title=T.time(time)+' · 拖动调整';button.onpointerdown=e=>{e.preventDefault();e.stopPropagation();button.setPointerCapture(e.pointerId);drag={type:'cut',index:i,element:button};};button.onpointermove=e=>{if(!drag||drag.element!==button)return;const rect=$('adv-wave').getBoundingClientRect();const value=Math.round((lo+F.scrubPosition(e.clientX,rect.left,rect.width,hi-lo))*SR),previous=i?cuts[i-1]:0,next=cuts[i+1]??s.project.samples;cuts[i]=Math.max(previous+11025,Math.min(next-11025,value));button.style.left=(cuts[i]/SR-lo)/(hi-lo)*100+'%';};button.onpointerup=()=>{drag=null;cutsEdited=true;draftIncluded=null;scheduleScheme();};button.onkeydown=e=>{if(e.code!=='ArrowLeft'&&e.code!=='ArrowRight')return;e.preventDefault();cuts[i]+=Math.round((e.shiftKey?.01:.001)*SR)*(e.code==='ArrowLeft'?-1:1);cutsEdited=true;draftIncluded=null;scheduleScheme();};host.append(button);}}
  function seekSourceTime(time){seek(s.assembly?window.SpectrumPane.sourceToPlayback(time*SR,s.assembly.mapping)/SR:time);}
  const wave=$('adv-wave');wave.onpointerdown=e=>{if(!s.project||e.button!==0)return;e.preventDefault();const rect=wave.getBoundingClientRect(),[lo,hi]=windowRange(),t=lo+F.scrubPosition(e.clientX,rect.left,rect.width,hi-lo);if(!s.assembly&&!window.ChartArcade?.isActive()&&e.clientY-rect.top>=(s.draft?.segments?.length||s.project.segments.length?24:0)&&e.clientY-rect.top<=(s.draft?.segments?.length||s.project.segments.length?44:20)){const marker=T.clusters(T.tempoPoints(s.project.tempo),lo,hi,rect.width).find(m=>Math.abs(m.x-(e.clientX-rect.left))<65);if(marker&&(!s.musicAnalysis?.timing_map||s.project.tempo.manual&&!s.project.tempo.uncertain)){changeMode('segment');$('aw-tempo-points').closest('details').open=true;run(()=>editTempo(marker.indices[0]));return;}}seekSourceTime(t);drag={type:s.mode==='segment'&&!s.draft&&!s.assembly&&!window.ChartArcade?.isActive()?'range':'seek',start:t};wave.setPointerCapture(e.pointerId);};wave.onpointermove=e=>{if(!['range','seek'].includes(drag?.type))return;const rect=wave.getBoundingClientRect(),[lo,hi]=windowRange();let t=lo+F.scrubPosition(e.clientX,rect.left,rect.width,hi-lo);if(drag.type==='seek'){seekSourceTime(t);return;}if($('adv-snap').checked){const points=T.tempoPoints(s.project.tempo),anchor=points.filter(([ms])=>ms<=t*1000).at(-1)||points[0],beat=60/anchor[1];t=anchor[0]/1000+Math.round((t-anchor[0]/1000)/beat)*beat;}setRange(Math.min(t,drag.start),Math.max(t,drag.start));};wave.onpointerup=wave.onpointercancel=wave.onlostpointercapture=()=>{drag=null;};$('adv-zoom').oninput=$('adv-pan').oninput=drawTimeline;
  function inputOptions(){const items=[option('原曲','original'),option('人声＋伴奏 · 自动准备','vocals_accompaniment')];for(const set of s.stems){const name=T.stemName(set);items.push(option('人声＋伴奏 → 合并谱面 · '+name,'dual:'+set.id));for(const row of set.stems||[])items.push(option((row.role==='vocals'?'人声':'伴奏')+' · '+name,row.source_id));}return items;}
  function refreshSourceControls(){const signature=s.stems.map(x=>x.id).join('|')+':'+s.project?.id;if(signature===sourceSignature)return;sourceSignature=signature;for(const id of ['aw-input-source','aw-generation-source']){const el=$(id);el.replaceChildren(...inputOptions());el.value=[...el.options].some(o=>o.value===inputSource)?inputSource:'original';}inputSource=$('aw-input-source').value;$('adv-listen-source').replaceChildren(option('试听原曲','original'),...s.stems.flatMap(set=>(set.stems||[]).map(row=>option('试听'+(row.role==='vocals'?'人声':'伴奏')+' · '+T.stemName(set),row.source_id))));$('adv-listen-source').value=s.source;renderStems();}
  function chooseInput(value){const changed=inputSource!==value;inputSource=value;for(const id of ['aw-input-source','aw-generation-source'])$(id).value=value;if(s.project)localStorage.setItem('startrail.advanced-source.'+s.project.id,value);renderGenerationCount();if(changed)scheduleScheme();}
  $('aw-input-source').onchange=()=>chooseInput($('aw-input-source').value);$('aw-generation-source').onchange=()=>chooseInput($('aw-generation-source').value);
  async function modelCatalog(){const data=await api('/separation/models');models=data.models;$('aw-separation-model').replaceChildren(...models.map(m=>option(m.label,m.id)));$('aw-separation-model').value=data.defaults.fast;renderSeparationParams();}
  function renderSeparationParams(){const model=models.find(m=>m.id===$('aw-separation-model').value),host=$('aw-separation-params');host.replaceChildren();if(!model)return;const installed=model.assets_installed??model.status?.assets_installed;$('aw-separation-availability').textContent=installed?(model.description||'本机模型可用'):(model.error||model.status?.error||'模型未安装，请先完成模型配置。');$('aw-separate').disabled=$('aw-trial').disabled=!installed;for(const field of model.parameters){if(field.read_only){host.append(node('p',field.label+'：'+field.default,'aw-hint'));continue;}const label=node('label',field.label,'aw-field'),input=node(field.choices?'select':'input');input.dataset.separation=field.key;if(field.choices)input.append(...field.choices.map(v=>option(String(v),v)));else{input.type='number';input.min=field.min;input.max=field.max;input.step=field.step;}input.value=field.default;label.append(input);host.append(label);}}
  $('aw-separation-model').onchange=renderSeparationParams;
  function separationSettings(){const m=models.find(x=>x.id===$('aw-separation-model').value),settings={model:m.id};for(const field of m.parameters){if(field.read_only)continue;const input=view.querySelector('[data-separation="'+field.key+'"]'),error=window.SeparationAudition.parameterError(field,input.value);if(error)throw new Error(error);settings[field.key]=Number(input.value);}return settings;}
  bind('aw-separate',async()=>{const task=await api(base()+'/separations','POST',{settings:separationSettings(),expected_revision:s.project.revision});notice('分离已加入队列，任务 '+task.id.slice(0,6));await pollTasks();});
  bind('aw-trial',async()=>{const [a,b]=inputRange();await api(base()+'/separation-trials','POST',{settings:separationSettings(),start_sample:Math.round(a*SR),end_sample:Math.round(b*SR),expected_revision:s.project.revision});notice('局部分离试听已加入队列。');await pollTasks();});
  function renderStems(){const host=$('aw-stem-versions');host.replaceChildren();for(const set of s.stems){const row=node('details',null,'aw-stem-version');row.append(node('summary',T.stemName(set)),node('p',set.summary||'完整原曲 · 人声与伴奏已对齐','aw-hint'));const buttons=node('div',null,'aw-inline-actions');for(const stem of set.stems||[])buttons.append(action(stem.role==='vocals'?'听人声':'听伴奏',()=>{resultAudio.override(stem.source_id);return setSource(stem.source_id,position(),!audio.paused);}));buttons.append(action('用于两路生成',()=>{chooseInput('dual:'+set.id);notice('已选制谱输入，试听声音未改变。');}));row.append(buttons);if(set.performance)row.append(node('p',(set.device_name||set.device||'GPU')+' · '+Number(set.performance.wall_seconds).toFixed(1)+' 秒','aw-hint'));host.append(row);}}
  function generationNumber(label,key,value,min,max,step=1,host=$('aw-generation-params')){const field=node('label',label,'aw-field'),input=node('input');input.type='number';input.min=min;input.max=max;input.step=step;input.value=value;input.dataset.setting=key;input.oninput=()=>{const path=key.split('.');let target=generationDraft;for(const part of path.slice(0,-1))target=target[part];target[path.at(-1)]=Number(input.value);if(key.startsWith('nps_ranges.')){const difficulty=path[1],bounds=generationDraft.nps_ranges[difficulty];generationDraft.difficulty_rules[difficulty].rate=Number(((bounds.min+bounds.max)/2).toFixed(1));}renderGenerationCount();if(key.startsWith('bpm_bucket_')||key==='dynamic_strength'||key.startsWith('conditions.')||key.startsWith('difficulty_rules.')||key.startsWith('nps_ranges.'))scheduleScheme();};field.append(input);host.append(field);}
  function updateStrategyControl(){const strategy=$('aw-strategy');if(!strategy)return;strategy.replaceChildren(option('每档独立生成','independent'));if(generationDraft.engine==='mug')strategy.append(option('母谱快速分层','fast'));if(generationDraft.engine==='v32'&&generationDraft.strategy==='fast')generationDraft.strategy='independent';if(generationDraft.engine==='mug'&&!['independent','fast'].includes(generationDraft.strategy))generationDraft.strategy='independent';strategy.value=generationDraft.strategy;strategy.disabled=generationDraft.engine==='v32';const hint=$('aw-strategy-hint');if(hint)hint.textContent=generationDraft.engine==='v32'?'V32 按每档 NPS 范围自动映射难度条件，并结合 BPM 桶独立生成。':'MuG 保留原有独立生成与母谱快速分层策略。';}
  function validateNpsRanges(){for(const key of defaults.difficulties){const range=generationDraft.nps_ranges?.[key],min=Number(range?.min),max=Number(range?.max);if(!Number.isFinite(min)||!Number.isFinite(max)||min<.5||max>50||min>max)throw new Error(key+' 的 NPS 范围需满足 0.5–50，且下限不高于上限。');}}
  function initGeneration(){
    if(!s.project)return;
    generationDraft=structuredClone($('aw-settings-scope').value==='segment'?mergeSettings(s.project.settings,segment()?.overrides||{}):s.project.settings);
    if(generationDraft.engine==='mug')mugStrategyPreference=generationDraft.strategy==='fast'?'fast':'independent';
    else if(generationDraft.strategy==='fast')mugStrategyPreference='fast';
    generationDraft.nps_ranges=W.initializeNpsRanges(generationDraft,defaults.difficulties);
    for(const key of defaults.difficulties)generationDraft.difficulty_rules[key].rate=Number(((generationDraft.nps_ranges[key].min+generationDraft.nps_ranges[key].max)/2).toFixed(1));
    if(generationDraft.engine==='v32'&&generationDraft.strategy==='fast')generationDraft.strategy='independent';
    generationDraft.region_granularity??='balanced';generationDraft.region_max_count??=24;generationDraft.bpm_bucket_count??=5;generationDraft.bpm_bucket_range??=.2;
    W.normalizeFusion(generationDraft);
    $('aw-region-granularity').value=generationDraft.region_granularity;
    $('aw-overrides-note').textContent=s.project.segments.filter(x=>Object.keys(x.overrides||{}).length).length+' 个片段有独立设置，生成时优先采用各片段设置。';
    if(s.project.profile&&! [...$('aw-profile').options].some(o=>o.value===s.project.profile))$('aw-profile').append(option('旧项目玩法',s.project.profile));
    $('aw-profile').value=s.project.profile;$('aw-engine').value=generationDraft.engine;
    for(const [id,values,selected]of [['aw-patterns',Object.keys(patterns),new Set(s.project.variants.map(v=>v.pattern))],['aw-difficulties',defaults.difficulties,new Set(s.project.variants.map(v=>v.difficulty))]]){const host=$(id);host.replaceChildren();for(const value of values){const label=node('label',null,'aw-option'),input=node('input');input.type='checkbox';input.value=value;input.checked=selected.has(value);input.onchange=()=>{renderGenerationCount();scheduleScheme();};label.append(input,node('span',patterns[value]||value));host.append(label);}}
    const host=$('aw-generation-params');host.replaceChildren();
    const regionLimit=node('input');regionLimit.id='aw-region-max-count';regionLimit.type='number';regionLimit.min=1;regionLimit.max=100;regionLimit.step=1;regionLimit.value=generationDraft.region_max_count;regionLimit.onchange=()=>{const value=Number(regionLimit.value);if(!Number.isInteger(value)||value<1||value>100){regionLimit.reportValidity();return;}generationDraft.region_max_count=value;changeRegionPolicy();};
    const regionLimitLabel=node('label','建议区域数量上限','aw-field');regionLimitLabel.append(regionLimit);host.append(regionLimitLabel);
    const strategy=node('select');strategy.id='aw-strategy';strategy.onchange=()=>{generationDraft.strategy=strategy.value;if(generationDraft.engine==='mug')mugStrategyPreference=strategy.value;renderGenerationCount();};
    const label=node('label','多难度策略','aw-field');label.append(strategy);host.append(label);const strategyHint=node('p',null,'aw-hint');strategyHint.id='aw-strategy-hint';host.append(strategyHint);updateStrategyControl();
    const granularity=node('select');granularity.append(option('细分 · 约 8 秒','fine'),option('均衡 · 约 16 秒','balanced'),option('粗分 · 约 32 秒','coarse'));granularity.value=generationDraft.section_granularity;granularity.onchange=()=>{generationDraft.section_granularity=granularity.value;renderGenerationCount();};
    const grainLabel=node('label','推理窗口粒度','aw-field');grainLabel.append(granularity);host.append(grainLabel);
    const seed=node('input');seed.type='checkbox';seed.checked=generationDraft.fixed_seed;seed.onchange=()=>generationDraft.fixed_seed=seed.checked;const seedLabel=node('label',null,'aw-check');seedLabel.append(seed,document.createTextNode('固定种子复现'));host.append(seedLabel);generationNumber('种子','seed',generationDraft.seed,0,2147483640);
    const steps=node('select');steps.append(...[20,50,100].map(n=>option(n+' 步',n)));steps.value=generationDraft.steps;steps.onchange=()=>generationDraft.steps=Number(steps.value);const stepsLabel=node('label','MuG 采样步数','aw-field');stepsLabel.append(steps);host.append(stepsLabel);
    generationNumber('长条比例','ln_ratio',generationDraft.ln_ratio,0,.8,.05);generationNumber('排键倾向','pattern_strength',generationDraft.pattern_strength,0,100);
    for(const [title,key,min,max,step]of [['V32 温度','v32_temperature',.1,2,.05],['Top-p','v32_top_p',.1,1,.05],['轨道随机性','v32_column_temperature',.1,2,.05],['CFG','v32_cfg_scale',.5,5,.1],['MuG 引导强度','mug_guidance',0,10,.1]])generationNumber(title,key,generationDraft[key],min,max,step);
    const dynamic=node('input');dynamic.type='checkbox';dynamic.checked=generationDraft.dynamic_enabled;dynamic.onchange=()=>{generationDraft.dynamic_enabled=dynamic.checked;scheduleScheme();};const dynamicLabel=node('label',null,'aw-check');dynamicLabel.append(dynamic,document.createTextNode('按 BPM 桶调整各段难度'));host.append(dynamicLabel);
    generationNumber('BPM 桶数量上限','bpm_bucket_count',generationDraft.bpm_bucket_count,1,9,1);generationNumber('最大密度浮动（0.2＝20%）','bpm_bucket_range',generationDraft.bpm_bucket_range,0,.25,.05);generationNumber('难度浮动强度','dynamic_strength',generationDraft.dynamic_strength??1,0,1,.1);
    for(const key of defaults.difficulties){
      const detail=node('details');detail.append(node('summary',key+' 难度参数'));host.append(detail);
      if(generationDraft.engine==='mug')generationNumber('MuG 模型条件','conditions.mug.'+key,generationDraft.conditions.mug[key],.1,10,.1,detail);
      for(const [bound,label]of [['min','NPS 下限'],['max','NPS 上限']])generationNumber(label,'nps_ranges.'+key+'.'+bound,generationDraft.nps_ranges[key][bound],.5,50,.1,detail);
      for(const [field,name,min,max]of [['peak','峰值参考',1,100],['gap','同轨间隔 ms',0,2000],['chord','同刻上限',1,4],['hold_ms','最长长条 ms',100,30000]])generationNumber(name,'difficulty_rules.'+key+'.'+field,generationDraft.difficulty_rules[key][field],min,max,1,detail);
    }
    renderGenerationCount();
  }
  const checked=id=>[...$(id).querySelectorAll('input:checked')].map(i=>i.value);
  function syncTargetSelectors(){
    for(const [id,group] of [['aw-target-difficulty','aw-difficulties'],['aw-target-pattern','aw-patterns']]){
      const select=$(id),choices=[...$(group).querySelectorAll('input')].map(input=>input.value),selected=checked(group);
      select.replaceChildren(...choices.map(value=>option(patterns[value]||value[0].toUpperCase()+value.slice(1),value)));
      if(selected.length!==1){select.append(option(selected.length?'已选择 '+selected.length+' 个组合':'选择目标','multiple'));select.value='multiple';}else select.value=selected[0];
    }
  }
  for(const [id,group] of [['aw-target-difficulty','aw-difficulties'],['aw-target-pattern','aw-patterns']])$(id).onchange=()=>{
    if($(id).value==='multiple')return;
    for(const input of $(group).querySelectorAll('input'))input.checked=input.value===$(id).value;
    renderGenerationCount();scheduleScheme();
  };
  function generationTargets(){return W.generationTargets(s.project,s.generationScope,s.segmentId,s.selected);}
  function renderGenerationSelection(){
    const host=$('aw-generation-selection');host.hidden=s.generationScope!=='custom';host.replaceChildren();
    if(host.hidden)return;for(const part of s.project?.segments||[]){const label=node('label',null,'aw-scope-choice'),check=node('input');check.type='checkbox';check.checked=s.selected.has(part.id);check.onchange=()=>{check.checked?s.selected.add(part.id):s.selected.delete(part.id);renderGenerationCount();};label.append(check,node('span',part.name+' · '+T.time(part.start_sample/SR)+'–'+T.time(part.end_sample/SR)));host.append(label);}
  }
  $('aw-generation-scope').onchange=()=>{s.generationScope=$('aw-generation-scope').value;renderGenerationSelection();renderGenerationCount();};
  function syncFusionControls(){
    const draft=generationDraft;$('aw-fusion-controls').hidden=!draft||!W.fusionUsesStems(inputSource);if(!draft)return;
    W.normalizeFusion(draft);$('aw-fusion-mode').value=draft.fusion_mode;$('aw-fusion-primary').value=draft.fusion_primary;
    $('aw-fusion-hint').textContent=W.fusionModeHint(draft.fusion_mode);$('aw-fusion-primary-field').hidden=!W.fusionPrimaryVisible(draft.fusion_mode);
  }
  $('aw-fusion-mode').onchange=()=>{if(!generationDraft)return;generationDraft.fusion_mode=$('aw-fusion-mode').value;syncFusionControls();};
  $('aw-fusion-primary').onchange=()=>{if(!generationDraft)return;generationDraft.fusion_primary=$('aw-fusion-primary').value;syncFusionControls();};
  function renderGenerationCount(){
    syncFusionControls();syncTargetSelectors();
    const targets=generationTargets(),count=targets.length,pats=checked('aw-patterns'),diff=checked('aw-difficulties'),roles=(inputSource.startsWith('dual:')||inputSource==='vocals_accompaniment')?2:1,combinations=pats.length*diff.length;
    let cores=0;for(const seg of s.project?.segments||[])if(targets.includes(seg.id)&&!s.project.silent_segment_ids?.includes(seg.id))cores+=s.plan?.sections?.filter(row=>row.core[0]<Math.min(seg.end_sample,W.contentEnd(s.project))&&row.core[1]>seg.start_sample).length||Math.max(1,Math.ceil((Math.min(seg.end_sample,W.contentEnd(s.project))-seg.start_sample)/SR/({fine:8,balanced:16,coarse:32}[generationDraft.section_granularity]||16)));
    $('aw-generation-count').textContent=count+' 个片段 × '+combinations+' 个组合';
    $('aw-generation-estimate').textContent=roles+' 路音乐 · 生成后可直接试玩。';
    $('aw-generate').textContent=s.generationScope==='all'?'生成全部 '+count+' 段':s.generationScope==='current'?'生成当前片段':'生成指定 '+count+' 段';
    $('aw-generate').disabled=!count||!combinations||busy.has('aw-generate');renderGenerationSelection();
  }
  function mergeSettings(a,b){const out=structuredClone(a);for(const [k,v]of Object.entries(b))out[k]=v&&typeof v==='object'&&!Array.isArray(v)?mergeSettings(out[k]||{},v):v;return out;}
  $('aw-settings-scope').onchange=initGeneration;bind('aw-clear-overrides',async()=>{if(!segment())throw new Error('先选择片段');await acceptProject(await api(base()+'/segments/'+s.segmentId,'PATCH',{overrides:{},expected_revision:s.project.revision}),false);$('aw-settings-scope').value='project';initGeneration();notice('当前片段已恢复继承项目模板。');});
  async function saveSettings(metadata=captureMetadata()){await saveMetadata(metadata);assertMetadataContext(metadata);const pid=s.project.id,sid=s.segmentId,scope=$('aw-settings-scope').value;generationDraft.engine=$('aw-engine').value;validateNpsRanges();const pats=checked('aw-patterns'),diff=checked('aw-difficulties');if(!pats.length||!diff.length)throw new Error('至少选择一种排键和一个难度');let p=await api(base(),'PATCH',{...($('aw-settings-scope').value==='project'?{settings:generationDraft}:{}),patterns:pats,difficulties:diff,profile:$('aw-profile').value,expected_revision:s.project.revision});if(pid!==s.project?.id||sid!==s.segmentId)throw new Error('项目或片段已切换，请重新确认设置后提交');await acceptProject(p,false);assertMetadataContext(metadata);if(scope==='segment'){if(!segment())throw new Error('先选中要单独设置的片段');p=await api(base()+'/segments/'+s.segmentId,'PATCH',{overrides:generationDraft,expected_revision:s.project.revision});if(pid!==s.project?.id||sid!==s.segmentId)throw new Error('项目或片段已切换，请重新确认设置后提交');await acceptProject(p,false);}return p;}
  bind('aw-save-settings',async()=>{await saveSettings();scheduleScheme();notice('设置已保存，没有提交生成。');});
  async function generate(currentOnly){
    const metadata=captureMetadata();
    if(busy.has('generation-submission'))return;
    busy.add('generation-submission');
    const previousSubmission=s.submittedBatch;
    const anchor={projectId:s.project?.id,segmentId:s.segmentId,variant:s.variant,autoAllowed:visible&&s.mode==='generate',delivered:false,submitting:true};
    if(previousSubmission)previousSubmission.autoAllowed=false;
    s.submittedBatch=anchor;
    try{
    if(!s.project)throw new Error('先打开音乐');generationDraft.engine=$('aw-engine').value;validateNpsRanges();const epoch=projectEpoch,pid=s.project.id,targetIds=currentOnly?[s.segmentId]:generationTargets();
    if(!targetIds.length||targetIds.some(id=>!id))throw new Error('请选择要生成的片段');
    await saveMetadata(metadata);assertMetadataContext(metadata);
    const currentVariant=s.variant,intent=JSON.stringify([pid,targetIds,currentOnly,currentVariant,metadata.values,generationDraft,inputSource,checked('aw-patterns'),checked('aw-difficulties'),$('aw-profile').value]);
    if(pendingSubmission?.intent!==intent){
      if(!currentOnly)await saveSettings(metadata);if(epoch!==projectEpoch||pid!==s.project?.id)return;
      const payload={request_id:crypto.randomUUID().replaceAll('-',''),segment_ids:targetIds,variants:currentOnly?[currentVariant]:s.project.variants.map(v=>v.key),settings:generationDraft,expected_revision:s.project.revision,...sourceOptions()};
      pendingSubmission={intent,payload};
    }
    notice('正在提交 '+targetIds.length+' 个片段…');const result=await api(base()+'/generation-batches','POST',pendingSubmission.payload);
    if(epoch!==projectEpoch||pid!==s.project?.id)return;pendingSubmission=null;if(!result.jobs.length){s.submittedBatch=previousSubmission;notice('静音尾段已排除，无需生成');renderDelivery();return;}
    s.submittedBatch={...anchor,id:result.id||result.batch_id,submitting:false};s.resultBatch=null;s.adoption.clear();localCandidateIds.clear();historicalCandidateIds.clear();
    notice('已加入队列 · '+result.jobs.length+' 个片段任务');renderDelivery();await pollTasks();
    }catch(error){if(s.submittedBatch===anchor)s.submittedBatch=previousSubmission;renderDelivery();throw error;}
    finally{busy.delete('generation-submission');}
  }
  async function regenerateCurrent(sid,key){
    await chooseSegment(sid);s.variant=key;renderProject();
    changeMode('generate');
    s.generationScope='current';$('aw-generation-scope').value='current';renderGenerationCount();
    await exclusive('aw-regenerate',()=>generate(true));
  }
  bind('aw-generate',()=>generate(false));bind('aw-regenerate',()=>generate(true));$('aw-engine').onchange=()=>{const next=$('aw-engine').value;if(generationDraft.engine==='mug')mugStrategyPreference=generationDraft.strategy==='fast'?'fast':'independent';generationDraft.engine=next;if(next==='mug')generationDraft.strategy=mugStrategyPreference;else{if(generationDraft.strategy==='fast')mugStrategyPreference='fast';generationDraft.strategy='independent';}updateStrategyControl();scheduleScheme();};
  function taskCard(task){
    const row=node('article',null,'aw-task');row.dataset.taskId=task.id;
    row.append(node('strong',null,'aw-task-status'),node('p',null,'aw-task-phase'),node('progress'),node('div',null,'aw-task-errors'),node('div',null,'aw-task-actions'));
    updateTaskCard(row,task);return row;
  }
  function updateTaskCard(row,task){
    const info=F.taskPresentation(task);row.dataset.status=task.status;
    row.querySelector('.aw-task-status').textContent=info.status;
    row.querySelector('.aw-task-phase').textContent=(task.task_type==='generation'?(s.project.segments.find(x=>x.id===task.segment_id)?.name||'旧区域')+' · ':'')+info.phase;
    const progress=row.querySelector('progress');progress.hidden=!info.active;progress.max=100;if(info.progress==null)progress.removeAttribute('value');else progress.value=info.progress;
    const errors=row.querySelector('.aw-task-errors'),signature=JSON.stringify(F.taskFailures(task));
    if(errors.dataset.signature!==signature){errors.dataset.signature=signature;errors.replaceChildren(...F.taskFailures(task).map(error=>node('p',error.scope+'：'+error.summary,'aw-error')));}
    const buttons=row.querySelector('.aw-task-actions');if(buttons.dataset.status===task.status)return;buttons.dataset.status=task.status;buttons.replaceChildren();
    if(['failed','interrupted'].includes(task.status))buttons.append(action('重试',()=>retryTask(task)));
    if(['queued','paused'].includes(task.status))buttons.append(action('取消排队',async()=>{const response=await fetch('/api/jobs/'+task.id,{method:'DELETE'});if(!response.ok)throw new Error('任务已开始，请等待完成');await pollTasks();}));
    if(task.status==='paused')buttons.append(action('继续队列',async()=>{const response=await fetch('/api/queue/resume',{method:'POST'});if(!response.ok)throw new Error('恢复失败');await pollTasks();}));
  }
  function syncTaskCards(host,tasks){const old=new Map([...host.querySelectorAll(':scope>.aw-task')].map(row=>[row.dataset.taskId,row]));for(const task of tasks){const row=old.get(task.id)||taskCard(task);updateTaskCard(row,task);if(row.parentNode!==host)host.append(row);old.delete(task.id);}for(const row of old.values())row.remove();}
  async function pollTasks(){if(!s.project||polling)return;polling=true;const pid=s.project.id;try{const [p,tasks,seps,trials]=await Promise.all([api(base()),api(base()+'/tasks'),api(base()+'/separations'),api(base()+'/separation-trials')]);if(s.project?.id!==pid||p.revision<s.project.revision)return;const previousTaskState=JSON.stringify((s.tasks||[]).map(t=>[t.id,t.status]));const changed=p.revision!==s.project.revision;s.project=p;s.tasks=tasks.tasks;if(previousTaskState!==JSON.stringify(s.tasks.map(t=>[t.id,t.status])))window.notifyGenerationChanged?.();await pollMusicAnalysis();s.stems=seps.stem_sets||[];refreshSourceControls();const separation=s.tasks.filter(t=>['separation','separation_trial'].includes(t.task_type)).sort((a,b)=>String(b.created).localeCompare(String(a.created))).slice(0,3);syncTaskCards($('aw-separation-tasks'),separation);await updateBatchDelivery(pid);const active=s.tasks.filter(t=>['queued','running','paused'].includes(t.status));$('aw-job-summary').textContent=active.length?active.length+' 个后台任务 · '+F.taskPresentation(active[0]).status:'完整原曲 '+T.time(p.duration)+' · '+p.segments.length+' 个实际片段';$('aw-trials').replaceChildren();for(const trial of trials.trials||[]){const detail=node('details');detail.append(node('summary','试分离 '+T.time(trial.core[0]/SR)+'–'+T.time(trial.core[1]/SR)));for(const role of trial.audio||[])detail.append(action('试听'+({vocals:'人声',accompaniment:'伴奏',original:'原曲'}[role.role]||role.role),()=>listenTrial(trial,role)));$('aw-trials').append(detail);}if(changed){renderSegments();renderVersions();renderExport();await syncWhole();}else{renderSegments();}if(reviewTask)await pollReview();}catch(e){notice('任务状态更新失败：'+e.message,true);}finally{polling=false;}}
  async function syncWhole(){if(!s.project)return;const p=s.project,key=s.variant,signature=p.id+':'+key+':'+p.segments.map(seg=>seg.active?.[key]||'').join('|');if(signature===wholeSignature)return;wholeSignature=signature;const context={pid:p.id,variant:key};const data=await Promise.all(p.segments.filter(seg=>seg.active[key]).map(async seg=>{const r=await getRevision(seg.active[key]);return r.events;}));if(context.pid!==s.project?.id||context.variant!==s.variant)return;wholeEvents=data.flat();if(!s.assembly)await prepareContinuous(s.preview);draw();}
  bind('aw-rule',async()=>{const r=selectedData()?.revision;if(!r)throw new Error('先预览要修订的谱面');const result=await api(base()+'/revisions/'+r.id+'/rules','POST');localCandidateIds.add(result.id);await pollTasks();await loadSelected(result.id);renderVersions();notice('规则修订已保留为新方案。');});
  bind('aw-fusion-tool',async()=>{const host=$('aw-tool-fusion');host.hidden=!host.hidden;if(host.hidden)return;host.replaceChildren();const rows=segment()?.versions[s.variant]||[],inputs={};for(const role of ['vocals','accompaniment']){const matches=rows.filter(v=>v.kind==='stem_raw'&&v.source_role===role&&v.range[0]===segment().start_sample&&v.range[1]===segment().end_sample),label=node('label',role==='vocals'?'人声原谱':'伴奏原谱','aw-field'),select=node('select');select.append(...matches.slice().reverse().map(v=>option(v.stats.notes+' 音符 · '+v.id.slice(0,6),v.id)));inputs[role]=select;label.append(select);host.append(label);}host.append(action('生成合并新方案',async()=>{if(!inputs.vocals.value||!inputs.accompaniment.value)throw new Error('先完成同范围的两路原谱生成');const plan=await api(base()+'/section-plans','POST');const result=await api(base()+'/segments/'+s.segmentId+'/fuse','POST',{vocal_revision:inputs.vocals.value,accompaniment_revision:inputs.accompaniment.value,plan_id:plan.id,expected_revision:s.project.revision});localCandidateIds.add(result.id);await pollTasks();await loadSelected(result.id);notice('已创建合并候选，没有重复模型推理。');}));});
  $('aw-agent-tool').onclick=()=>{$('aw-tool-agent').hidden=!$('aw-tool-agent').hidden;};
  bind('aw-layout-history',async()=>{const data=await api(base()+'/layouts'),host=$('aw-old-layouts');host.replaceChildren();for(const layout of data.layouts){const item=node('details');item.append(node('summary',new Date(layout.created).toLocaleString('zh-CN')+' · '+layout.segments.length+' 个原片段'));for(const seg of layout.segments){item.append(node('strong',seg.name));for(const [key,rows]of Object.entries(seg.versions))for(const row of rows)item.append(action(key+' · '+row.stats.notes+' 音符 · '+(kinds[row.kind]||'历史'),async()=>{await loadSelected(row.id,seg.start_sample/SR);range=[seg.start_sample/SR,seg.end_sample/SR];renderRange();draw();notice('正在预览划段前历史；采用需要先撤销到对应布局。');}));}host.append(item);}if(!data.layouts.length)host.append(node('p','还没有划段前历史。','aw-hint'));});
  bind('adv-review',async()=>{const rid=segment()?.active[s.variant];if(!rid)throw new Error('请先采用要审核的谱面');const a=Math.max(segment().start_sample/SR,Math.min(position(),segment().end_sample/SR-.25)),b=Math.min(segment().end_sample/SR,a+12);reviewTask=await api(base()+'/reviews','POST',{revision_id:rid,start_ms:a*1000,end_ms:b*1000,goal:$('adv-goal').value,send_audio:$('adv-send-audio').checked});await pollReview();notice('Agent 分析已开始；结果不会自动替换谱面。');});
  async function pollReview(){if(!reviewTask)return;const pid=s.project.id,task=await api(base()+'/reviews/'+reviewTask.id);if(pid!==s.project?.id)return;reviewTask=task;const host=$('aw-review-tasks');host.replaceChildren(node('p',task.message||task.status));if(task.usage)host.append(node('p','用量：'+JSON.stringify(task.usage),'aw-hint'));if(task.status==='failed'||task.status==='interrupted')host.append(node('p',task.error||'分析已中断，可重新提交。','aw-error'));if(task.status!=='completed')return;const groups=task.groups||task.proposal?.groups||[];for(const group of groups){const label=node('label',null,'aw-review-group'),input=node('input');input.type='checkbox';input.value=group.id;input.checked=true;label.append(input,node('span',group.reason||group.summary||'修改建议'));host.append(label);}if(groups.length)host.append(action('将所选建议保存为新方案',async()=>{const ids=[...host.querySelectorAll('input:checked')].map(i=>i.value);const result=await api(base()+'/reviews/'+task.id+'/apply','POST',{groups:ids,activate:false});localCandidateIds.add(result.id);reviewTask=null;await pollTasks();await loadSelected(result.id);notice('已保存 Agent 修订候选，试听后决定是否使用。');}));}
  const apiDialog=node('dialog',null,'aw-api-dialog');apiDialog.id='adv-api-dialog';apiDialog.innerHTML='<header><h2>Agent 连接设置</h2><button id="adv-api-close" aria-label="关闭 Agent 设置">×</button></header>'+['repair','audio'].map(role=>`<fieldset><legend>${role==='repair'?'修谱模型':'听音模型（可选）'}</legend><label class="aw-field">地址<input id="aw-api-${role}-url" type="url" placeholder="https://api.openai.com/v1"></label><label class="aw-field">接口<select id="aw-api-${role}-protocol"><option value="responses">Responses</option><option value="chat">Chat Completions</option></select></label><label class="aw-field">模型<input id="aw-api-${role}-model"></label><label class="aw-field">密钥<input id="aw-api-${role}-key" type="password" autocomplete="off"></label><button id="aw-api-${role}-probe">检查连接</button></fieldset>`).join('')+'<label class="aw-check"><input id="aw-api-persist" type="checkbox">加密保存到本机</label><p id="aw-api-status" role="status"></p><button id="aw-api-save" class="aw-primary">保存连接设置</button>';document.body.append(apiDialog);
  $('adv-api-close').onclick=()=>apiDialog.close();bind('adv-api-open',async()=>{const config=await api('/agent/config');for(const role of ['repair','audio'])for(const field of ['url','protocol','model'])$('aw-api-'+role+'-'+field).value=config[role]?.[field==='url'?'base_url':field]||'';apiDialog.showModal();});
  function apiPayload(){const payload={remember:$('aw-api-persist').checked};for(const role of ['repair','audio']){if(role==='audio'&&!$('aw-api-audio-model').value)continue;payload[role]={};for(const field of ['url','protocol','model','key'])payload[role][field==='url'?'base_url':field]=$('aw-api-'+role+'-'+field).value;}return payload;}
  bind('aw-api-save',async()=>{await api('/agent/config','POST',apiPayload());for(const role of ['repair','audio'])$('aw-api-'+role+'-key').value='';$('aw-api-status').textContent='连接设置已保存。';});for(const role of ['repair','audio'])bind('aw-api-'+role+'-probe',async()=>{await api('/agent/config','POST',apiPayload());const result=await api('/agent/probe/'+role,'POST');$('aw-api-status').textContent=JSON.stringify(result);});
  function exportTab(value){$('aw-export-area').hidden=!value;syncPrimaryAction();$('aw-candidate-area').hidden=value;$('aw-show-export').setAttribute('aria-pressed',String(value));$('aw-show-candidates').setAttribute('aria-pressed',String(!value));renderExport();}
  $('aw-show-export').onclick=()=>exportTab(true);$('aw-show-candidates').onclick=()=>exportTab(false);
  function renderExport(){const host=$('aw-export-summary');host.replaceChildren();const selection=$('aw-export-segments');selection.replaceChildren();if(!s.project)return;const choices=node('details',null,'aw-block');choices.append(node('summary','片段选择 · '+s.project.segments.filter(p=>p.included).length+'/'+s.project.segments.length+' 段'));selection.append(choices);for(const part of s.project.segments){const label=node('label',null,'aw-scope-choice'),check=node('input');check.type='checkbox';check.checked=part.included;check.onchange=()=>run(async()=>{await acceptProject(await api(base()+'/segments/'+part.id,'PATCH',{included:check.checked,expected_revision:s.project.revision}),false);});label.append(check,node('span',part.name+' · '+T.time(part.start_sample/SR)+'–'+T.time(part.end_sample/SR)));choices.append(label);}const selected=W.effectiveSegments(s.project).filter(x=>x.included),duration=selected.reduce((sum,x)=>sum+(Math.min(x.end_sample,W.contentEnd(s.project))-x.start_sample)/SR,0);host.append(node('strong',selected.length+' 个片段 · 保留 '+T.time(duration)));const retained=node('details');retained.append(node('summary','保留的音乐片段'));for(const seg of selected)retained.append(node('p',seg.name+' · '+T.time(seg.start_sample/SR)+'–'+T.time(seg.end_sample/SR),'aw-hint'));host.append(retained);if(s.assembly)host.append(node('p','正在预览 · '+T.time(s.assembly.duration)+' · '+(s.assembly.seam_changes?.length||0)+' 项接缝处理','aw-hint'));const readiness=W.exportReadiness(s.project);$('adv-assemble').disabled=!W.allExportReady(s.project)||busy.has('adv-assemble');
    for(const row of readiness){host.append(node('p',(patterns[row.pattern]||row.pattern)+' · '+row.difficulty+'：'+(row.missing.length?'缺少 '+row.missing.join('、'):'可以导出'),row.missing.length?'aw-error':'aw-ready'));
      for(const id of row.missingIds){const part=s.project.segments.find(p=>p.id===id),recovery=W.recoveryFor(part,row.key,s.tasks),block=node('div',null,'aw-export-recovery');
        block.append(action('查看 '+part.name,async()=>{await chooseSegment(id);s.variant=row.key;await loadSelected(part.active?.[row.key]);changeMode('finish');$('aw-show-candidates').click();renderProject();}));
        if(recovery.kind==='retry')block.append(action('重试 '+part.name,()=>retryTask(recovery.task),'aw-primary'));
        else if(recovery.kind==='waiting')block.append(node('span',part.name+' 正在排队或生成，请等待完成后采用','aw-hint'));
        else if(recovery.kind==='choose')block.append(action('查看已生成方案',async()=>{await chooseSegment(id);s.variant=row.key;localCandidateIds.add(recovery.version.id);await loadSelected(recovery.version.id);changeMode('finish');$('aw-show-candidates').click();renderProject();},'aw-primary'));
        else block.append(action('重新生成 '+part.name,()=>regenerateCurrent(id,row.key)));
        host.append(block);
      }
    }if(selected.length<s.project.segments.length)host.append(node('p','未选片段的音乐和时间将一起删除。','aw-hint'));const list=$('adv-assemblies');list.replaceChildren();const products=[...s.project.assemblies].reverse(),current=products.find(item=>item.id===s.assembly?.id)||products[0];
    function productCard(item,latest){const row=node('article',null,'aw-assembly');row.append(node('strong',(latest?'当前成品 · ':'历史成品 · ')+T.time(item.duration)),node('p',new Date(item.created).toLocaleString('zh-CN')+' · '+item.charts.length+' 个组合','aw-hint'),action('预览成品',()=>loadAssembly(item.id)));const link=node('a','下载 MCZ','aw-primary');link.href=item.download;row.append(link);if(item.missing?.length)row.append(node('p',item.missing.length+' 个组合没有完整版本，未导出。','aw-hint'));return row;}
    if(current)list.append(productCard(current,true));
    const history=products.filter(item=>item.id!==current?.id);if(history.length){const detail=node('details',null,'aw-block');detail.append(node('summary','历史成品（'+history.length+'）'));for(const item of history)detail.append(productCard(item,false));list.append(detail);}}

  bind('adv-assemble',async()=>{const metadata=captureMetadata();await saveMetadata(metadata);assertMetadataContext(metadata);const pid=s.project.id,p=await api(base());if(pid!==s.project?.id)return;await acceptProject(p,false);assertMetadataContext(metadata);if(!W.allExportReady(s.project))throw new Error('所选组合尚未全部完成，请补齐并采用缺少的区域方案后导出');const a=await api(base()+'/assemblies','POST',{preroll:Number($('adv-preroll').value),expected_revision:s.project.revision,require_complete:true});await pollTasks();const product=s.project.assemblies.find(item=>item.id===a.id)||a;if(!product.download)throw new Error('成品已准备好，但下载地址不可用，请重新导出');const link=document.createElement('a');link.href=product.download;link.download='';document.body.append(link);link.click();link.remove();notice('完整 MCZ 已准备好，正在下载。');});
  async function loadAssembly(id){continuous=null;++continuousEpoch;resultAudio.reset();const context=store.context(),report=await api(base()+'/assemblies/'+id);if(!store.matches(context))return;s.assembly={...report,id};s.compare=null;s.preview=null;s.parallel=false;const available=report.charts.map(v=>v.key);if(!available.includes(s.variant))s.variant=available[0];context.variant=s.variant;const chart=await api(base()+'/assemblies/'+id+'/charts/'+encodeURIComponent(s.variant));if(!store.matches(context))return;s.preview={...chart,events:report.preview[s.variant]||[]};if(!store.matches(context))return;await setSource('assembly:'+id,0,false);if(!store.matches(context))return;range=[0,report.duration];renderProject();renderComparison();$('aw-leave-assembly').hidden=false;$('aw-timeline-context').textContent='成品预览 · 原曲时间轴仅供来源对照';draw();}
  bind('aw-leave-assembly',async()=>{s.assembly=null;$('aw-leave-assembly').hidden=true;$('aw-timeline-context').textContent='分析建议和实际片段分别显示';await chooseSegment(s.segmentId);renderProject();});
  async function setSource(source,at=position(),play=false){window.ChartArcade?.close();const epoch=++audioEpoch;playbackIntent=play;pendingSource={epoch,at};audio.pause();activeTrial=null;s.source=source;const url=source==='original'?'/api/advanced'+base()+'/audio':source.startsWith('assembly:')?'/api/advanced'+base()+'/assemblies/'+source.slice(9)+'/audio':'/api/advanced'+base()+'/sources/'+encodeURIComponent(source)+'/audio';if(audio.getAttribute('src')!==url)audio.src=url;try{await waitAudio(epoch);}catch(e){if(epoch===audioEpoch){pendingSource=null;playbackIntent=false;}throw e;}if(epoch!==audioEpoch)return;pendingSource=null;audio.currentTime=Math.max(0,Math.min(at,Number.isFinite(audio.duration)?audio.duration:at));const sources=$('adv-listen-source');for(const opt of [...sources.options])if(opt.value.startsWith('assembly:')&&opt.value!==source)opt.remove();if(source.startsWith('assembly:')&&![...sources.options].some(opt=>opt.value===source))sources.append(option('试听剪辑成品',source));sources.value=source;$('aw-playing-source').textContent='当前声音：'+window.AdvancedResultAudio.sourceLabel(source,s.stems).split(' · ')[0]+(resultAudio.resolve(s.stems).explicit?' · 单独试听':'');await matchVolume(source);if(epoch!==audioEpoch)return;prepareSpectrum(source);draw();if(playbackIntent)await playAudio();}
  function waitAudio(epoch){return new Promise((resolve,reject)=>{if(audio.readyState>=1)return resolve();let timer;const cleanup=()=>{clearTimeout(timer);audio.removeEventListener('loadedmetadata',done);audio.removeEventListener('error',failed);};const done=()=>{cleanup();resolve();};const failed=()=>{cleanup();if(epoch===audioEpoch)reject(new Error('试听音源加载失败，请重新选择'));else resolve();};audio.addEventListener('loadedmetadata',done);audio.addEventListener('error',failed);timer=setTimeout(()=>{cleanup();reject(new Error('音源加载超时，请重试'));},20000);});}
  async function matchVolume(source){audio.volume=1;if(!$('aw-match-volume').checked||source.startsWith('assembly:')||activeTrial)return;const pid=s.project.id,epoch=audioEpoch;for(const id of ['original',source]){const key=pid+':'+id;if(!sourceMeta[key])sourceMeta[key]=await api(base()+'/audition-metadata?source_id='+encodeURIComponent(id));}if(pid!==s.project?.id||epoch!==audioEpoch||source!==s.source)return;audio.volume=window.SeparationAudition.matchingVolumes([sourceMeta[pid+':original'],sourceMeta[pid+':'+source]])[1];}
  $('aw-match-volume').onchange=()=>run(()=>matchVolume(s.source));
  $('adv-listen-source').onchange=()=>run(async()=>{if(s.assembly)throw new Error('先返回原曲工作台再切换声部');resultAudio.override($('adv-listen-source').value);await setSource($('adv-listen-source').value,position(),playbackIntent);});
  async function listenTrial(trial,row){window.ChartArcade?.close();const at=position(),playing=playbackIntent,epoch=++audioEpoch;pendingSource={epoch,at};audio.pause();activeTrial={offset:trial.core[0]/SR,end:trial.core[1]/SR};audio.src=row.audio_url;try{await waitAudio(epoch);}catch(e){if(epoch===audioEpoch){pendingSource=null;playbackIntent=false;}throw e;}if(epoch!==audioEpoch)return;pendingSource=null;audio.currentTime=Math.max(0,Math.min((trial.core[1]-trial.core[0])/SR,at-activeTrial.offset));$('aw-playing-source').textContent='试听：局部分离 · '+row.role;draw();if(playbackIntent)await playAudio();}
  function updateTimingAnchors(){if(pane&&s.project)pane.setAnchors(s.assembly?[]:T.timingAnchors(s.project.tempo,s.musicAnalysis?.timing_map,s.project.duration));}
  async function prepareSpectrum(source){if(!pane||!s.project)return;const epoch=++spectrumEpoch,pid=s.project.id;try{const manifest=await api(base()+'/analysis','POST',{source_id:source,start_ms:Math.max(0,position()*1000-300),end_ms:position()*1000+5000});if(epoch!==spectrumEpoch||pid!==s.project?.id)return;const prefix='/api/advanced/projects/'+pid+'/analysis/';pane.setSource({initialManifest:manifest,manifestUrl:prefix+manifest.analysis_id,tileUrl:(aid,level,index)=>prefix+aid+'/tiles/'+level+'/'+index,label:source==='original'?'原曲频谱':'当前音轨频谱',isAssembly:source.startsWith('assembly:')});updateTimingAnchors();draw();}catch(e){if(epoch===spectrumEpoch)notice('频谱暂不可用：'+e.message,true);}}
  function seek(value){if(!s.project)return;if(s.submittedBatch)s.submittedBatch.autoAllowed=false;if(arcadeTransport&&window.ChartArcade?.isActive()){window.ChartArcade.seek(value*1000-arcadeTransport.offset);}else audio.currentTime=Math.max(0,Math.min(Number.isFinite(audio.duration)?audio.duration:s.project.duration,value-(activeTrial?.offset||0)));s.position=position();draw();}
  function bounds(){if(activeTrial)return [activeTrial.offset,activeTrial.end];return $('adv-loop').checked&&range[1]>range[0]?range:[0,s.assembly?.duration||s.project?.duration||0];}
  async function playAudio(){playbackIntent=true;const lease=window.MusicPlayback?.claim(audio,'advanced-preview');if(lease)await window.MusicPlayback.play(lease);else await audio.play();if(!playbackIntent)audio.pause();}
  $('adv-play').onclick=()=>run(async()=>{if(!s.project)return;if(window.ChartArcade?.isActive())throw new Error('请先退出试玩');if(!audio.paused||pendingSource&&playbackIntent){pauseAudio();return;}const [a,b]=bounds();if(position()<a||position()>=b-.005)seek(a);await playAudio();});$('adv-seek').oninput=()=>seek(Number($('adv-seek').value));
  $('adv-seek').onpointerdown=e=>{seekDragging=true;$('adv-seek').setPointerCapture?.(e.pointerId);};
  $('adv-seek').onpointerup=$('adv-seek').onpointercancel=$('adv-seek').onlostpointercapture=$('adv-seek').onblur=()=>{seekDragging=false;draw();};
  function playbackFrame(){raf=0;if(!visible)return;const [a,b]=bounds();if(!audio.paused&&position()>=b){if($('adv-loop').checked){seek(a);}else pauseAudio();}draw();if(!audio.paused)raf=requestAnimationFrame(playbackFrame);}
  for(const name of ['play','pause','timeupdate','loadedmetadata'])audio.addEventListener(name,()=>{draw();if(!audio.paused&&!raf)raf=requestAnimationFrame(playbackFrame);});audio.addEventListener('ended',()=>{if($('adv-loop').checked){seek(bounds()[0]);run(playAudio);}});
  function draw(){if(!visible)return;followPlayback();const data=selectedData(),events=s.assembly?s.assembly.preview?.[s.variant]||[]:continuousEnabled()?continuous.events:data?.events||(segment()?[]:wholeEvents),now=position()*1000,speed=Number($('adv-speed').value),z=canvasSize($('adv-chart-before'));if(z){R.render(z.ctx,z.w,z.h,{...window.GameAppearance?.get(),notes:prepare(s.parallel?s.preview?.events||[]:events),now,speed,caption:(s.assembly?'剪辑成品':s.compare?(s.slot==='a'?'A 正在使用':'B 新方案'):data?'片段谱面':'原曲 · 已采用谱面')+' · '+speed.toFixed(1)+'×',night:document.documentElement.dataset.theme==='dark'});if(s.compare)drawDiff(z,s.parallel?'a':s.slot,now,speed);if(!window.ChartArcade?.isActive())spectrumFrame({displayTimeMs:now},R.geometry(z.w,z.h),5000/speed);}if(s.parallel&&s.compare){const second=canvasSize($('adv-chart-after'));if(second)R.render(second.ctx,second.w,second.h,{...window.GameAppearance?.get(),notes:prepare(s.compare.events),now,speed,caption:'B 新方案 · '+speed.toFixed(1)+'×'});if(second)drawDiff(second,'b',now,speed);}const populated=events.length||data?.chart||s.assembly;$('adv-preview-empty').hidden=!!populated;$('adv-preview-empty').querySelector('p').textContent=s.project?(segment()?'这个组合还没有谱面。可以生成当前片段。':'可以先试听，再确认右侧区域方案。'):'打开完整音乐，在时间轴上划段后生成。';$('aw-empty-action').textContent=s.project?(segment()?'查看生成进度':'查看方案'):'打开音乐';$('adv-preview-note').textContent=s.assembly?'成品时钟 · 已删去未选音乐':data?(data.revision?.id===segment()?.active[s.variant]?'正在使用':'预览候选')+' · '+(data.revision?.provenance?.source_role==='vocals'?'人声原谱':data.revision?.provenance?.source_role==='accompaniment'?'伴奏原谱':kinds[data.revision?.kind]||'谱面')+' · '+(data.stats?.notes??events.length)+' 音符':'原曲时钟 · 不改变音乐速度';$('adv-current-time').textContent=T.time(position());$('adv-total-time').textContent=T.time(s.assembly?.duration||s.project?.duration||0);$('adv-play').textContent=audio.paused?'▶ 试听':'Ⅱ 暂停';const seekMax=String(s.assembly?.duration||s.project?.duration||1);if($('adv-seek').max!==seekMax)$('adv-seek').max=seekMax;if(!seekDragging)$('adv-seek').value=position();$('adv-play-game').disabled=$('adv-autoplay').disabled=!data?.chart&&!s.assembly;drawTimeline();}
  function drawDiff(z,slot,now,speed){const g=R.geometry(z.w,z.h),ctx=z.ctx;ctx.save();ctx.beginPath();ctx.rect(0,g.header,z.w,g.hit-g.header);ctx.clip();for(const change of s.diff||[]){const e=slot==='a'?change.before:change.after;if(!e)continue;const y=g.hit-(e.start_ms-now)/(5000/speed)*g.runway;if(y<g.header-10||y>g.hit+10)continue;ctx.strokeStyle=ctx.fillStyle=slot==='a'?'#f3a9c4':'#b0f4de';ctx.lineWidth=1.5;ctx.setLineDash(slot==='a'?[3,3]:[]);ctx.strokeRect(e.lane*g.lane+g.lane*.17,y-8,g.lane*.66,16);ctx.font='600 13px Bahnschrift, sans-serif';ctx.fillText(change.type==='delete'?'−':change.type==='add'?'＋':'↔',e.lane*g.lane+g.lane*.05,y+4);}ctx.restore();}
  function spectrumFrame(snapshot,geometry,travel){spectrumProjection={snapshot:{...snapshot},projection:{...geometry,travelMs:travel}};const z=canvasSize(spectrumCanvas);if(z&&pane)pane.render(snapshot,{...geometry,travelMs:travel,timeToY:t=>geometry.hit-(t-snapshot.displayTimeMs)/travel*geometry.runway});}
  // Keep the drag projection fixed while the audio clock continues advancing.
  function spectrumSeek(event){if(!spectrumDrag)return;const y=event.clientY-spectrumCanvas.getBoundingClientRect().top;const ms=window.SpectrumPane.yToTime(y,spectrumDrag.snapshot,spectrumDrag.projection);if(Number.isFinite(ms))seek(ms/1000);}
  spectrumCanvas.onpointerdown=event=>{if(event.button!==0||!s.project||!spectrumProjection)return;event.preventDefault();spectrumDrag=spectrumProjection;spectrumCanvas.setPointerCapture?.(event.pointerId);spectrumSeek(event);};
  spectrumCanvas.onpointermove=event=>{if(spectrumDrag){event.preventDefault();spectrumSeek(event);}};
  spectrumCanvas.onpointerup=spectrumCanvas.onpointercancel=spectrumCanvas.onlostpointercapture=()=>{spectrumDrag=null;};
  $('aw-empty-action').onclick=()=>{if(!s.project)$('aw-new').click();else changeMode(segment()?'generate':'prepare');};
  $('adv-speed').value=localStorage.getItem('startrail.advanced-speed')||'8';$('adv-speed-value').textContent=Number($('adv-speed').value).toFixed(1)+'×';$('adv-speed').oninput=()=>{$('adv-speed-value').textContent=Number($('adv-speed').value).toFixed(1)+'×';localStorage.setItem('startrail.advanced-speed',$('adv-speed').value);draw();};
  $('aw-spectrum-toggle').onclick=()=>{const hidden=$('adv-spectrum-stage').classList.toggle('aw-no-spectrum');$('aw-spectrum-toggle').setAttribute('aria-pressed',String(!hidden));draw();};
  async function playGame(autoplay){
    const payload=selectedData();if(!payload?.chart)throw new Error('有音符的谱面才能试玩');pauseAudio();
    if(s.submittedBatch)s.submittedBatch.autoAllowed=false;
    const pid=s.project.id,rid=payload.revision?.id,offset=s.assembly?0:payload.range[0]*1000/SR;
    const data=s.assembly?payload:{...payload,audio_url:resultAudio.gameAudioUrl(payload)};
    window.ChartArcade.close();const transport={offset};arcadeTransport=transport;
    await window.ChartArcade.open('advanced',s.variant,{autoplay,data,sourceTimeOffsetMs:offset,host:'adv-game-host',speedInput:$('adv-speed'),onFrame:(snapshot,projection,canvas)=>{if(arcadeTransport!==transport||pid!==s.project?.id||rid!==selectedData()?.revision?.id)return;s.position=(snapshot.displayTimeMs+offset)/1000;draw();const delta=canvas.getBoundingClientRect().top-spectrumCanvas.getBoundingClientRect().top;spectrumFrame({...snapshot,displayTimeMs:snapshot.displayTimeMs+offset},{...projection,header:projection.header+delta,hit:projection.hit+delta},projection.travelMs||5000/Number($('adv-speed').value));},onClose:()=>{if(arcadeTransport!==transport)return;const at=s.position;arcadeTransport=null;if(Number.isFinite(at)){audio.currentTime=Math.max(0,Math.min(Number.isFinite(audio.duration)?audio.duration:Infinity,at-(activeTrial?.offset||0)));}draw();},onPositionJump:ms=>seek((ms+offset)/1000)});
  }
  $('adv-play-game').onclick=()=>run(()=>playGame(false));$('adv-autoplay').onclick=()=>run(()=>playGame(true));

  function renderDelivery(){
    const host=$('aw-delivery'),submission=s.submittedBatch;host.replaceChildren();host.hidden=!submission;
    if(!submission)return;host.append(node('strong',submission.submitting?'正在提交本次生成…':submission.finished?'本次生成已结束':'本次生成已加入队列'));
    if(!submission.id)return;
    host.append(action(submission.finished?'查看本次结果':'查看本次进度',()=>openBatch(submission.id),submission.finished?'aw-primary':''));
  }
  function renderBatchResults(){
    const host=$('aw-batch-results'),batch=s.resultBatch;host.replaceChildren();$('aw-use-selected').hidden=!batch;
    $('aw-result-title').textContent=batch?'本次结果 · '+new Date(batch.created).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):'当前片段';
    if(!batch)return;
    const primary=batch.results.filter(r=>r.primary),ready=primary.filter(r=>r.adoptable);
    host.append(node('p',ready.length+' 个可采用结果 · '+(batch.counts.failed+batch.counts.partial)+' 个任务需要检查','aw-hint'));
    for(const job of batch.jobs.filter(job=>!job.superseded)){
      const part=s.project.segments.find(p=>p.id===job.segment_id),block=node('section',null,'aw-batch-part');
      block.append(node('strong',part?.name||job.segment_name||'历史片段'));
      for(const key of job.latest_variants||job.variants||[]){
        const row=primary.find(r=>r.job_id===job.id&&r.variant===key),line=node('div',null,'aw-batch-choice');
        if(row){
          const check=node('input');check.type='checkbox';check.checked=s.adoption.has(row.revision_id);check.disabled=!row.adoptable;
          check.setAttribute('aria-label','采用 '+(part?.name||'历史片段')+' '+key);check.onchange=()=>{check.checked?s.adoption.add(row.revision_id):s.adoption.delete(row.revision_id);updateAdoptionAction();};
          const button=action(key.replace('--',' · '),()=>previewBatchRow(row));button.disabled=!part||!row.current_range;
          const viewing=selectedData()?.revision?.id===row.revision_id,adopted=part?.active?.[key]===row.revision_id;
          line.dataset.viewing=String(viewing);line.append(check,button,node('span',!row.current_range?'范围已变更':adopted?'已采用':viewing?'正在查看':'可预览','aw-result-state'));
          line.append(node('span',window.MalodyDensity?.label(row.density_validation)||'密度未评估','aw-density-summary'));
        }else{line.append(node('span',key.replace('--',' · ')),node('span',['running','queued','paused'].includes(job.status)?'处理中':'生成失败','aw-result-state'));}
        block.append(line);
      }
      const failures=job.errors||[];if(job.error||failures.length){const detail=node('details');detail.append(node('summary','查看失败原因'),node('p',job.error||failures.map(x=>typeof x==='string'?x:(x.error||x.message||JSON.stringify(x))).join('；'),'aw-error'));block.append(action('重试此片段',()=>retryTask(job),'aw-primary'));block.append(detail);}
      const details=node('details');details.append(node('summary','生成细节与两路原谱'));
      for(const row of batch.results.filter(r=>r.job_id===job.id&&!r.primary)){const button=action((row.source_role==='vocals'?'人声':row.source_role==='accompaniment'?'伴奏':kinds[row.kind]||row.kind)+' · '+row.variant.replace('--',' · '),()=>previewBatchRow(row));button.disabled=!row.current_range;details.append(button);}
      details.append(action('打开任务文件夹',()=>openHistoryFolder(job.id))); block.append(details);host.append(block);
    }
    updateAdoptionAction();
  }
  function updateAdoptionAction(){const count=W.batchChoices(s.project,s.resultBatch,s.adoption).length;$('aw-use-selected').disabled=!count;$('aw-use-selected').textContent='采用本次勾选的 '+count+' 个结果';}
  async function retryTask(job){
    if(busy.has('retry:'+job.id))return;busy.add('retry:'+job.id);
    try{const pid=s.project.id,bid=job.batch_id||(s.resultBatch?.jobs.some(t=>t.id===job.id)?s.resultBatch.id:job.id),response=await fetch('/api/jobs/'+job.id+'/regenerate',{method:'POST'}),data=await response.json();if(!response.ok)throw new Error(data.detail||'重试失败');
    if(pid!==s.project?.id)return;if(data.status==='skipped'){notice(data.message);return;}
    if(bid){for(const row of (s.resultBatch?.results||[]).filter(r=>r.segment_id===job.segment_id&&(job.latest_variants||job.variants).includes(r.variant)))s.adoption.delete(row.revision_id);
      s.submittedBatch={id:bid,projectId:pid,segmentId:s.segmentId,variant:s.variant,autoAllowed:false,finished:false,delivered:false};
      const batch=await api(base()+'/generation-batches/'+bid);if(pid!==s.project?.id)return;s.resultBatch=batch;renderVersions();renderDelivery();}
    if(data.status==='skipped'){notice(data.message);return;}
    notice(data.reused?'此任务已在队列中，未重复提交。':'重试已加入队尾，完成后请查看本次结果并采用。');await pollTasks();renderExport();
    }finally{busy.delete('retry:'+job.id);}
  }
  async function openHistoryFolder(recordId){const response=await fetch('/api/task-history/open-folder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id:recordId})});const data=await response.json();if(!response.ok)throw new Error(data.detail||'无法打开文件夹');}
  async function previewBatchRow(row){
    if(s.submittedBatch)s.submittedBatch.autoAllowed=false;
    const part=s.project.segments.find(p=>p.id===row.segment_id);if(!part||!row.current_range)throw new Error('片段范围已变更，请重新生成');
    const at=s.segmentId===part.id?position():part.start_sample/SR;s.segmentId=part.id;s.variant=row.variant;s.assembly=null;range=[part.start_sample/SR,part.end_sample/SR];
    $('adv-variant').value=row.variant;renderRange();
    if(row.primary&&part.active?.[row.variant]&&part.active[row.variant]!==row.revision_id){await compareWith(row.revision_id);await selectSlot('b');}
    else await loadSelected(row.revision_id,at);
    renderSegments();
  }
  async function openBatch(id){
    const pid=s.project.id,epoch=++resultEpoch;notice('正在打开本次结果…');
    const [batch,project]=await Promise.all([api(base()+'/generation-batches/'+id),api(base())]);
    if(pid!==s.project?.id||epoch!==resultEpoch)return;await acceptProject(project,false);
    s.resultBatch=batch;s.adoption=new Set(batch.results.filter(r=>r.primary&&r.adoptable).map(r=>r.revision_id));localCandidateIds.clear();historicalCandidateIds.clear();historyOpen=false;$('aw-result-history-list').hidden=true;
    changeMode('finish');exportTab(false);renderVersions();view.querySelector('.aw-panel-scroll').scrollTop=0;
    const first=batch.results.find(r=>r.primary&&r.adoptable&&['fusion','fusion_candidate'].includes(r.kind))||batch.results.find(r=>r.primary&&r.adoptable);
    if(first){await previewBatchRow(first);notice('已打开本批 '+batch.counts.total+' 段结果，可先试听再采用。');}else notice('本次没有可预览的成功结果，请查看失败原因。',true);
  }
  async function updateBatchDelivery(pid){
    const submission=s.submittedBatch,host=$('aw-generation-tasks');
    if(!submission){const active=s.tasks.filter(t=>t.task_type==='generation'&&['queued','running','paused'].includes(t.status));syncTaskCards(host,active);return;}
    if(!submission.id){renderDelivery();return;}
    const batch=await api(base()+'/generation-batches/'+submission.id);if(pid!==s.project?.id||s.submittedBatch!==submission)return;
    const current=batch.jobs.filter(t=>!t.superseded),active=current.filter(t=>['queued','running','paused'].includes(t.status));
    if(active.length){
      let summary=host.querySelector('.aw-current-batch');
      if(!summary){summary=node('section',null,'aw-current-batch');summary.append(node('strong'),node('progress'));const taskList=node('div',null,'aw-current-task-list');summary.append(taskList);host.replaceChildren(summary);}
      const done=current.length-active.length;summary.querySelector('strong').textContent='本次进度 · '+done+'/'+current.length+' 个区域';
      const progress=summary.querySelector('progress');progress.max=current.length;progress.value=done+active.reduce((sum,t)=>sum+(Number(s.tasks.find(x=>x.id===t.id)?.progress)||0)/100,0);
      syncTaskCards(summary.querySelector('.aw-current-task-list'),current.map(t=>s.tasks.find(x=>x.id===t.id)||t));
    }else if(!submission.finished){
      host.replaceChildren();delete host.dataset.progressSignature;
      submission.finished=true;
      const auto=W.completionCanOpen(submission,s,{visible,playing:playbackIntent||!audio.paused,game:window.ChartArcade?.isActive()});submission.delivered=true;
      if(auto)await openBatch(submission.id);
    }
    if(s.resultBatch?.id===batch.id&&JSON.stringify(s.resultBatch)!==JSON.stringify(batch)){s.resultBatch=batch;renderVersions();}renderDelivery();
  }
  $('aw-result-history').onclick=()=>run(async()=>{
    const host=$('aw-result-history-list');historyOpen=!historyOpen;host.hidden=!historyOpen;if(!historyOpen)return;
    const pid=s.project.id;host.replaceChildren(node('p','正在读取历史…','aw-hint'));
    const response=await fetch('/api/task-history?type=advanced&page_size=10&project_id='+encodeURIComponent(pid)),data=await response.json();
    if(pid!==s.project?.id||!historyOpen)return;if(!response.ok)throw new Error(data.detail||'历史读取失败');host.replaceChildren();
    for(const record of data.items||[])host.append(action(new Date(record.created_at||record.created).toLocaleString('zh-CN')+' · '+(record.section_count||record.counts?.total||'')+' 段',()=>openBatch(record.batch_id||record.result_target?.batch_id)));
    if(!data.items?.length)host.append(node('p','没有可准确关联的历史批次。','aw-hint'));
    const past=node('details');past.append(node('summary','当前片段更早的版本（历史）'));for(const v of [...(segment()?.versions[s.variant]||[])].reverse())past.append(action((kinds[v.kind]||'历史版本')+' · '+v.stats.notes+' 音符 · '+new Date(v.created).toLocaleString('zh-CN'),async()=>{historicalCandidateIds.add(v.id);await loadSelected(v.id);notice('正在预览历史版本，点击「使用这一版」可恢复。');}));host.append(past);
    host.append(action('查看全部完成记录',()=>window.TaskHistoryUI?.openCompleted?.({type:'advanced',projectId:s.project.id})));
  });
  async function openResult(target){
    const pid=target.projectId||target.project_id,bid=target.batchId||target.batch_id,aid=target.assemblyId||target.assembly_id;
    if(!visible)showView('advanced');await enter();if(pid&&s.project?.id!==pid)await openProject(pid);
    if(aid){changeMode('finish');exportTab(true);await loadAssembly(aid);}
    else if(bid)await openBatch(bid);else{changeMode(target.mode==='prepare'?'prepare':'finish');if(target.stemSetId||target.stem_set_id){const set=s.stems.find(x=>x.id===(target.stemSetId||target.stem_set_id));if(set){for(const detail of $('aw-stem-versions').children)if(detail.textContent.includes(T.stemName(set)))detail.open=true;}}}
  }

  async function enter(){
    visible=true;pane?.resume();window.ChartArcade?.close();if(enterPromise)return enterPromise;
    enterPromise=(async()=>{if(!defaults)defaults=await api('/defaults');await Promise.all([listProjects(),importSources(),modelCatalog()]);
      if(!s.project){const id=localStorage.getItem('startrail.advanced-project');if(id&&[...$('adv-projects').options].some(o=>o.value===id))await openProject(id);else changeMode('prepare');}
      renderProject();clearInterval(poll);poll=setInterval(()=>pollTasks(),2200);draw();})();
    try{return await enterPromise;}finally{enterPromise=null;}
  }
  function leave(){if(s.submittedBatch)s.submittedBatch.autoAllowed=false;visible=false;pauseAudio();window.ChartArcade?.close();pane?.suspend();clearInterval(poll);cancelAnimationFrame(raf);raf=0;apiDialog.close();}
  nav.onclick=()=>showView('advanced');window.AdvancedStudio={enter:()=>run(enter),leave,isVisible:()=>visible,openResult:target=>run(()=>openResult(target))};
  new ResizeObserver(()=>draw()).observe(view);window.addEventListener('appearancechange',draw);window.addEventListener('gameappearancechange',draw);
  window.addEventListener('keydown',e=>{if(!visible||window.ChartArcade?.isActive()||apiDialog.open)return;if(e.code==='Escape'){e.preventDefault();pauseAudio();return;}if(/INPUT|TEXTAREA|SELECT/.test(e.target.tagName))return;if(e.code==='ArrowLeft'||e.code==='ArrowRight'){e.preventDefault();seek(position()+(e.code==='ArrowLeft'?-1:1));}});
  window.addEventListener('blur',pauseAudio);document.addEventListener('visibilitychange',()=>{if(document.hidden)pauseAudio();});
})();
