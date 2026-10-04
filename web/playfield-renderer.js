(function (root) {
  'use strict';
  const colors=['#62d6d1','#83a7ff','#ffd166','#ef83bf'];
  const skins=['float','mechanical','minimal'];
  const diagnostics=Boolean(root.location&&new URLSearchParams(root.location.search).has('playfieldDiagnostics'));
  const timings=new WeakMap();
  function recordTiming(ctx,started){
    let t=timings.get(ctx);if(!t){t={costs:[],intervals:[],previous:0,count:0,output:null};timings.set(ctx,t);}
    const now=performance.now();t.costs.push(now-started);if(t.previous&&started-t.previous<100)t.intervals.push(started-t.previous);t.previous=started;
    if(t.costs.length>240)t.costs.shift();if(t.intervals.length>240)t.intervals.shift();
    if(++t.count%120)return;
    if(!t.output){t.output=document.createElement('span');t.output.className='playfield-diagnostics';Object.assign(t.output.style,{position:'absolute',left:'12px',top:'35px',font:'11px sans-serif',color:'#c0dbe8',background:'#071521cc',padding:'4px',pointerEvents:'none',zIndex:'5'});ctx.canvas.parentElement.append(t.output);}
    const sorted=[...t.costs].sort((a,b)=>a-b),p95=sorted[Math.floor((sorted.length-1)*.95)],interval=t.intervals.reduce((sum,n)=>sum+n,0)/(t.intervals.length||1);
    t.output.textContent=`绘制 P95 ${p95.toFixed(2)} ms · 帧间隔 ${interval.toFixed(2)} ms · ${t.costs.length} 样本`;
  }
  function geometry(w,h){return {header:28,hit:Math.max(29,h-64),lane:w/4,runway:Math.max(1,h-92)};}
  function speedValue(value){const n=Number(value);return Number.isFinite(n)?Math.max(1,Math.min(16,n)):1;}
  function prepareNotes(notes){
    const lanes=[[],[],[],[]];for(const note of notes)if(lanes[note.lane])lanes[note.lane].push(note);
    for(const lane of lanes){lane.sort((a,b)=>a.start-b.start);for(let i=0;i<lane.length;i++)lane[i].spacingMs=Math.min(i?lane[i].start-lane[i-1].start:Infinity,i+1<lane.length?lane[i+1].start-lane[i].start:Infinity);}
    return notes;
  }
  function createEffects(){
    const items=[];
    return {items,clear(){items.length=0;},emit(lane,kind,time){if(!Number.isInteger(lane)||lane<0||lane>3)return;items.push({lane,kind,time});if(items.length>32)items.splice(0,items.length-32);},live(time){for(let i=items.length-1;i>=0;i--)if(time-items[i].time>240)items.splice(i,1);return items;}};
  }
  function panel(ctx,x,y,w,h,skin){
    ctx.beginPath();
    if(skin==='mechanical'){const cut=Math.min(4,h/3);ctx.moveTo(x+cut,y);ctx.lineTo(x+w,y);ctx.lineTo(x+w,y+h-cut);ctx.lineTo(x+w-cut,y+h);ctx.lineTo(x,y+h);ctx.lineTo(x,y+cut);ctx.closePath();}
    else ctx.roundRect(x,y,w,h,Math.min(skin==='minimal'?2:4,h/2));
  }
  function noteHead(ctx,x,y,w,h,color,skin='float',holding=false){
    panel(ctx,x,y,w,h,skin);ctx.fillStyle=color+(skin==='float'?'d9':'ee');ctx.fill();
    if(skin!=='minimal'){
      panel(ctx,x+.5,y+.5,w-1,Math.max(1,h-1),skin);ctx.strokeStyle=color+'dd';ctx.lineWidth=1;ctx.stroke();
      if(h>5){ctx.fillStyle='#ffffff70';ctx.fillRect(x+4,y+1,w-8,1);}
      if(skin==='mechanical'&&w>30&&h>7){ctx.fillStyle='#0a203a66';ctx.fillRect(x+7,y+4,2,h-6);ctx.fillRect(x+w-9,y+4,2,h-6);}
    }
    if(holding){ctx.fillStyle='#ffffff90';ctx.fillRect(x+3,y+1,w-6,1);}
  }
  function receptors(ctx,w,h,{skin='float',pressed=new Set(),labels=['1','2','3','4']}={}){
    const {hit,lane}=geometry(w,h);ctx.fillStyle='#0b1b29';ctx.fillRect(0,hit,w,h-hit);
    ctx.fillStyle='#8fbedb20';ctx.fillRect(0,hit,w,1);
    for(let i=0;i<4;i++){
      const c=colors[i],down=pressed.has(i),nw=lane*.64,x=i*lane+(lane-nw)/2,y=hit+17+(down?1.5:0);
      ctx.fillStyle=c+(down?'ff':'90');ctx.fillRect(x,hit,nw,down?2:1);
      if(skin==='minimal'){
        ctx.fillStyle=c+(down?'24':'09');ctx.fillRect(x,y,nw,27);ctx.fillStyle=c+(down?'ff':'75');ctx.fillRect(x,y+26,nw,1);
        ctx.fillRect(x,y+8,1,11);ctx.fillRect(x+nw-1,y+8,1,11);
      }else{
        panel(ctx,x,y,nw,27,skin);ctx.fillStyle=skin==='float'?(down?c+'2d':'#b9e9f10b'):(down?c+'35':'#203343');ctx.fill();
        ctx.strokeStyle=c+(down?'c0':'45');ctx.lineWidth=1;ctx.stroke();
        ctx.fillStyle=c+(down?'ee':'88');ctx.fillRect(x+7,y+1,nw-14,1);
        if(skin==='mechanical'){ctx.fillRect(x+7,y+7,2,12);ctx.fillRect(x+nw-9,y+7,2,12);}
        else{ctx.fillStyle=c+'70';ctx.beginPath();ctx.arc(x+10,y+13.5,1.5,0,Math.PI*2);ctx.fill();}
      }
      if(down){ctx.fillStyle=c+'24';ctx.fillRect(i*lane+1,hit-25,lane-2,25);}
      ctx.fillStyle=down?'#f3fdff':'#c4dbe5';ctx.font='600 13px Bahnschrift, sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(labels[i],i*lane+lane/2,y+13.5);
    }
  }
  function shortEffects(ctx,w,h,events,time,level='standard',reduced=false){
    if(level==='off'||reduced)return;
    const {hit,lane}=geometry(w,h);
    for(const e of events){
      const age=Math.max(0,time-e.time),life=e.kind==='hit'?220:140,t=age/life;if(t>=1)continue;
      const c=colors[e.lane],x=(e.lane+.5)*lane;ctx.save();ctx.globalAlpha=(1-t)*.8;
      if(e.kind==='hit'){
        if(level==='standard'){const g=ctx.createLinearGradient(0,hit-65,0,hit);g.addColorStop(0,c+'00');g.addColorStop(1,c+'66');ctx.fillStyle=g;ctx.fillRect(x-lane*.22,hit-65,lane*.44,65);}
        ctx.strokeStyle=c;ctx.lineWidth=1.5;ctx.beginPath();ctx.ellipse(x,hit,lane*(.13+t*.18),2+t*8,0,0,Math.PI*2);ctx.stroke();
      }else if(e.kind==='tail'){ctx.strokeStyle=c;ctx.lineWidth=1.5;ctx.beginPath();ctx.ellipse(x,hit,lane*.23*(1-t),4*(1-t),0,0,Math.PI*2);ctx.stroke();}
      else if(e.kind==='miss'){ctx.fillStyle='#071321aa';ctx.fillRect(e.lane*lane,hit-12,lane,12);}
      else{ctx.fillStyle=c;ctx.fillRect(x-lane*.28,hit,lane*.56,1.5);}
      ctx.restore();
    }
  }
  function render(ctx,w,h,options={}){
    if(!w||!h)return;
    const started=diagnostics?performance.now():0;
    const {header,hit,lane,runway}=geometry(w,h),skin=skins.includes(options.skin)?options.skin:'float',speed=speedValue(options.speed),travel=5000/speed,now=options.now||0;
    const pressed=options.pressed||new Set(),reduced=Boolean(options.reduced),night=options.night;
    ctx.clearRect(0,0,w,h);ctx.fillStyle=night?'#071611':'#0b1d2b';ctx.fillRect(0,0,w,h);
    for(let i=0;i<4;i++){ctx.fillStyle=night?(i%2?'#102b22':'#0d241c'):(i%2?'#102839':'#0f2333');ctx.fillRect(i*lane,header,lane,hit-header);ctx.strokeStyle=i===2?'#9ec5dc40':'#9ec5dc18';ctx.lineWidth=i===2?1.5:1;ctx.beginPath();ctx.moveTo(i*lane,header);ctx.lineTo(i*lane,hit);ctx.stroke();}
    ctx.save();ctx.beginPath();ctx.rect(0,header,w,hit-header+1);ctx.clip();
    for(const n of options.notes||[]){
      if(n.head&&(n.end===null||n.tail))continue;
      const end=n.end??n.start;if(end<now-130||n.start>now+travel+80)continue;
      const headY=n.holding?hit:hit-(n.start-now)/travel*runway,tailY=hit-(end-now)/travel*runway,x=n.lane*lane+lane*.18,nw=lane*.64,c=colors[n.lane];
      if(!c)continue;
      if(n.end!==null&&!n.tail){
        const top=Math.max(header,tailY),bottom=Math.min(hit,headY);
        if(bottom>top){ctx.fillStyle=c+(n.headMissed?'13':n.holding?'50':'24');ctx.fillRect(x+1,top,nw-2,bottom-top);ctx.fillStyle=c+(n.holding?'cc':'70');ctx.fillRect(x+1,top,1,bottom-top);ctx.fillRect(x+nw-2,top,1,bottom-top);}
        if(tailY>=header&&tailY<=hit)noteHead(ctx,x,tailY-3,nw,6,c,skin,n.holding);
      }
      const spacing=n.spacingMs*runway/travel,nh=Math.max(3.5,Math.min(15,Number.isFinite(spacing)?spacing*.78:13));
      if(headY>=header-16&&headY<=hit+nh/2)noteHead(ctx,x,headY-nh/2,nw,nh,c,skin,n.holding);
    }
    ctx.restore();receptors(ctx,w,h,{skin,pressed,labels:options.labels});
    ctx.save();ctx.beginPath();ctx.rect(0,header,w,h-header);ctx.clip();shortEffects(ctx,w,h,options.events||[],options.effectTime||0,options.hitEffects||'standard',reduced);ctx.restore();
    ctx.fillStyle='#a6c0d2';ctx.font='600 11px Bahnschrift, sans-serif';ctx.textAlign='left';ctx.textBaseline='middle';ctx.fillText(options.caption||`4K 预览 · ${speed.toFixed(1)}×`,12,14);
    if(diagnostics)recordTiming(ctx,started);
  }
  const api={colors,skins,geometry,speedValue,prepareNotes,createEffects,noteHead,receptors,render};
  if(typeof module==='object'&&module.exports)module.exports=api;else root.PlayfieldRenderer=api;
})(typeof window==='object'?window:this);
