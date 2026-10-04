// Meaningful arithmetic/date/CSV checks without Node packages or a browser dependency.
const assert = require('node:assert/strict');
const {stats,cohortKey,filterDays,shiftDate,csvCell} = require('../dashboard/app.js');
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
console.log('Dashboard UI: 9 checks passed');
