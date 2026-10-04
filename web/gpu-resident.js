(() => {
  const status=document.getElementById('gpu-resident-status');
  async function request(release=false){
    const button=document.getElementById(release?'gpu-resident-release':'gpu-resident-refresh');button.disabled=true;
    try{
      const response=await fetch('/api/gpu/resident'+(release?'/release':''),{method:release?'POST':'GET'});
      const data=await response.json();if(!response.ok)throw new Error(data.detail||'状态读取失败');
      status.textContent=data.alive?(data.engine==='v32'?'V32':'MuG')+' · '+(data.phase==='running'?'正在推理':data.loaded?'模型常驻，等待任务':'工作进程就绪，模型未加载')+' · 空闲 15 分钟释放':'当前没有驻留模型，显存已释放。';
    }catch(error){status.textContent=error.message;}finally{button.disabled=false;}
  }
  document.getElementById('gpu-resident-refresh').onclick=()=>request();
  document.getElementById('gpu-resident-release').onclick=()=>request(true);
})();
