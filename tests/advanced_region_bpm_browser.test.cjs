const test=require('node:test');
const assert=require('node:assert/strict');

test('advanced region cards round each detected BPM and omit unavailable BPM',{skip:!process.env.MALODY_BROWSER_URL},async()=>{
  const {chromium}=require('playwright');
  const browser=await chromium.launch({headless:true,channel:process.env.MALODY_BROWSER_CHANNEL||'msedge'});
  try{
    const origin=process.env.MALODY_BROWSER_URL,page=await browser.newPage({viewport:{width:1440,height:1000}});
    const defaults=await (await page.request.get(origin+'/api/advanced/defaults')).json();
    const pid='f'.repeat(32),errors=[];
    const project={id:pid,revision:0,title:'Region BPM',artist:'Test',duration:10,samples:441000,sample_rate:44100,
      settings:defaults.settings,profile:'keyboard',variants:[{key:'balanced--expert',pattern:'balanced',difficulty:'expert'}],
      segments:[],assemblies:[],workflow:{},tempo:{bpm:120,points:[[0,120]],uncertain:true},waveform:[.2,.3,.1],
      tail_trim:{enabled:true,cutoff_sample:441000}};
    const bpms=[220.198,110.962,223.729,274.39,110.941,null];
    const draft={id:'d'.repeat(32),project_revision:0,arrangement_plan_id:'c'.repeat(64),preserved_boundaries:[],
      segments:bpms.map((_,i)=>({start_sample:i*73500,end_sample:(i+1)*73500,included:true})),
      regions:bpms.map((bpm,i)=>({start_sample:i*73500,end_sample:(i+1)*73500,detected_bpm:bpm,intensity:'normal',cut_reason:'Fixture'}))};
    page.on('pageerror',e=>errors.push(e.message));
    const wav=Buffer.alloc(44+441000*2);wav.write('RIFF');wav.writeUInt32LE(wav.length-8,4);wav.write('WAVEfmt ',8);
    wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(44100,24);
    wav.writeUInt32LE(88200,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(wav.length-44,40);
    await page.addInitScript(()=>localStorage.removeItem('startrail.advanced-project'));
    await page.route('**/api/advanced/projects**',route=>{
      const p=new URL(route.request().url()).pathname;
      let data;
      if(p==='/api/advanced/projects')data={projects:[project]};
      else if(p.endsWith('/audio'))return route.fulfill({contentType:'audio/wav',body:wav});
      else if(p.endsWith('/music-analysis')||p.includes('/music-analysis/'))data={id:'a'.repeat(32),analysis_id:'a'.repeat(32),status:'ready',source:{source_id:'original'},resolved_source:{source_id:'original'},evidence_id:'e'.repeat(64),timing_map_id:'t'.repeat(64)};
      else if(p.endsWith('/segmentation-preview'))data=draft;
      else if(p.endsWith('/tasks'))data={tasks:[]};
      else if(p.endsWith('/separations'))data={stem_sets:[]};
      else if(p.endsWith('/separation-trials'))data={trials:[]};
      else if(p.endsWith('/section-plans'))data={plans:[]};
      else if(p.endsWith('/analysis'))return route.fulfill({status:503,json:{detail:'Fixture has no spectrum'}});
      else if(route.request().method()!=='GET')return route.fulfill({status:409,json:{detail:'Production mutation blocked'}});
      else data=project;
      return route.fulfill({json:data});
    });
    await page.goto(origin);await page.locator('#nav-advanced').click();
    await page.locator('#adv-projects').selectOption(pid);
    await page.waitForFunction(()=>document.querySelector('#advanced-view').getAttribute('aria-busy')==='false');
    await page.locator('#aw-analyze').click();
    await page.waitForFunction(()=>document.querySelectorAll('.aw-scheme-region').length===6);
    assert.deepEqual(await page.locator('.aw-region-bpm').allTextContents(),['220 BPM','111 BPM','224 BPM','274 BPM','111 BPM']);
    assert.equal(await page.locator('.aw-scheme-region').nth(5).locator('.aw-region-bpm').count(),0);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
