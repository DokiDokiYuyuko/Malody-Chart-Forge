(function(root){
  function label(report){
    if(!report||report.status==='not_evaluated')return '密度未评估';
    if(report.target_range)return `范围 ${Number(report.target_range.min).toFixed(1)}–${Number(report.target_range.max).toFixed(1)} · 跨度 ${Number(report.measured_nps).toFixed(1)} NPS · ${report.status==='in_range'?'达标':'未达标'}`;
    const target=Number(report.target_nps||0).toFixed(1),actual=Number(report.active_nps||0).toFixed(1);
    const status=report.status==='pass'?'达标':report.status==='overfilled'?'偏密':report.status==='infeasible'?'目标与限制冲突':'未达标';
    return `目标 ${target} · 实际 ${actual} NPS · ${status}`;
  }
  function compact(report){
    if(!report||report.status==='not_evaluated')return '未评估';
    if(report.target_range)return `${Number(report.target_range.min).toFixed(1)}–${Number(report.target_range.max).toFixed(1)} / ${Number(report.measured_nps).toFixed(1)} · ${report.status==='in_range'?'达标':'未达标'}`;
    const state=report.status==='pass'?'达标':report.status==='overfilled'?'偏密':'未达标';
    return `${Number(report.target_nps||0).toFixed(1)} / ${Number(report.active_nps||0).toFixed(1)} · ${state}`;
  }
  root.MalodyDensity={label,compact};
  if(typeof module==='object'&&module.exports)module.exports=root.MalodyDensity;
})(typeof window==='object'?window:globalThis);
