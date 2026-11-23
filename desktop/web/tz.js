// 她按哪个时区过日子 (服务端 wm/tz.py, 面板"桌宠与工作台"页里设; 2026-10-27 用户: "时间可以在设置里面预设，
// 如果不预设，就根据设备时间或者地理位置时间决定"). 页面的 Date 只认电脑的时区, 这里把"现在"挪成她那个时区的钟点:
// herDate() 返回的 Date 用 getHours / getMinutes / getDay 读出来就是她那边的几点 (别拿它的 toISOString / getTime 当真时间).
// 没设 (跟着电脑) 或连不上服务端: 就是电脑的钟.
let offsetMin = null;  // 她那边比 UTC 早多少分钟 (东八区 480); null = 跟着电脑

/** 从服务端拿一次 (启动时、之后每 5 分钟; 面板里改了最多 5 分钟跟上) */
export async function syncTz() {
  try {
    const d = await (await fetch('/api/clock', { cache: 'no-store' })).json();
    offsetMin = d?.tz && Number.isFinite(d.offset_min) ? d.offset_min : null;
  } catch { /* 服务端没开: 照旧 */ }
}

export function herDate(ms = Date.now()) {
  if (offsetMin === null) return new Date(ms);
  const device = -new Date(ms).getTimezoneOffset();
  return new Date(ms + (offsetMin - device) * 60000);
}

let timer = 0;
export function startTz() {
  if (timer) return;
  syncTz();
  timer = setInterval(syncTz, 5 * 60 * 1000);
}
