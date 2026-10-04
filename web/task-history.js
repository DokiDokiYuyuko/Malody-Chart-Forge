(function (root) {
  'use strict';

  const pageSize = 10;
  const typeLabels = {advanced:'高级制谱',song:'单曲制谱',separation:'音频分离',separation_trial:'局部分离试听'};
  const stateLabels = {completed:'已完成',partial:'部分完成',failed:'生成失败',cancelled:'已取消',interrupted:'已中断',historical:'历史结果',queued:'等待生成',running:'生成中',paused:'已暂停',needs_source:'需重新导入'};
  const count = value => Math.max(0, Number(value) || 0);
  const recordIdentity = item => String(item.record_id || item.id || '');
  const historyQuery = state => { const params = new URLSearchParams({page:String(state.page),page_size:String(pageSize),q:state.query,type:state.type}); if (state.projectId) params.set('project_id',state.projectId); return params.toString(); };
  const contextKey = state => [state.page, state.query, state.type, state.projectId].join('\u0000');
  function summary(item) {
    const success = count(item.success_count ?? item.counts?.completed);
    const failed = count(item.failed_count ?? item.counts?.failed);
    const pieces = [];
    if (item.type === 'advanced') {
      if (item.section_count != null) pieces.push(`${count(item.section_count)} 段`);
      if (item.combination_count != null) pieces.push(`${count(item.combination_count)} 个组合`);
      pieces.push(`成功 ${success} · 失败 ${failed}`);
    } else {
      pieces.push(stateLabels[item.status] || item.status || '历史记录');
    }
    if (item.partial_count) pieces.push(`部分完成 ${count(item.partial_count)}`);
    if (item.counts?.cancelled) pieces.push(`已取消 ${count(item.counts.cancelled)}`);
    if (item.counts?.interrupted) pieces.push(`已中断 ${count(item.counts.interrupted)}`);
    return pieces.join(' · ');
  }
  function inputLabel(item) {
    if (item.source_label) return item.source_label;
    return (item.input_sources || []).map(source => typeof source === 'string' ? source : source.label || source.name || source.kind || '').filter(Boolean).join('、') || '来源未记录';
  }
  function resultDestination(item) {
    const target = item.result_target || {};
    const projectId = target.project_id || item.project_id;
    const batchId = target.batch_id || item.batch_id;
    const jobId = target.job_id || item.job_id;
    if (item.type === 'song' || target.type === 'job') return jobId ? {type:'job',jobId} : null;
    if (!projectId) return null;
    if (item.type === 'separation' || item.type === 'separation_trial' || target.task_type === 'separation') {
      return {type:'advanced',projectId,mode:'prepare',jobId,stemSetId:target.stem_set_id || item.stem_set_id || item.jobs?.find(job => job.stem_set_id)?.stem_set_id};
    }
    return batchId || jobId ? {type:'advanced',projectId,batchId,jobId} : null;
  }
  function folderIdentity(item, job) {
    const recordId = recordIdentity(item), jobId = job?.job_id || job?.id;
    return jobId ? {record_id:recordId,job_id:jobId} : {record_id:recordId};
  }
  function formatDate(value) {
    const date = new Date(value);
    return value && Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',year:'numeric',hour12:false}) : '提交时间未记录';
  }

  function createController(options) {
    const doc = options.document, get = id => doc.getElementById(id);
    const schedule = options.setTimeout || setTimeout, cancel = options.clearTimeout || clearTimeout;
    const state = {visible:false,tab:'running',page:1,pages:1,query:'',type:'',projectId:'',request:0,timer:null};
    const cards = new Map();
    let renderedContext = '', initialized = false;
    let errorKind = '';
    function error(message, kind = 'action') {
      const host = get('task-history-error');
      errorKind = message ? kind : ''; host.textContent = message || ''; host.hidden = !message;
    }
    function element(tag, className, text) {
      const node = doc.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    }
    function button(text, className, handler) {
      const node = element('button', className, text); node.type = 'button'; node.onclick = handler; return node;
    }
    async function openFolder(item, job, control) {
      control.disabled = true; error('');
      try {
        await options.request('/api/task-history/open-folder', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(folderIdentity(item, job))});
      } catch (failure) { if (state.visible) error(`无法打开文件夹：${failure.message}`); }
      finally { control.disabled = false; }
    }
    async function openResult(item, control) {
      const destination = resultDestination(item); if (!destination) return;
      control.disabled = true; error('');
      try {
        if (destination.type === 'job') await options.openJob(destination.jobId);
        else {
          if (!options.advanced()?.openResult) throw new Error('结果入口尚未就绪，请刷新页面后重试。');
          const {type, ...selection} = destination;
          await options.advanced().openResult(selection);
        }
      } catch (failure) {
        const message = `无法查看结果：${failure.message}`;
        if (state.visible) error(message); else options.resultError?.(message);
      }
      finally { control.disabled = false; }
    }
    function renderJobs(host, item) {
      const jobs = item.jobs || [];
      host.replaceChildren(...jobs.map(job => {
        const row = element('div', 'task-history-job');
        const body = element('div', 'task-history-job-info');
        const title = job.segment_name || job.title || '生成任务';
        const variants = (job.variants || []).map(variant => typeof variant === 'string' ? variant : variant.label || variant.key || '').filter(Boolean).join('、');
        body.append(element('strong', '', title), element('span', '', [job.superseded ? '旧尝试（已被重试替代）' : '', stateLabels[job.status] || job.status, variants, job.source_label, job.message].filter(Boolean).join(' · ')));
        for (const failure of job.errors || []) {
          const label = typeof failure === 'string' ? failure : [failure.segment_name || failure.stage || failure.variant, failure.error || failure.message || failure.reason || failure.summary].filter(Boolean).join('：');
          if (label) body.append(element('p', 'task-history-failure', label));
        }
        if ((job.status === 'failed' || job.status === 'interrupted') && !job.errors?.length) body.append(element('p', 'task-history-failure', job.error || job.message || '未记录失败原因，可打开结果查看。'));
        const open = button('打开任务文件夹', 'task-history-folder', () => openFolder(item, job, open));
        open.disabled = !(job.job_id || job.id);
        row.append(body, open); return row;
      }));
      if (!jobs.length) host.append(element('p', 'task-history-muted', '这条历史记录没有可定位的任务详情。'));
    }
    function updateCard(entry, item) {
      const signature = JSON.stringify(item);
      if (entry.signature === signature) return;
      entry.signature = signature; entry.item = item;
      entry.card.dataset.status = item.status || 'historical';
      entry.title.textContent = item.title || '未命名歌曲'; entry.title.title = entry.title.textContent;
      entry.tag.textContent = typeLabels[item.type] || '历史结果';
      entry.date.textContent = formatDate(item.created_at || item.created);
      entry.meta.textContent = summary(item);
      entry.source.textContent = `输入：${inputLabel(item)}`;
      entry.status.textContent = stateLabels[item.status] || item.status || '历史记录';
      entry.open.disabled = !resultDestination(item);
      entry.open.title = entry.open.disabled ? '历史记录缺少可靠的结果关联' : '';
      entry.detailsSummary.textContent = `任务详情 · ${(item.jobs || []).length} 个任务`;
      renderJobs(entry.jobs, item);
    }
    function createCard(item) {
      const card = element('article', 'task-history-record'); card.dataset.recordId = recordIdentity(item);
      const heading = element('div', 'task-history-record-heading');
      const tag = element('span', 'task-history-kind'), title = element('h2'), status = element('span', 'task-history-state');
      heading.append(tag, title, status);
      const information = element('div', 'task-history-information');
      const date = element('time'), meta = element('p', 'task-history-summary'), source = element('p', 'task-history-source');
      information.append(date, meta, source);
      const actions = element('div', 'task-history-actions');
      const entry = {card,title,tag,status,date,meta,source,item};
      const open = button('查看结果', 'task-history-open', () => openResult(entry.item, open));
      const folder = button('打开所在文件夹', 'task-history-folder', () => openFolder(entry.item, null, folder));
      actions.append(open, folder);
      const details = element('details', 'task-history-details'), detailsSummary = element('summary'), jobs = element('div', 'task-history-jobs');
      details.append(detailsSummary, jobs); card.append(heading, information, actions, details);
      Object.assign(entry, {open,details,detailsSummary,jobs}); updateCard(entry, item); return entry;
    }
    function render(data, key) {
      const list = get('task-history-items'), items = data.items || [];
      if (renderedContext !== key) { list.replaceChildren(); cards.clear(); renderedContext = key; if (list.parentElement) list.parentElement.scrollTop = 0; }
      const identities = new Set(items.map(recordIdentity));
      for (const [id, entry] of cards) if (!identities.has(id)) { entry.card.remove(); cards.delete(id); }
      items.forEach((item, index) => {
        const id = recordIdentity(item); let entry = cards.get(id);
        if (!entry) { entry = createCard(item); cards.set(id, entry); } else updateCard(entry, item);
        if (list.children[index] !== entry.card) list.insertBefore(entry.card, list.children[index] || null);
      });
      state.pages = Math.max(1, count(data.pages ?? data.total_pages) || Math.ceil(count(data.total) / pageSize));
      get('task-history-empty').hidden = items.length > 0;
      get('task-history-empty').textContent = state.query || state.type ? '没有符合条件的完成记录。' : '还没有完成记录。任务结束后，可在这里查看结果。';
      get('task-history-summary').textContent = `${state.projectId ? '当前项目 · ' : ''}共 ${count(data.total)} 条记录 · 每页 ${pageSize} 条`;
      get('task-history-page').textContent = `${state.page} / ${state.pages}`;
      get('task-history-prev').disabled = state.page <= 1;
      get('task-history-next').disabled = state.page >= state.pages;
    }
    async function refresh() {
      cancel(state.timer); state.timer = null;
      if (!state.visible || state.tab !== 'completed') return;
      const ticket = ++state.request, key = contextKey(state), isCurrent = () => state.visible && state.tab === 'completed' && ticket === state.request && key === contextKey(state);
      const host = get('task-history-items'); host.setAttribute('aria-busy', 'true');
      if (renderedContext !== key) {
        host.replaceChildren(); cards.clear(); get('task-history-empty').hidden = true;
        get('task-history-summary').textContent = '正在读取完成记录…';
        get('task-history-prev').disabled = true; get('task-history-next').disabled = true;
      }
      try {
        const data = await options.request('/api/task-history?' + historyQuery(state));
        if (!isCurrent()) return;
        const pages = Math.max(1, count(data.pages ?? data.total_pages) || Math.ceil(count(data.total) / pageSize));
        if (state.page > pages) { state.page = pages; return refresh(); }
        render(data, key); if (errorKind === 'fetch') error('');
      } catch (failure) { if (isCurrent()) error(`完成记录读取失败：${failure.message}`, 'fetch'); }
      finally {
        if (isCurrent()) { host.setAttribute('aria-busy', 'false'); state.timer = schedule(refresh, 5000); }
      }
    }
    function selectTab(tab) {
      state.tab = tab; ++state.request; cancel(state.timer); state.timer = null;
      for (const [name, suffix] of [['running','active'],['completed','history']]) {
        const active = tab === name, button = get('queue-tab-' + name);
        button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1;
        get('queue-' + suffix + '-panel').hidden = !active;
      }
      get('queue-running-controls').hidden = tab !== 'running';
      if (tab === 'completed') refresh();
    }
    function initialize() {
      if (initialized) return; initialized = true;
      for (const tab of ['running','completed']) {
        const button = get('queue-tab-' + tab); button.onclick = () => selectTab(tab);
        button.addEventListener('keydown', event => {
          if (['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) { event.preventDefault(); const next = event.key === 'Home' ? 'running' : event.key === 'End' ? 'completed' : state.tab === 'running' ? 'completed' : 'running'; selectTab(next); get('queue-tab-' + next).focus(); }
        });
      }
      get('task-history-search').onsubmit = event => { event.preventDefault(); state.query = get('task-history-query').value.trim(); state.page = 1; refresh(); };
      get('task-history-type').onchange = () => { state.type = get('task-history-type').value; state.page = 1; refresh(); };
      get('task-history-reset').onclick = () => { get('task-history-query').value = ''; get('task-history-type').value = ''; state.query = ''; state.type = ''; state.projectId = ''; state.page = 1; refresh(); };
      get('task-history-prev').onclick = () => { if (state.page > 1) { --state.page; refresh(); } };
      get('task-history-next').onclick = () => { if (state.page < state.pages) { ++state.page; refresh(); } };
    }
    return {
      enter() { initialize(); state.visible = true; selectTab(state.tab); },
      openCompleted(selection = {}) { initialize(); state.query = selection.query || ''; state.type = selection.type || ''; state.projectId = selection.projectId || selection.project_id || ''; state.page = 1; get('task-history-query').value = state.query; get('task-history-type').value = state.type; error(''); state.tab = 'completed'; if (!state.visible && options.openQueue) options.openQueue(); else { state.visible = true; selectTab('completed'); } },
      leave() { state.visible = false; ++state.request; cancel(state.timer); state.timer = null; },
      refresh, selectTab, state,
    };
  }
  const exported = {pageSize,historyQuery,summary,resultDestination,folderIdentity,createController};
  if (typeof module === 'object' && module.exports) { module.exports = exported; return; }
  root.TaskHistory = root.TaskHistoryUI = createController({document,request:(...args) => request(...args),openQueue:() => showView('queue'),openJob:id => watchJob(id),advanced:() => root.AdvancedStudio,resultError:message => { const notice = document.getElementById('adv-notice'); if (notice && root.AdvancedStudio?.isVisible()) { notice.textContent = message; notice.hidden = false; } }});
  const header = document.querySelector('.topbar'), view = document.getElementById('queue-view');
  const syncHeader = () => view.style.setProperty('--queue-topbar-height', `${header.getBoundingClientRect().height}px`);
  syncHeader(); if (root.ResizeObserver) new root.ResizeObserver(syncHeader).observe(header);
})(typeof window === 'object' ? window : globalThis);
