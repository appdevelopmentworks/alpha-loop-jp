// Meaningful arithmetic/date/CSV checks without Node packages or a browser dependency.
const assert = require('node:assert/strict');
const {stats,cohortKey,filterDays,shiftDate,csvCell,negativeMiss,signedPercent} = require('../dashboard/app.js');
const a={session:'2026-10-02',data_grade:'synthetic',strategy_id:'price_baseline',strategy_version:'1.1.0',threshold:.1,candidates:[{hit:true},{hit:null}]};
const b={...a,session:'2026-10-01',candidates:Array.from({length:9},()=>({hit:false}))};
assert.equal(stats([a,b]).hit_rate,.1);
assert.equal(stats([a,b]).unknown,1);
assert.equal(stats([]).hit_rate,null);
assert.equal(filterDays([a,b],cohortKey(a),'2026-10-02','2026-10-02').length,1);
assert.notEqual(cohortKey(a),cohortKey({...a,data_grade:'reconstructed'}));
assert.notEqual(cohortKey({...a,data_grade:'observed',prediction_eligible:true}),cohortKey({...a,data_grade:'observed',prediction_eligible:false}));
assert.equal(shiftDate('2026-10-02',-6),'2026-09-26');
assert.equal(csvCell('=HYPERLINK("url")'),'"\'=HYPERLINK(""url"")"');
assert.equal(csvCell(.1),'"0.1"');
const declines={...a,candidates:[{hit:false,close_return:-.03},{hit:false,close_return:-.09},
  {hit:false,close_return:0},{hit:false,close_return:.04},{hit:false,close_return:null},
  {hit:true,close_return:-.5},{hit:null,close_return:null}]};
assert.equal(stats([declines]).negative_misses,2);
assert.equal(stats([declines]).misses_close_unknown,1);
assert.equal(stats([declines]).worst_miss_close_return,-.09);
assert.equal(stats([declines]).hit_rate,1/6);
assert.equal(negativeMiss({hit:false,close_return:null}),false);
assert.equal(negativeMiss({hit:true,close_return:-.5}),false);
assert.equal(signedPercent(-.09),'-9.00%');
assert.equal(signedPercent(.03),'+3.00%');
assert.equal(signedPercent(null),'—');
console.log('Dashboard UI: 18 checks passed');
