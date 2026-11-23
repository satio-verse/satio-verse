// 各层的"出厂状态": 层刚建好 (还没跑过一帧) 时把自己的纯数据字段拍一份, 执行评估每个场景复位时整份放回去
// (web/motion-eval.js 的 reset). 以前复位只清了十几个字段 (kin._ang、loco 的几样), 后来加的字段都漏了:
// 同一页面里同一个场景连跑两次结果就不一样 (begin 那一刻行动层就有 80 多处不同: 惯性化的半衰期、运动匹配的查询和帧、
// 脚的锁定点、仪态 / 交叠 / 情绪体态的权重……), 评测台开几个标签页、调参闭环的 A/B 都带上了跟前面跑过什么有关的噪声.
//
// 纯数据: 数、字符串、布尔、null、数组、普通对象 (原型是 Object.prototype 或 null)、类型数组、Vector3 / Quaternion / Euler.
// 别的 (类的实例、Object3D、Map、函数, 很长的数组) 是"引用": 顶层的不拍也不动; 夹在纯数据里的, 放回时只把引用放回去.
// 放回时连引用一起放回: 这个字段出厂时指向哪个对象 (数组、Vector3……), 就让它重新指向那个对象, 再把那个对象的内容放回去;
// 不往此刻指着的对象里写 (此刻指着的可能是共用的, 比如程序手势层的 clip 指着某个动作的定义). 后来才加上的纯数据字段删掉
// (懒算的缓存会重新算). skip: 不拍也不动的键 (动作库、场景这种又大又不变的, 或者是外面给的开关).

const BIG = 4096;  // 比这长的数组、类型数组当成不变的数据 (动作库、网格), 当引用

function isPlainObject(v) {
  if (v === null || typeof v !== 'object') return false;
  const p = Object.getPrototypeOf(v);
  return p === Object.prototype || p === null;
}

/** prim 数字一类 | vec Vector3 / Quaternion / Euler | typed 类型数组 | array 数组 | object 普通对象 | ref 别的 (只记引用). */
function kindOf(v) {
  if (v === null || v === undefined) return 'prim';
  const t = typeof v;
  if (t === 'number' || t === 'string' || t === 'boolean') return 'prim';
  if (t !== 'object') return 'ref';
  if (v.isVector3 || v.isQuaternion || v.isEuler) return 'vec';
  if (ArrayBuffer.isView(v)) return v.length <= BIG ? 'typed' : 'ref';
  if (Array.isArray(v)) return v.length <= BIG ? 'array' : 'ref';
  return isPlainObject(v) ? 'object' : 'ref';
}

function take(v, seen) {
  const kind = kindOf(v);
  if (kind === 'prim') return { p: v };
  if (kind === 'ref') return { r: v };
  if (seen.has(v)) return seen.get(v);  // 同一个对象被两处指着: 放回后还是同一个
  const node = { ref: v };
  seen.set(v, node);
  if (kind === 'vec') node.val = v.clone();
  else if (kind === 'typed') node.val = v.slice();
  else if (kind === 'array') node.items = v.map((x) => take(x, seen));
  else { node.keys = {}; for (const k of Object.keys(v)) node.keys[k] = take(v[k], seen); }
  return node;
}

function put(node, done) {
  if ('p' in node) return node.p;
  if ('r' in node) return node.r;
  const v = node.ref;
  if (done.has(node)) return v;
  done.add(node);
  if (node.val) { if (ArrayBuffer.isView(v)) v.set(node.val); else v.copy(node.val); }
  else if (node.items) { const items = node.items.map((n) => put(n, done)); v.length = 0; v.push(...items); }
  else {
    for (const k of Object.keys(v)) if (!(k in node.keys) && kindOf(v[k]) !== 'ref') delete v[k];
    for (const [k, n] of Object.entries(node.keys)) v[k] = put(n, done);
  }
  return v;
}

/** 拍 obj 自己的纯数据字段 (顶层是引用的不拍). */
export function snapshotPlain(obj, skip = []) {
  const s = new Set(skip), seen = new Map(), out = {};
  for (const k of Object.keys(obj)) if (!s.has(k) && kindOf(obj[k]) !== 'ref') out[k] = take(obj[k], seen);
  return out;
}

/** 把 obj 放回 snapshotPlain 拍的样子 (skip 同拍的时候): 拍过的放回, 后来加上的纯数据字段删掉, 顶层是引用的不动. */
export function restorePlain(obj, snap, skip = []) {
  const s = new Set(skip), done = new Set();
  for (const k of Object.keys(obj)) if (!s.has(k) && !(k in snap) && kindOf(obj[k]) !== 'ref') delete obj[k];
  for (const [k, n] of Object.entries(snap)) obj[k] = put(n, done);
}
