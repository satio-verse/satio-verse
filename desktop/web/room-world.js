// 小窝可供性的真值后果 (环境, 见 docs/roadmap/room-afford.md): 做一件事以后世界怎么回应 —— 这本书好不好看、土湿着再浇会怎样、
// 天黑了关灯舒不舒服. 只给执行 (room-afford.js) 和评测台 (tools/bench_afford.mjs) 用; 她挑事情的那一层 (room-known.js) 和服务端都不读.
// 规则写在可供性表每条的 out 里, 这里按"这一次的真值"挑出那一项. 不 import three (node 能测).
import { NEEDS } from './room-known.js';

/** 书好不好看 (她的口味, 真值): 按节点名算一个 0-1 的数, 每次打开都一样. */
export const appeal = (id) => { let h = 2166136261; for (const c of id) h = Math.imul(h ^ c.charCodeAt(0), 16777619); return ((h >>> 0) % 1000) / 1000; };

/** 土里的水 -> 看得出来的粗档. */
export const soilLevel = (m) => (m < 0.3 ? 'dry' : m > 1 ? 'wet' : 'ok');

const needs = (o) => Object.fromEntries(NEEDS.filter((k) => typeof o?.[k] === 'number').map((k) => [k, o[k]]));

/**
 * a: 可供性表的一条; t: 这一次的真值 —— node 拿的哪一件, dark 天黑没黑, seat 坐在哪, lampOff 落地灯关着, again 1.5 小时内刚刷过,
 * level 浇之前的土, lampOn 按之前灯开着, what 擦的结果 (wiped / stain / gone / missed), lonely 想你了, found 新发现了几样.
 * 返回需求的变化 {fun: …}.
 */
export function outcomeOf(a, t = {}) {
  const o = a.out || {};
  switch (a.verb) {
    case 'read': {
      if (a.again) return needs(t.again ? o.again : o);
      const [lo, hi] = Array.isArray(o.fun) ? o.fun : [o.fun ?? 0, o.fun ?? 0];
      const g = { ...needs(o), fun: lo + (hi - lo) * appeal(t.node || '') };
      if (t.dark && t.seat === 'reading_chair' && t.lampOff) g.fun -= 4;  // 天黑了, 阅读椅旁边的落地灯没开: 看得眼睛累
      return g;
    }
    case 'water': return needs(o[t.level]);
    case 'switch': return needs(o[`${t.dark ? 'night' : 'day'}-${t.lampOn ? 'on' : 'off'}`]);
    case 'wipe': return needs(o[t.what]);
    case 'hug': return needs(o[t.lonely ? 'lonely' : 'fine']);
    case 'explore': return { fun: Math.min(o.max ?? 10, (o.fun ?? 0) + (o.per_new ?? 0) * (t.found || 0)), ...(o.energy ? { energy: o.energy } : {}) };
    default: return needs(o);
  }
}
