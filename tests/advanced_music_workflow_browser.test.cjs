const test=require('node:test');
const assert=require('node:assert/strict');

test('desktop music workflow waits for explicit processing and reuses evidence for scheme changes',{skip:!process.env.MALODY_BROWSER_URL},async()=>{
  const {chromium}=require('playwright');
  const browser=await chromium.launch({headless:true,channel:process.env.MALODY_BROWSER_CHANNEL||'msedge'});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}}),origin=process.env.MALODY_BROWSER_URL;
    const defaults=await (await page.request.get(origin+'/api/advanced/defaults')).json();
    defaults.capabilities={music_workflow:1};
    await page.route('**/api/advanced/defaults',route=>route.fulfill({json:defaults}));
    const pid='f'.repeat(32),errors=[],calls=[];
    page.on('pageerror',error=>errors.push(error.message));
    let holdAnalysis=false;
    let project={id:pid,revision:0,title:'Workflow test',artist:'Test',duration:2,samples:88200,sample_rate:44100,settings:defaults.settings,profile:'keyboard',variants:[{key:'balanced--hard',pattern:'balanced',difficulty:'hard'}],segments:[],assemblies:[],tempo:{bpm:120,points:[[0,120]],uncertain:true},waveform:[.2,.3,.1],tail_trim:{enabled:true,cutoff_sample:88200}};
    const wav=Buffer.alloc(44+88200*2);wav.write('RIFF');wav.writeUInt32LE(wav.length-8,4);wav.write('WAVEfmt ',8);wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(44100,24);wav.writeUInt32LE(88200,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(wav.length-44,40);
    await page.addInitScript(()=>localStorage.removeItem('startrail.advanced-project'));
    await page.route('**/api/advanced/projects**',async route=>{
      const req=route.request(),path=new URL(req.url()).pathname,body=req.postDataJSON();calls.push({path,method:req.method(),body});
      let data;
      if(path==='/api/advanced/projects')data={projects:[project]};
      else if(path.endsWith('/audio'))return route.fulfill({contentType:'audio/wav',body:wav});
      else if(path.endsWith('/music-analysis'))data={id:'a'.repeat(32),analysis_id:'a'.repeat(32),status:'queued'};
      else if(path.includes('/music-analysis/'))data=holdAnalysis?{status:'running',message:'正在分析'}:{status:'ready',source:{source_id:'original'},resolved_source:{source_id:'original'},evidence_id:'e'.repeat(64),timing_map_id:'t'.repeat(64)};
      else if(path.endsWith('/segmentation-preview'))data={id:'d'.repeat(32),project_revision:project.revision,arrangement_plan_id:'c'.repeat(64),segments:[{start_sample:0,end_sample:88200,included:true}],preserved_boundaries:[],regions:[{intensity:'dense',cut_reason:'测试音乐节奏变化'}]};
      else if(path.endsWith('/confirm-and-generate')){project={...project,revision:2,segments:[{id:'s'.repeat(32),name:'区域 1',start_sample:0,end_sample:88200,included:true,active:{},versions:{},overrides:{}}]};data={project,batch:{id:'b'.repeat(32),jobs:[]}};}
      else if(path.includes('/generation-batches/'))data={id:'b'.repeat(32),status:'completed',jobs:[],results:[],counts:{total:0}};
      else if(path.endsWith('/tasks'))data={tasks:[]};
      else if(path.endsWith('/separations'))data={stem_sets:[]};
      else if(path.endsWith('/separation-trials'))data={trials:[]};
      else if(path.endsWith('/section-plans'))data={plans:[]};
      else if(path.endsWith('/analysis'))return route.fulfill({status:503,json:{detail:'测试不加载频谱'}});
      else data=project;
      return route.fulfill({json:data});
    });
    await page.goto(origin);await page.locator('#nav-advanced').click();await page.locator('#adv-projects').selectOption(pid);
    await page.locator('#aw-analyze').waitFor({state:'visible'});
    await page.waitForFunction(()=>document.querySelector('#advanced-view').getAttribute('aria-busy')==='false');
    await page.locator('#aw-input-source').selectOption('vocals_accompaniment');
    await page.locator('#aw-target-difficulty').selectOption('expert');
    await page.waitForTimeout(600);
    assert.equal(calls.filter(x=>x.path.endsWith('/music-analysis')).length,0);
    assert.equal(calls.filter(x=>x.path.endsWith('/segmentation-preview')).length,0);
    assert.equal(await page.locator('#aw-apply-segmentation').isVisible(),false);
    await page.locator('#aw-input-source').selectOption('original');
    await page.locator('#aw-target-difficulty').selectOption('hard');
    holdAnalysis=true;
    await page.locator('#aw-analyze').click();
    await page.waitForFunction(()=>document.querySelector('#aw-analysis-status').textContent==='正在分析');
    assert.equal(await page.locator('#aw-analyze').isDisabled(),true);
    // Changing the requested stem while analysis is in flight must not enqueue
    // another processing task when that first task finishes.
    await page.locator('#aw-input-source').selectOption('vocals_accompaniment');
    holdAnalysis=false;
    await page.waitForFunction(()=>document.querySelector('#aw-analysis-status').textContent.includes('待处理'));
    assert.equal(calls.filter(x=>x.path.endsWith('/music-analysis')).length,1);
    assert.equal(await page.locator('#aw-apply-segmentation').isVisible(),false);
    await page.locator('#aw-input-source').selectOption('original');
    await page.locator('#aw-apply-segmentation').waitFor({state:'visible'});
    assert.equal(await page.locator('#aw-apply-segmentation').textContent(),'确认方案并生成');
    assert.equal(await page.locator('.aw-scheme-region .aw-intensity').inputValue(),'dense');
    assert.equal(calls.filter(x=>x.path.endsWith('/confirm-and-generate')||x.path.endsWith('/generation-batches')).length,0);
    assert.equal(await page.locator('#aw-generation-source').isVisible(),false);
    assert.equal(await page.locator('#aw-difficulties').isVisible(),false);
    assert.equal(await page.locator('#aw-target-difficulty').inputValue(),'hard');
    await page.locator('#aw-batch-settings').click();
    await page.locator('input[data-setting="bpm_bucket_count"]').fill('3');
    await page.locator('input[data-setting="bpm_bucket_range"]').fill('0.15');
    await page.locator('#aw-difficulties input[value="expert"]').check();
    await page.locator('#aw-settings-close').click();
    await page.waitForFunction(()=>document.querySelector('#aw-apply-segmentation')&&!document.querySelector('#aw-apply-segmentation').disabled);
    const latest=calls.filter(x=>x.path.endsWith('/segmentation-preview')).at(-1);
    assert.deepEqual(latest.body.difficulties,['hard','expert']);assert.equal(latest.body.source_id,'original');
    assert.equal(latest.body.settings.bpm_bucket_count,3);
    assert.equal(latest.body.settings.bpm_bucket_range,.15);
    await page.locator('#aw-region-granularity').selectOption('coarse');
    await page.waitForFunction(()=>!document.querySelector('#aw-apply-segmentation').disabled);
    const changed=calls.filter(x=>x.path.endsWith('/segmentation-preview')).at(-1);
    assert.equal(changed.body.settings.region_granularity,'coarse');
    assert.equal(changed.body.cuts,undefined);
    assert.equal(calls.filter(x=>x.path.endsWith('/music-analysis')).length,1);
    await page.locator('#aw-settings-open').click();assert.equal(await page.locator('#aw-engine').isVisible(),true);await page.locator('#aw-settings-close').click();
    await page.locator('#aw-apply-segmentation').click();
    await page.waitForFunction(()=>document.querySelector('#advanced-view').dataset.mode!=='prepare');
    assert.equal(calls.filter(x=>x.path.endsWith('/confirm-and-generate')).length,1);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
