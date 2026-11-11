const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('static/app.js', 'utf8');

function mkEl() {
  return {value:'',innerHTML:'',textContent:'',hidden:false,dataset:{},style:{},
    addEventListener(){},querySelectorAll(){return []},querySelector(){return null},
    classList:{add(){},remove(){}},focus(){},
    setAttribute(){},select(){},setSelectionRange(){},appendChild(){},removeChild(){},remove(){}};
}
function harness(opts) {
  opts = opts || {};
  const nodes = new Map(), data = new Map();
  function element(id) {
    if (!nodes.has(id)) nodes.set(id, mkEl());
    return nodes.get(id);
  }
  const context = vm.createContext({console, Uint8Array, Array, Date, JSON, Math, String,
    crypto:require('node:crypto').webcrypto,
    navigator:opts.navigator,
    document:{getElementById:element,querySelectorAll(){return []},querySelector(){return element('query')},
      createElement:()=>mkEl(),body:{appendChild(){},removeChild(){}},
      getSelection:()=>({rangeCount:0,getRangeAt(){return null},removeAllRanges(){},addRange(){}}),
      execCommand:()=>true},
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
test('mdText strips markdown and preserves newlines',()=>{
  const {context:c}=harness();
  assert.equal(c.mdText('# 标题\n**加粗** 与 *斜体* 与 `代码`\n- 项目一\n• 项目二'),
    '标题\n加粗 与 斜体 与 代码\n· 项目一\n· 项目二');
});
test('copy-body button appears only when entry has body',()=>{
  const {context:c,element:e}=harness();
  c.state.entries=[
    {id:1,date:'2026-11-11',polished:'# 标题\n正文'},
    {id:2,date:'2026-11-10',polished:'   '},
    {id:3,date:'2026-11-09'}
  ];
  c.renderLog();
  assert.match(e('logList').innerHTML,/data-copy="1"/);
  assert.doesNotMatch(e('logList').innerHTML,/data-copy="2"/);
  assert.doesNotMatch(e('logList').innerHTML,/data-copy="3"/);
});
test('copyText uses Clipboard API when available',async()=>{
  let copied=null;
  const {context:c}=harness({navigator:{clipboard:{writeText:(t)=>{copied=t;return Promise.resolve();}}}});
  const ok=await c.copyText('# 标题\n正文');
  assert.equal(ok,true);assert.equal(copied,'# 标题\n正文');
});
test('copyText falls back to legacy copy when Clipboard API unavailable',async()=>{
  const {context:c}=harness({});
  const ok=await c.copyText('hello');
  assert.equal(ok,true);
});
