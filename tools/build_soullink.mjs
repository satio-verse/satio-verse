// 把 web/vendor/soullink3d/ 下的 TypeScript 源码转成浏览器可以直接 import 的 ES 模块, 输出到 dist/.
// 用 Node 22.13+ 自带的 module.stripTypeScriptTypes (transform 模式, 支持参数属性等非纯类型语法), 不需要 npm.
//
//   node tools/build_soullink.mjs
//
// 只做两件事: 去掉类型, 给相对路径的 import 补上 .js. 'three' / '@pixiv/three-vrm' 等裸模块名
// 由页面的 importmap 解析到 web/vendor/ 下的本地副本.
import { stripTypeScriptTypes } from 'node:module';
import { mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../web/vendor/soullink3d/', import.meta.url));
const out = join(root, 'dist');
const source = JSON.parse(readFileSync(join(root, 'vendor', 'soullink', 'SOURCE.json'), 'utf8'));

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (path === out) return [];
    return statSync(path).isDirectory() ? walk(path) : path.endsWith('.ts') ? [path] : [];
  });
}

// from './x' / import './x' / import('./x')  ->  './x.js'
const RELATIVE = /((?:from|import)\s*\(?\s*['"])(\.{1,2}\/[^'"]+?)(['"])/g;

rmSync(out, { recursive: true, force: true });
let count = 0;
for (const file of walk(root)) {
  const rel = relative(root, file).split(sep).join('/');
  let js = stripTypeScriptTypes(readFileSync(file, 'utf8'), { mode: 'transform', sourceMap: false });
  js = js.replace(RELATIVE, (_, head, spec, tail) => `${head}${spec.endsWith('.js') ? spec : spec + '.js'}${tail}`);
  const header = `// 由 tools/build_soullink.mjs 从 ${rel} 生成, 不要手改. ` +
    `来源: SoullinkEmotion3D (Apache-2.0), 内含 soullink-emotion-sdk @ ${source.commit.slice(0, 8)}.\n`;
  const target = join(out, rel.replace(/\.ts$/, '.js'));
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, header + js);
  count++;
}
console.log(`已生成 ${count} 个模块 -> ${relative(process.cwd(), out)}`);
