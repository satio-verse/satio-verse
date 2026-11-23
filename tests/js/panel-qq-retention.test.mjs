import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { archiveChoice, archiveOptions, setArchiveChoice, setGroupArchive,
         retentionSummary, retentionDifference, retentionCard } from '../../desktop/web/panel-qq-retention.js';
import { defaultQQ, renderQQ, showStatus } from '../../desktop/web/panel-qq.js';

const copy = value => JSON.parse(JSON.stringify(value));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
class Element {
  constructor(attributes = '', body = '') {
    this.attributes = attributes; this.body = body; this.listeners = new Map(); this.isConnected = true;
    this.dataset = Object.fromEntries([...attributes.matchAll(/data-([a-z-]+)="([^"]*)"/g)]
      .map(([,k,v]) => [k.replace(/-([a-z])/g, (_, c) => c.toUpperCase()), v]));
    this.value = attributes.match(/value="([^"]*)"/)?.[1] ?? '';
    const option = [...body.matchAll(/<option\b([^>]*)>/g)].find(([,a]) => /\bselected\b/.test(a));
    if (option) this.value = option[1].match(/value="([^"]*)"/)?.[1] ?? '';
    this.checked = /\bchecked\b/.test(attributes); this.textContent = ''; this.hidden = false;
  }
  addEventListener(name, fn) { if (!this.listeners.has(name)) this.listeners.set(name, []); this.listeners.get(name).push(fn); }
  emit(name, target = this) { for (const fn of this.listeners.get(name) || []) fn({target}); }
  focus() {}
  querySelector() { return null; }
}
class Row extends Element {
  constructor(attributes, body) {
    super(attributes, body); this.fields = new Map();
    for (const match of body.matchAll(/<(select|button)\b([^>]*data-c="[^"]+"[^>]*)>([\s\S]*?)<\/\1>/g)) {
      const node = new Element(match[2], match[3]); this.fields.set(node.dataset.c, node);
    }
    // Input is a void element, so parse it separately rather than swallowing neighboring controls.
    for (const [, attrs] of body.matchAll(/<input\b([^>]*data-c="[^"]+"[^>]*)>/g)) {
      const node = new Element(attrs); this.fields.set(node.dataset.c, node);
    }
  }
  querySelector(selector) { return this.fields.get(selector.match(/data-c="([^"]+)"/)?.[1]) || null; }
}
class Main extends Element {
  set innerHTML(html) {
    this.html = html; this.nodes = new Map(); this.rows = []; this.sections = []; this.tabs = [];
    for (const [,tag,attrs] of html.matchAll(/<(\w+)\b([^>]*\bid="[^"]+"[^>]*)>/g)) {
      const id = attrs.match(/\bid="([^"]+)"/)[1];
      const body = tag === 'select' ? html.slice(html.indexOf(`<${tag}${attrs}>`) + tag.length + attrs.length + 2).split('</select>')[0] : '';
      this.nodes.set(id, new Element(attrs, body));
    }
    for (const [,attrs,body] of html.matchAll(/<tr\b([^>]*data-i="[^"]+"[^>]*)>([\s\S]*?)<\/tr>/g)) this.rows.push(new Row(attrs, body));
    for (const [,attrs] of html.matchAll(/<section\b([^>]*data-qq-section="[^"]+"[^>]*)>/g)) this.sections.push(new Element(attrs));
    for (const [,attrs] of html.matchAll(/<button\b([^>]*data-qq-tab="[^"]+"[^>]*)>/g)) this.tabs.push(new Element(attrs));
  }
  get innerHTML() { return this.html; }
  querySelector(selector) {
    if (selector.startsWith('#')) return this.nodes.get(selector.slice(1)) || null;
    if (selector === '.qq-convs tr:last-child input[data-c="id"]') return this.rows.at(-1)?.fields.get('id') || null;
    return null;
  }
  querySelectorAll(selector) {
    if (selector === '.qq-convs tr[data-i]') return this.rows;
    if (selector === '[data-qq-section]') return this.sections;
    if (selector === '[data-qq-tab]') return this.tabs;
    return [];
  }
}
const server = {state:'online', retention:{group_log_raw:false, api_log_raw:false, group_overrides:0}};
function fixture(t, qq = defaultQQ(), reported = server) {
  t.mock.method(globalThis, 'setInterval', () => 1); t.mock.method(globalThis, 'clearInterval', () => {});
  const main = new Main(); const snapshots = []; let changes = 0;
  const ctx = { S:{qq,personas:[],providers:{}}, esc,
    $:(selector,root=main)=>root.querySelector(selector), $$:(selector,root=main)=>root.querySelectorAll(selector),
    changed:()=>{changes++;}, save:async()=>{snapshots.push(copy(ctx.S));return true;}, setKey:()=>{},
    call:async()=>({ok:true,status:reported,api:{on:false}}), render:()=>renderQQ(main,ctx) };
  renderQQ(main,ctx);
  return {main,ctx,snapshots,get changes(){return changes;},
    field(id,value,event='change'){const node=main.querySelector('#'+id);assert.ok(node,`real rendered control ${id}`);node.value=value;node.emit(event);},
    row(index,field,value,event='change'){const row=main.rows[index],node=row.fields.get(field);assert.ok(node,`real rendered row field ${field}`);node.value=value;row.emit(event,node);} };
}

test('missing means inherit, false stays explicit, and malformed truthy values are not enabled', () => {
  assert.equal(archiveChoice(undefined),'inherit');assert.equal(archiveChoice(false),'off');assert.equal(archiveChoice(true),'on');
  for(const value of ['false','true',0,1,null,{},[]]) assert.equal(archiveChoice(value),'invalid');
  assert.match(archiveOptions('false'),/value="invalid" selected disabled/);
});

test('choices change only the selected field and preserve unrelated nested settings', () => {
  const q={group:{log_raw:true,look_gap_s:120,custom:{keep:true}},api_log_raw:true,convs:[{type:'group',id:101,log_raw:false,enabled:false}]};
  setGroupArchive(q,'off');assert.equal(q.group.log_raw,false);assert.equal(q.api_log_raw,true);
  setGroupArchive(q,'inherit');assert.deepEqual(q.group,{look_gap_s:120,custom:{keep:true}});
  setArchiveChoice(q.convs[0],'log_raw','inherit');assert.deepEqual(q.convs[0],{type:'group',id:101,enabled:false});
  setArchiveChoice(q,'api_log_raw','off');assert.equal(q.api_log_raw,false);
  const before=copy(q);assert.throws(()=>setGroupArchive(q,'false'),RangeError);assert.deepEqual(q,before);
  const empty={};setGroupArchive(empty,'inherit');assert.deepEqual(empty,{});
});

test('rendering a legacy configuration does not materialize archive defaults or alter overrides', async t => {
  const q={...defaultQQ(),convs:[{type:'group',id:101,name:'Synthetic',enabled:false,log_raw:false},{type:'private',id:202,name:'Friend'}]};
  const f=fixture(t,q);await showStatus(f.main,f.ctx);
  assert.ok(!Object.hasOwn(q,'group'));assert.ok(!Object.hasOwn(q,'api_log_raw'));
  assert.equal(q.convs[0].log_raw,false);assert.equal(q.convs[0].enabled,false);assert.ok(!Object.hasOwn(q.convs[1],'log_raw'));
  assert.match(f.main.innerHTML,/聊天记录/);assert.match(f.main.innerHTML,/qGroupLogRaw/);assert.match(f.main.innerHTML,/qApiLogRaw/);
  assert.equal(f.main.rows[0].fields.get('log_raw').value,'off');assert.ok(!f.main.rows[1].fields.has('log_raw'));
  assert.match(f.main.querySelector('#qRetentionStatus').textContent,/群聊默认不保存/);assert.equal(f.changes,0);
});

test('actual panel handlers preserve all archive choices while editing unrelated settings and saving', async t => {
  const q={...defaultQQ(),group:{log_raw:true,look_gap_s:91},api_log_raw:false,convs:[
    {type:'group',id:101,name:'A',log_raw:false,enabled:false,extra:'kept'},
    {type:'group',id:202,name:'B',log_raw:true},{type:'group',id:303,name:'C'}]};
  const f=fixture(t,q);
  f.field('qGroupSend','auto');f.row(0,'name','Edited','input');f.row(1,'send','draft');
  await f.ctx.save();const saved=f.snapshots[0].qq;
  assert.deepEqual(saved.group,{log_raw:true,look_gap_s:91});assert.equal(saved.api_log_raw,false);
  assert.equal(saved.convs[0].log_raw,false);assert.equal(saved.convs[0].enabled,false);assert.equal(saved.convs[0].extra,'kept');
  assert.equal(saved.convs[1].log_raw,true);assert.ok(!Object.hasOwn(saved.convs[2],'log_raw'));assert.equal(f.changes,3);
});

test('actual global, API, and per-group controls support on/off/inherit with literal booleans', async t => {
  const q={...defaultQQ(),convs:[{type:'group',id:101,name:'A',enabled:false}]};const f=fixture(t,q);
  f.field('qGroupLogRaw','on');f.field('qApiLogRaw','off');f.row(0,'log_raw','off');
  assert.equal(q.group.log_raw,true);assert.equal(q.api_log_raw,false);assert.equal(q.convs[0].log_raw,false);
  f.field('qGroupLogRaw','off');f.field('qApiLogRaw','on');f.row(0,'log_raw','on');
  assert.equal(q.group.log_raw,false);assert.equal(q.api_log_raw,true);assert.equal(q.convs[0].log_raw,true);
  f.field('qGroupLogRaw','inherit');f.field('qApiLogRaw','inherit');f.row(0,'log_raw','inherit');
  assert.ok(!Object.hasOwn(q.group,'log_raw'));assert.ok(!Object.hasOwn(q,'api_log_raw'));assert.ok(!Object.hasOwn(q.convs[0],'log_raw'));
  assert.equal(q.convs[0].enabled,false);assert.equal(f.changes,9);
});

test('adding and deleting other conversations never rebuilds surviving per-group policy', async t => {
  const q={...defaultQQ(),convs:[{type:'group',id:101,name:'A',log_raw:false},{type:'group',id:202,name:'B',log_raw:true}]};
  const f=fixture(t,q),survivor=q.convs[0];f.main.querySelector('#qAddGroup').emit('click');
  assert.equal(q.convs.length,3);assert.equal(q.convs[0],survivor);assert.equal(q.convs[1].log_raw,true);
  assert.ok(!Object.hasOwn(q.convs[2],'log_raw'));
  f.main.rows[1].fields.get('remove').emit('click');assert.equal(q.convs.length,2);assert.equal(q.convs[0],survivor);assert.equal(survivor.log_raw,false);
});

test('status does not claim effectiveness for absent/old/invalid server reports and flags a mismatch', async t => {
  for(const status of [null,{}, {retention:{}},{retention:{group_log_raw:'false',api_log_raw:false}}]) {
    assert.match(retentionSummary(status),/尚未收到/);assert.doesNotMatch(retentionSummary(status),/群聊默认不保存/);
  }
  const q={...defaultQQ(),group:{log_raw:true},api_log_raw:false};const f=fixture(t,q);
  await showStatus(f.main,f.ctx);assert.match(f.main.querySelector('#qRetentionDifference').textContent,/尚未保存或尚未生效/);
  assert.equal(q.group.log_raw,true,'status polling must not overwrite draft');
  assert.equal(retentionDifference({group:{log_raw:false}},server),'');
  assert.match(retentionSummary({retention:{group_log_raw:false,api_log_raw:true,group_overrides:2}}),/未提供逐群明细/);
});

test('real panel adopt/save-roundtrip preserves archive false and missing fields from a normalized reply', () => {
  const source=readFileSync(new URL('../../desktop/web/panel.js',import.meta.url),'utf8');
  const start=source.indexOf('function adopt(into, from) {'),end=source.indexOf('\nasync function flush()',start);
  assert.ok(start>=0 && end>start,'locate unchanged real settings adoption function');
  const context=vm.createContext({});vm.runInContext(source.slice(start,end)+';globalThis.realAdopt=adopt;',context);
  const settings={qq:{group:{log_raw:false},api_log_raw:true,convs:[{type:'group',id:101,log_raw:false},{type:'group',id:202}]}};
  const reply=copy(settings);reply.qq.group_send='auto';const original=settings.qq.convs[0];
  context.realAdopt(settings,reply);
  assert.equal(settings.qq.convs[0],original);assert.equal(original.log_raw,false);assert.equal(settings.qq.group.log_raw,false);
  assert.equal(settings.qq.api_log_raw,true);assert.ok(!Object.hasOwn(settings.qq.convs[1],'log_raw'));
});

test('archive wording distinguishes raw records, short context, existing data, and external logs', () => {
  const html=retentionCard({});
  for(const word of ['短期上下文','可信知识','不会自动开启','第三方框架自己的记录','不清理已有档案','服务配置的保留期'])assert.ok(html.includes(word));
});
