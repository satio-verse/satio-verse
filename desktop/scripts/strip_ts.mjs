// 把一个目录里的 TypeScript 原样转成浏览器能直接用的 ES 模块 JS (Live2D Cubism Framework 用, 见 prepare_live2d.py):
//   node strip_ts.mjs <源目录> <输出目录>
// 用 Node 22 自带的 module.stripTypeScriptTypes (transform 模式, 能处理 enum 和 namespace), 不用装 TypeScript. 逻辑不动, 另外做两件事:
//   - 相对导入补上 .js 扩展名 (浏览器的 ES 模块要完整路径);
//   - 去掉只导入类型的名字: 源码里 `import { ICubismClippingManager } from ...` 这类接口没写 `import type`,
//     去掉类型之后对方文件里已经没有这个导出, 浏览器会报"没有这个导出". 按对方文件真正的导出过滤一遍.
import { stripTypeScriptTypes } from 'node:module';
import { mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';

const [root, out] = process.argv.slice(2);
if (!root || !out) {
  console.error('用法: node strip_ts.mjs <源目录> <输出目录>');
  process.exit(2);
}
const files = [];
const walk = (d) => {
  for (const n of readdirSync(d)) {
    const p = join(d, n);
    if (statSync(p).isDirectory()) walk(p);
    else if (p.endsWith('.ts') && !p.endsWith('.d.ts')) files.push(p);
  }
};
walk(root);

// 第一遍: 去类型, 补扩展名
const code = new Map();  // 输出路径 -> JS
const fails = [];
for (const f of files) {
  try {
    let js = stripTypeScriptTypes(readFileSync(f, 'utf8'), { mode: 'transform', sourceMap: false });
    js = js.replace(/((?:from|import)\s*\(?\s*['"])(\.{1,2}\/[^'"]+?)(['"])/g, (m, a, p, b) => (p.endsWith('.js') ? m : `${a}${p}.js${b}`));
    code.set(resolve(join(out, relative(root, f)).replace(/\.ts$/, '.js')), js);
  } catch (e) {
    fails.push(`${relative(root, f)}: ${String(e.message).split('\n')[0]}`);
  }
}

// 每个文件真正导出的名字 (export * from 的递归展开)
const own = new Map(), stars = new Map();
for (const [file, js] of code) {
  const names = new Set(), from = [];
  for (const m of js.matchAll(/export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|class|const|let|var)\s+([A-Za-z0-9_$]+)/g)) names.add(m[1]);
  for (const m of js.matchAll(/export\s*\{([^}]*)\}/g)) {
    for (const part of m[1].split(',')) {
      const name = part.trim().split(/\s+as\s+/).pop().trim();
      if (name) names.add(name);
    }
  }
  if (/export\s+default\b/.test(js)) names.add('default');
  for (const m of js.matchAll(/export\s*\*\s*from\s*['"]([^'"]+)['"]/g)) from.push(resolve(dirname(file), m[1]));
  own.set(file, names);
  stars.set(file, from);
}
const exportsOf = (file, seen = new Set()) => {
  if (seen.has(file)) return new Set();
  seen.add(file);
  const all = new Set(own.get(file) || []);
  for (const f of stars.get(file) || []) for (const n of exportsOf(f, seen)) all.add(n);
  return all;
};

// 第二遍: import { ... } from / export { ... } from 里, 对方没有导出的名字去掉 (都是接口、类型别名)
let dropped = 0;
for (const [file, js] of code) {
  const fixed = js.replace(/(import|export)\s*\{([^}]*)\}\s*from\s*(['"])(\.{1,2}\/[^'"]+)\3\s*;?/g, (m, kw, list, q, rel) => {
    const target = resolve(dirname(file), rel);
    if (!code.has(target)) return m;
    const have = exportsOf(target);
    const keep = list.split(',').map((s) => s.trim()).filter(Boolean).filter((s) => {
      const imported = s.replace(/^type\s+/, '').split(/\s+as\s+/)[0].trim();
      const ok = have.has(imported) && !s.startsWith('type ');
      if (!ok) dropped++;
      return ok;
    });
    if (!keep.length) return kw === 'import' ? `import ${q}${rel}${q};` : '';  // 保留导入的副作用
    return `${kw} { ${keep.join(', ')} } from ${q}${rel}${q};`;
  });
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, fixed);
}
console.log(`转换了 ${files.length - fails.length}/${files.length} 个文件, 去掉了 ${dropped} 个只导入类型的名字`);
for (const f of fails) console.log(`失败 ${f}`);
process.exit(fails.length ? 1 : 0);
