/* Audition-only time and gain helpers. Generation PCM is never altered. */
(function(root){
  'use strict';
  function bounds(core, duration){
    if(!Array.isArray(core)||core.length!==2||!core.every(Number.isFinite)||core[0]<0||core[1]<=core[0]||core[1]>duration)throw new Error('试听范围无效');
    return core.slice();
  }
  function localTime(originalSeconds, offsetSeconds, duration){
    return Math.max(0,Math.min(duration,originalSeconds-offsetSeconds));
  }
  function originalTime(localSeconds,offsetSeconds){return localSeconds+offsetSeconds;}
  function safeRms(row){const rms=Number(row?.rms),peak=Number(row?.peak);return Number.isFinite(rms)&&rms>1e-7&&Number.isFinite(peak)&&peak>=0?rms*(peak>1?.98/peak:1):0;}
  function matchingVolumes(rows){
    const levels=rows.map(safeRms),positive=levels.filter(x=>x>0),target=positive.length?Math.min(...positive):0;
    return levels.map(x=>x>0&&target>0?Math.min(1,target/x):1);
  }
  function shouldRestart(loop,{paused,ended},endedEvent=false){return !!loop&&(endedEvent||ended||!paused);}
  function parameterError(field,raw){
    if(field.type==='boolean')return typeof raw==='boolean'?'':'请选择有效开关。';
    const value=typeof raw==='string'&&raw.trim()===''?NaN:Number(raw);
    if(!Number.isFinite(value)||value<field.min||value>field.max||(field.type==='integer'&&!Number.isInteger(value))||(field.choices&&!field.choices.includes(value)))return field.label+'超出允许范围。'+(field.help||'');
    return '';
  }
  const api={bounds,localTime,originalTime,safeRms,matchingVolumes,parameterError,shouldRestart};
  if(typeof module==='object'&&module.exports)module.exports=api;
  if(root)root.SeparationAudition=api;
})(typeof window==='object'?window:globalThis);
