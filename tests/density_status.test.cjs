const test=require('node:test');const assert=require('node:assert/strict');
const {label,compact}=require('../web/density-status');
test('historical charts never imply density acceptance',()=>{
  assert.equal(label(undefined),'密度未评估');assert.equal(compact({status:'not_evaluated'}),'未评估');
});
test('completed sparse chart exposes its frozen target and actual density',()=>{
  assert.equal(label({status:'underfilled',target_nps:13,active_nps:5.9016}),'目标 13.0 · 实际 5.9 NPS · 未达标');
  assert.match(compact({status:'constraints',target_nps:13,active_nps:5.9}),/未达标/);
});
test('direct mapping displays min/max and measured chart-span NPS',()=>{
  const report={status:'in_range',target_range:{min:8,max:10},measured_nps:9.2};
  assert.match(label(report),/范围 8.0–10.0/);
  assert.match(label(report),/9.2 NPS · 达标/);
  assert.match(compact({...report,status:'out_of_range'}),/未达标/);
});
