(() => {
  const canvas=document.getElementById('simple-spectrum');
  const pane=window.SpectrumPane.create(canvas,{onUpdate:()=>window.drawChart?.()});
  let epoch=0;
  async function load(jobId){
    const current=++epoch;
    try{
      const response=await fetch('/api/jobs/'+jobId+'/analysis',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({start_ms:0,end_ms:5000})});
      if(!response.ok)throw new Error('频谱暂不可用');const manifest=await response.json();if(current!==epoch)return;
      const prefix='/api/jobs/'+jobId+'/analysis/';pane.setSource({initialManifest:manifest,manifestUrl:prefix+manifest.analysis_id,tileUrl:(aid,level,index)=>prefix+aid+'/tiles/'+level+'/'+index,label:'音乐频谱'});
    }catch(e){if(current===epoch)pane.status=e.message;}
  }
  function render(snapshot,geometry,travel){
    if(!canvas.clientWidth||!canvas.clientHeight)return;
    pane.render(snapshot,{...geometry,travelMs:travel,timeToY:t=>geometry.hit-(t-snapshot.displayTimeMs)/travel*geometry.runway});
  }
  window.SimpleSpectrum={load,render};
})();
