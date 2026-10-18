const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('static/app.js', 'utf8');

function harness() {
  const nodes = new Map(), data = new Map();
  function element(id) {
    if (!nodes.has(id)) nodes.set(id, {value:'',innerHTML:'',textContent:'',hidden:false,dataset:{},
      addEventListener(){},querySelectorAll(){return []},querySelector(){return null},
      classList:{add(){},remove(){}},focus(){}});
    return nodes.get(id);
  }
  const context = vm.createContext({console, Uint8Array, Array, Date, JSON, Math, String,
    crypto:require('node:crypto').webcrypto,
    document:{getElementById:element,querySelectorAll(){return []},querySelector(){return element('query')}},
    window:{scrollTo(){}}, location:{reload(){}},
    localStorage:{getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,String(v)),removeItem:k=>data.delete(k)},
    fetch:()=>new Promise(()=>{}), setTimeout(){},clearTimeout(){},setInterval(){return 1},clearInterval(){}});
  vm.runInContext(source, context);
  return {context,data,element};
}

test('draft preserves exact text, mode and selected historical date',()=>{
  const {context:c,data,element:e}=harness();
  e('inRaw').value='  不是放弃\n仍然在做  ';e('inMode').value='/深度复盘';e('inDate').value='2026-10-06';c.saveDraft();
  e('inRaw').value='';e('inMode').value='';e('inDate').value='';c.restoreDraft();
  assert.equal(e('inRaw').value,'  不是放弃\n仍然在做  ');
  assert.equal(e('inMode').value,'/深度复盘');assert.equal(e('inDate').value,'2026-10-06');
});
test('recovery never deletes failed raw while another draft is present',()=>{
  const {context:c,data,element:e}=harness();data.set('sg_failed_raw','旧失败原文');data.set('sg_draft','新草稿');c.recoverSession();
  assert.equal(e('inRaw').value,'新草稿');assert.equal(data.get('sg_failed_raw'),'旧失败原文');
});
test('recovered raw is saved as draft before removing recovery slot',()=>{
  const {context:c,data,element:e}=harness();data.set('sg_failed_raw','可恢复原文');c.recoverSession();
  assert.equal(e('inRaw').value,'可恢复原文');assert.equal(data.get('sg_draft'),'可恢复原文');assert.ok(!data.has('sg_failed_raw'));
});
test('first-ever submission displays processing skeleton',()=>{
  const {context:c,element:e}=harness();c._pendingNew={t0:1};c.renderLog();
  assert.match(e('logList').innerHTML,/正在整理刚才提交的日志/);
  assert.doesNotMatch(e('logList').innerHTML,/还没有归档的日志/);
});
test('background refresh does not overwrite unsaved profile',()=>{
  const {context:c,element:e}=harness();e('profileEdit').value='我的编辑';c._profileDirty=true;c.renderProfile();
  assert.equal(e('profileEdit').value,'我的编辑');
});
test('background refresh does not destroy entry editor',()=>{
  const {context:c,element:e}=harness();e('logList').innerHTML='正在编辑';c._editingEntry=1;c.renderLog();
  assert.equal(e('logList').innerHTML,'正在编辑');
});
test('request identifiers are independent',()=>{
  const {context:c}=harness();assert.match(c.newSubmissionId(),/^[a-f0-9]{32}$/);assert.notEqual(c.newSubmissionId(),c.newSubmissionId());
});
