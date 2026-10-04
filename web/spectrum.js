/* Cached time/frequency evidence. No playback, STFT or judgment mutation here. */
(function(root){
  'use strict';
  const DT=110/22050*1000,TILE=1024,BINS=192;
  function displayNow(snapshot){return Number(snapshot?.displayTimeMs??snapshot?.now??snapshot?.mediaTimeMs??0);}
  function timeToY(time,snapshot,projection){
    if(typeof projection.timeToY==='function')return projection.timeToY(time);
    return (projection.hitY??projection.hit??projection.geometry?.hit)-(time-displayNow(snapshot))/(projection.travelMs??projection.travel)*(projection.runwayPx??projection.runway??projection.geometry?.runway);
  }
  function visibleRange(snapshot,projection,offset=0){
    const now=displayNow(snapshot),travel=projection.travelMs??projection.travel;
    return [now+offset,now+travel+offset];
  }
  function playbackToSource(sample,mapping){
    const part=mapping.find(p=>p.output_start<=sample&&sample<p.output_end);
    return part?{sourceSample:part.source_start+sample-part.output_start,segmentId:part.segment_id}:null;
  }
  function chooseLevel(msPerPixel,manifest){
    const dt=manifest.dt_ms||DT,levels=manifest.levels||[1,2,4,8,16].map((stride,level)=>({stride,level}));
    let chosen=levels[0];for(const level of levels)if(level.stride*dt<=msPerPixel*1.2)chosen=level;
    return chosen;
  }
  function tileRows(manifest,index,stride){return Math.ceil(Math.min(manifest.tile_frames||TILE,manifest.total_frames-index*(manifest.tile_frames||TILE))/stride);}
  function rgba(buffer,rows,bins){
    const data=new Uint8Array(buffer),size=rows*bins,out=new Uint8ClampedArray(size*4);
    if(data.length!==2*size)throw new Error('频谱块长度无效');
    for(let frame=0;frame<rows;frame++)for(let f=0;f<bins;f++){
      const k=frame*bins+f,v=Math.max(data[k],data[size+k]*.88)/255,q=((rows-1-frame)*bins+f)*4;
      out[q]=Math.round(8+66*v*v);out[q+1]=Math.round(23+181*v);out[q+2]=Math.round(35+191*v);out[q+3]=255;
    }
    return out;
  }
  function workerMain(){
    const convert=rgba;
    self.onmessage=async e=>{
      const {id,buffer,rows,bins}=e.data;
      try{
        const data=convert(buffer,rows,bins),canvas=new OffscreenCanvas(bins,rows);
        canvas.getContext('2d').putImageData(new ImageData(data,bins,rows),0,0);
        const bitmap=canvas.transferToImageBitmap();self.postMessage({id,bitmap},[bitmap]);
      }catch(error){self.postMessage({id,error:error.message});}
    };
  }
  class TileDecoder{
    constructor(){
      this.pending=new Map();this.serial=0;this.worker=null;
      if(root.Worker&&root.OffscreenCanvas&&root.Blob&&root.URL){
        try{
          const code=`const rgba=${rgba.toString()};(${workerMain.toString()})();`,url=URL.createObjectURL(new Blob([code],{type:'text/javascript'}));
          this.worker=new Worker(url);URL.revokeObjectURL(url);
          this.worker.onmessage=e=>{const task=this.pending.get(e.data.id);if(!task){e.data.bitmap?.close();return;}this.pending.delete(e.data.id);e.data.error?task.reject(new Error(e.data.error)):task.resolve(e.data.bitmap);};
          this.worker.onerror=()=>{for(const task of this.pending.values())task.reject(new Error('频谱绘制线程不可用'));this.pending.clear();this.worker?.terminate();this.worker=null;};
        }catch(_){this.worker=null;}
      }
    }
    decode(buffer,rows,bins){
      if(this.worker){const id=++this.serial;return new Promise((resolve,reject)=>{this.pending.set(id,{resolve,reject});this.worker.postMessage({id,buffer,rows,bins},[buffer]);});}
      // Compatibility fallback converts only a downloaded tile, never per RAF.
      const canvas=document.createElement('canvas');canvas.width=bins;canvas.height=rows;
      canvas.getContext('2d').putImageData(new ImageData(rgba(buffer,rows,bins),bins,rows),0,0);
      return root.createImageBitmap?root.createImageBitmap(canvas):Promise.resolve(canvas);
    }
    close(){this.worker?.terminate();for(const task of this.pending.values())task.reject(new Error('频谱已关闭'));this.pending.clear();}
  }
  class Pane{
    constructor(canvas,options={}){
      this.canvas=canvas;this.options=options;this.decoder=new TileDecoder();this.cache=new Map();this.inflight=new Map();this.retry=new Map();
      this.generation=0;this.manifest=null;this.source=null;this.refreshAt=0;this.refreshing=false;this.bytes=0;this.closed=false;this.suspended=false;this.controller=new AbortController();
      this.status='未选择音频';this.anchors=[];this.highlights=[];this.lastFrame=null;this.retryTimer=0;this.budget=options.cacheBytes||32*1024*1024;
    }
    setSource(descriptor){
      this.controller.abort();clearTimeout(this.retryTimer);this.retryTimer=0;this.controller=new AbortController();this.generation++;this.closed=false;
      for(const entry of this.cache.values())entry.image.close?.();this.cache.clear();this.inflight.clear();this.retry.clear();this.bytes=0;
      this.source=descriptor||null;this.manifest=descriptor?.initialManifest||null;this.refreshAt=0;this.refreshing=false;this.lastFrame=null;
      this.status=descriptor?'频谱准备中':'未选择音频';this.anchors=[];this.highlights=[];return this;
    }
    setAnchors(anchors){this.anchors=Array.isArray(anchors)?anchors:[];return this;}
    setHighlights(events){this.highlights=Array.isArray(events)?events:[];return this;}
    _changed(){if(this.suspended||this.closed)return;this.options.onChange?.();if(this.lastFrame)this.render(...this.lastFrame);}
    _retryDraw(){
      if(this.retryTimer||this.closed||this.suspended)return;
      const generation=this.generation;this.retryTimer=setTimeout(()=>{this.retryTimer=0;if(generation===this.generation)this._changed();},450);
    }
    async _refresh(start,end){
      if(!this.source||this.refreshing||performance.now()<this.refreshAt)return;
      const generation=this.generation,descriptor=this.source,signal=this.controller.signal;
      this.refreshing=true;this.refreshAt=performance.now()+800;
      try{
        let manifest;
        if(descriptor.loadManifest)manifest=await descriptor.loadManifest(start,end,signal);
        else{
          const url=new URL(descriptor.manifestUrl,root.location?.href||'http://localhost/');url.searchParams.set('start_ms',Math.max(0,start));url.searchParams.set('end_ms',Math.max(start+1,end));
          const response=await fetch(url,{signal});if(!response.ok)throw new Error('频谱来源暂不可用');manifest=await response.json();
        }
        if(generation!==this.generation||this.closed)return;
        if(manifest.schema_version!==2||manifest.bins!==BINS||!manifest.analysis_id||!Number.isFinite(manifest.total_frames))throw new Error('频谱格式不兼容');
        if(this.manifest&&this.manifest.analysis_id!==manifest.analysis_id){for(const entry of this.cache.values())entry.image.close?.();this.cache.clear();this.bytes=0;}
        this.manifest=manifest;this.status=manifest.status==='failed'?'频谱分析失败':'频谱准备中';this._changed();
      }catch(error){if(generation===this.generation&&error.name!=='AbortError'){this.status=error.message;this._changed();}}
      finally{if(generation===this.generation)this.refreshing=false;}
    }
    _tileUrl(manifest,level,index){
      const value=this.source.tileUrl||this.options.tileUrl;
      return typeof value==='function'?value(manifest.analysis_id,level,index):String(value||'').replace('{analysis_id}',manifest.analysis_id).replace('{level}',level).replace('{index}',index);
    }
    async _request(level,index){
      const m=this.manifest,key=`${m.analysis_id}:${level.level}:${index}`,generation=this.generation;
      if(this.cache.has(key)||this.inflight.has(key)||this.inflight.size>=2||performance.now()<(this.retry.get(key)||0))return;
      this.inflight.set(key,true);const signal=this.controller.signal;
      try{
        const response=await fetch(this._tileUrl(m,level.level,index),{signal});
        if(response.status===202){if(generation===this.generation)this.retry.set(key,performance.now()+400);return;}
        if(!response.ok)throw new Error('频谱块暂不可用');
        const buffer=await response.arrayBuffer(),rows=tileRows(m,index,level.stride),image=await this.decoder.decode(buffer,rows,m.bins);
        if(generation!==this.generation||this.closed||m.analysis_id!==this.manifest?.analysis_id){image.close?.();return;}
        const bytes=rows*m.bins*4;this.cache.set(key,{image,rows,bytes,used:performance.now()});this.bytes+=bytes;this.retry.delete(key);
        while(this.bytes>this.budget&&this.cache.size>1){const oldest=[...this.cache].sort((a,b)=>a[1].used-b[1].used)[0];oldest[1].image.close?.();this.bytes-=oldest[1].bytes;this.cache.delete(oldest[0]);}
        this._changed();
      }catch(error){if(generation===this.generation&&error.name!=='AbortError'){this.retry.set(key,performance.now()+1500);this.status=error.message;}}
      finally{if(generation===this.generation){this.inflight.delete(key);}}
    }
    render(snapshot,projection){
      if(this.closed||this.suspended||!projection)return;
      this.lastFrame=[snapshot,projection];const c=this.canvas,w=c.clientWidth,h=c.clientHeight;if(!w||!h)return;
      const dpr=root.devicePixelRatio||1,cw=Math.round(w*dpr),ch=Math.round(h*dpr);if(c.width!==cw||c.height!==ch){c.width=cw;c.height=ch;}
      const ctx=c.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
      const header=projection.headerY??projection.header??projection.geometry?.header,hit=projection.hitY??projection.hit??projection.geometry?.hit,runway=projection.runwayPx??projection.runway??projection.geometry?.runway;
      ctx.fillStyle='#0a1c29';ctx.fillRect(0,0,w,h);
      const offset=Number(this.source?.sourceTimeOffsetMs)||0,[start,end]=visibleRange(snapshot,projection,offset);
      if(this.source)this._refresh(start,end);
      let missing=false,drawn=0;
      ctx.save();ctx.beginPath();ctx.rect(0,header,w,Math.max(0,hit-header));ctx.clip();
      const m=this.manifest;
      if(m){
        const level=chooseLevel((projection.travelMs??projection.travel)/(runway*dpr),m),span=m.tile_frames*m.dt_ms,total=Math.ceil(m.total_frames/m.tile_frames);
        const first=Math.max(0,Math.floor(start/span)),last=Math.min(total-1,Math.floor(end/span));
        for(let index=first;index<=last;index++){
          const key=`${m.analysis_id}:${level.level}:${index}`,entry=this.cache.get(key);
          if(!entry){missing=true;this._request(level,index);continue;}
          entry.used=performance.now();const left=index*m.tile_frames*m.dt_ms-m.dt_ms/2,right=left+entry.rows*level.stride*m.dt_ms;
          const top=timeToY(right-offset,snapshot,projection),bottom=timeToY(left-offset,snapshot,projection);
          ctx.imageSmoothingEnabled=true;ctx.drawImage(entry.image,0,0,m.bins,entry.rows,0,top,w,bottom-top);drawn++;
        }
        // A transparent mask prevents final partial-frame pooling drawing past EOF.
        if(end>m.duration_ms){const y=timeToY(m.duration_ms-offset,snapshot,projection);ctx.fillStyle='#0a1c29';ctx.fillRect(0,header,w,Math.max(0,y-header));}
      }
      for(const anchor of this.anchors){
        const t=Number(anchor.time_ms??anchor.start_ms);if(!Number.isFinite(t)||t<start||t>end)continue;
        const y=timeToY(t-offset,snapshot,projection);ctx.beginPath();ctx.setLineDash(anchor.kind==='onset'?[]:[4,5]);ctx.strokeStyle=anchor.kind==='onset'?'#79dfd57a':anchor.uncertain?'#dfc87840':'#dfc878a0';ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke();
      }
      // Selected notes mark a time row across all frequencies, never a pitch lane.
      for(const event of this.highlights){
        const t=Number(event.time_ms??event.start_ms);if(!Number.isFinite(t)||t<start||t>end)continue;
        const y=timeToY(t-offset,snapshot,projection);ctx.setLineDash([]);ctx.strokeStyle=event.color||'#ffffffb0';ctx.lineWidth=1.5;
        ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke();ctx.lineWidth=1;
      }
      ctx.setLineDash([]);ctx.restore();
      ctx.fillStyle='#efd076';ctx.fillRect(0,hit,w,1);ctx.fillStyle='#bbd2df';ctx.font='600 11px Bahnschrift,sans-serif';ctx.textBaseline='middle';ctx.textAlign='left';
      ctx.fillText(this.source?.label||'频谱 · 频率 →',9,14);
      ctx.font='10px Bahnschrift,sans-serif';ctx.fillStyle='#97b4c4';ctx.fillText('Hz',7,hit+18);
      if(m?.frequency_edges_hz){ctx.textAlign='center';for(const f of [100,1000,10000]){const edges=m.frequency_edges_hz;let bin=edges.findIndex(v=>v>=f)-1;bin=Math.max(0,Math.min(edges.length-2,bin));const x=(bin+(f-edges[bin])/(edges[bin+1]-edges[bin]))/(edges.length-1)*w;ctx.fillText(f>=1000?f/1000+'k':String(f),Math.max(23,Math.min(w-14,x)),hit+18);}}
      if(!drawn||missing){ctx.textAlign='center';ctx.fillStyle='#bad3df';ctx.font='11px Bahnschrift,sans-serif';ctx.fillText(this.status,w/2,Math.min(hit-12,header+24));if(this.source)this._retryDraw();}
      if(snapshot.sourcePosition===null&&this.source?.isAssembly){ctx.textAlign='center';ctx.fillText('准备时间',w/2,hit+39);}
    }
    suspend(){this.suspended=true;clearTimeout(this.retryTimer);this.retryTimer=0;return this;}
    resume(){this.suspended=false;return this;}
    dispose(){this.closed=true;this.generation++;clearTimeout(this.retryTimer);this.retryTimer=0;this.controller.abort();for(const entry of this.cache.values())entry.image.close?.();this.cache.clear();this.bytes=0;this.decoder.close();}
  }
  const api={create:(canvas,options)=>new Pane(canvas,options),displayNow,timeToY,visibleRange,playbackToSource,chooseLevel,tileRows,rgba};
  if(typeof module==='object'&&module.exports)module.exports=api;else root.SpectrumPane=api;
})(typeof window==='object'?window:globalThis);
