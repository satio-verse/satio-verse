import { test } from 'node:test';
import assert from 'node:assert/strict';
import { restorePlain, snapshotPlain } from '../../web/plain-state.js';

// 像 three.js 的 Vector3 一样的: 有 isVector3、clone、copy
class V3 {
  constructor(x = 0, y = 0, z = 0) { this.isVector3 = true; this.x = x; this.y = y; this.z = z; }
  clone() { return new V3(this.x, this.y, this.z); }
  copy(v) { this.x = v.x; this.y = v.y; this.z = v.z; return this; }
}
class Mesh { constructor() { this.position = new V3(); } }

test('出厂状态: 拍过的值放回去, 后来加的纯数据字段删掉, 引用不动', () => {
  const mesh = new Mesh(), fn = () => 1;
  const o = { n: 1, s: 'a', arr: [1, 2], obj: { a: 1, b: { c: 2 } }, v: new V3(1, 2, 3), f32: new Float32Array([1, 2]),
    items: { book: { held: null, mesh } }, onX: fn, inst: new Mesh(), big: new Array(5000).fill(0) };
  const snap = snapshotPlain(o, ['skipMe']);
  const v = o.v, f32 = o.f32, inst = o.inst;
  Object.assign(o, { n: 9, s: 'b', skipMe: { keep: true }, later: { x: 1 }, cache: [1, 2, 3] });
  o.arr.push(3);
  o.obj.a = 5; o.obj.b.c = 7; o.obj.extra = 1;
  o.v.x = 99; o.f32[0] = 9;
  o.items.book.held = 'right'; o.items.book.size = 0.2;
  o.inst = new Mesh();  // 顶层的类实例: 不拍也不动
  o.big[0] = 1;         // 很长的数组当成不变的数据: 不拍也不动
  restorePlain(o, snap, ['skipMe']);
  assert.equal(o.n, 1); assert.equal(o.s, 'a');
  assert.deepEqual(o.arr, [1, 2]);
  assert.deepEqual(o.obj, { a: 1, b: { c: 2 } }, '嵌套的普通对象放回, 后来加的键删掉');
  assert.equal(o.v, v, 'Vector3 原地放回 (外面拿着的引用照样有效)'); assert.equal(o.v.x, 1);
  assert.equal(o.f32, f32); assert.equal(o.f32[0], 1);
  assert.equal(o.items.book.held, null); assert.equal(o.items.book.size, undefined);
  assert.equal(o.items.book.mesh, mesh, '夹在纯数据里的引用照原样放回');
  assert.equal(o.onX, fn);
  assert.notEqual(o.inst, inst, '顶层的类实例不动');
  assert.equal(o.big[0], 1, '很长的数组不动');
  assert.equal(o.later, undefined); assert.equal(o.cache, undefined);
  assert.deepEqual(o.skipMe, { keep: true }, 'skip 的键不动');
});

test('出厂状态: 换掉的对象放回原来那个, 不往此刻指着的对象里写; 两处指着同一个的还是同一个', () => {
  const shared = { dur: 1, tag: 'idle' }, other = { dur: 5, tag: 'wave' };
  const o = { clip: shared, groups: { head: { clip: shared } }, v: new V3(1, 0, 0) };
  const snap = snapshotPlain(o);
  o.clip = other; o.groups.head.clip = other;  // 换成别的动作 (共用的定义)
  o.v = new V3(5, 5, 5);
  restorePlain(o, snap);
  assert.equal(o.clip, shared); assert.equal(o.groups.head.clip, shared);
  assert.deepEqual(other, { dur: 5, tag: 'wave' }, '此刻指着的那个没被改');
  assert.equal(o.v.x, 1);
  // 放回以后接着跑、再放回, 一样
  o.clip.dur = 3; o.groups.head.clip.tag = 'x';
  restorePlain(o, snap);
  assert.deepEqual(shared, { dur: 1, tag: 'idle' });
});
