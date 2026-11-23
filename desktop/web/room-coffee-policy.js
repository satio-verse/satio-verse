// Coarse observed facts and elapsed time schedule another observation, never prove cooling.
// No exact volume, temperature, hidden cup state or automatic rewards.
export const COFFEE_EVIDENCE = Object.freeze({ version: 1, tasteMs: 30000, brewMs: 60000, levelMs: 1800000,
  retryMs: 300000, retryMaxMs: 1200000 });
const LEVELS = new Set(['empty', 'low', 'half', 'full']);
const TEMPERATURES = new Set(['cold', 'cool', 'warm', 'hot']);
const safeId = id => typeof id === 'string' && id.length > 0 && id.length <= 200 && !['__proto__', 'constructor', 'prototype'].includes(id);
const timeOK = t => Number.isFinite(t) && t >= 0;
const streak = n => Number.isInteger(n) && n >= 1 ? Math.min(3, n) : 1;
const delay = n => Math.min(COFFEE_EVIDENCE.retryMaxMs, COFFEE_EVIDENCE.retryMs * 2 ** (streak(n) - 1));

/** Only our versioned coarse evidence is restored; old liquid ledgers never generate memories. */
export function restoreCoffeeEvidence(saved, now = Date.now()) {
  const thermal = {}, levels = {};
  if (saved?.version !== COFFEE_EVIDENCE.version || !timeOK(now)) return { thermal, levels };
  const rollback = timeOK(saved.at) && saved.at > now + 1000;
  for (const [id, e] of Object.entries(saved.thermal || {}).slice(0, 256)) {
    if (!safeId(id) || !e || !TEMPERATURES.has(e.temperature) || typeof e.inferred !== 'boolean') continue;
    const hot = !e.inferred && e.temperature === 'hot';
    if (!timeOK(e.at) || e.at > now + 1000 || rollback) {
      if (hot) thermal[id] = { at: now, temperature: 'hot', inferred: false, uncertain: true,
        hotStreak: streak(e.hotStreak), attemptAt: now, retryAt: now + delay(e.hotStreak) };
      continue; // A future warm/cool observation cannot authorize drinking.
    }
    const clean = { at: e.at, temperature: e.temperature, inferred: e.inferred };
    if (hot) {
      clean.hotStreak = streak(e.hotStreak);
      clean.attemptAt = timeOK(e.attemptAt) && e.attemptAt <= e.at && e.at - e.attemptAt < COFFEE_EVIDENCE.tasteMs ? e.attemptAt : e.at;
      clean.retryAt = clean.attemptAt + delay(clean.hotStreak); // Never trust a saved shortened deadline.
      if (e.uncertain === true) clean.uncertain = true;
    }
    thermal[id] = clean;
  }
  for (const [id, e] of Object.entries(saved.levels || {}).slice(0, 256)) {
    if (safeId(id) && e && LEVELS.has(e.level) && timeOK(e.at) && e.at <= now && !rollback && now - e.at <= COFFEE_EVIDENCE.levelMs)
      levels[id] = { at: e.at, level: e.level };
  }
  return { thermal, levels };
}

/** One continuous hot sip is one attempt, even when physics emits many drink events. */
export function tastedCoffee(previous, category, now = Date.now()) {
  const out = { at: now, temperature: category, inferred: false };
  if (category !== 'hot') return out;
  const priorHot = previous?.temperature === 'hot' && previous.inferred === false;
  const same = priorHot && !previous.uncertain && now >= previous.at && now - previous.at < COFFEE_EVIDENCE.tasteMs;
  out.hotStreak = same ? streak(previous.hotStreak) : priorHot ? Math.min(3, streak(previous.hotStreak) + 1) : 1;
  out.attemptAt = same && timeOK(previous.attemptAt) ? previous.attemptAt : now;
  out.retryAt = out.attemptAt + delay(out.hotStreak);
  return out;
}

export function coffeeThermalView(e, now = Date.now()) {
  const pastHot = e?.temperature === 'hot' && e.inferred === false;
  const known = !!e && !e.uncertain && now >= e.at && now - e.at < (e.inferred ? COFFEE_EVIDENCE.brewMs : COFFEE_EVIDENCE.tasteMs);
  const category = known ? e.temperature : 'unknown';
  const retry = { state: pastHot ? (now < e.retryAt ? 'wait' : 'due') : 'none' };
  const text = known ? e.inferred ? '刚冲好的咖啡，可能烫'
    : { cold: '刚才喝到的是冷的', cool: '刚才喝起来有点凉', warm: '刚才喝起来温温的', hot: '刚才入口时有点烫' }[category]
    : pastHot ? retry.state === 'wait' ? '记得上次入口还烫，先等一会；现在冷热还不确定'
      : '记得上次入口烫，已经等过一阵；仍需重新小口试温，不能认定已经凉了'
    : '只看杯子还不能确定冷热';
  return { known, category, temperature: category, inferred: known && !!e.inferred, pastHot, retry, text, reason: text };
}

export function coffeeDecision(observation, { beans = 0 } = {}) {
  const o = observation || {};
  const waiting = o.retry?.state === 'wait' || (o.known && !o.inferred && o.category === 'hot');
  if (!o.visible || !['empty', 'low', 'half', 'full'].includes(o.level))
    return waiting ? { action: 'wait', reason: '记得上次入口还烫，先等一会再走近复查；不反复热试' }
      : { action: 'observe', autoApproach: !o.visible && ['low', 'half', 'full'].includes(o.remembered?.level),
        reason: '还没看清杯里的液面，先走近看看；不直接加液' };
  if (o.level === 'empty') return beans > 0
    ? { action: 'brew', reason: '看见杯子是空的，可以准备新咖啡' }
    : { action: 'refuse', reason: '杯子是空的，咖啡豆也用完了；这次没有饮品可喝' };
  if (waiting)
    return { action: 'wait', reason: '刚才实际尝到还烫，先留着等一会；不重复冲泡，也不继续硬喝' };
  return { action: 'reuse', reason: o.level === 'low'
    ? '杯里还剩一点，先试着用已有的；够不到就保留剩余，不强行倾杯'
    : '杯里还有饮品，先用已有的，不重新冲泡' };
}
