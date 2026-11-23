// 动作成长的评估探针 (docs/roadmap/motion-growth.md): tools/motion_eval.mjs --probe motion-growth-probe.js 载入以后,
// 评估的每个场景里每帧跑和小屋同一套检测 (web/motion-faults.js), 结果附在 runEval 的每条结果上:
//   results[i].growth = { faults: {种类: {n, peak, motion, env}}, excess: {种类: 平滑量}, records: [前 20 条失败的摘要], exposure }
// 洒出来的事件在场景里是局部的液体世界算的, 只有事后的 carry.spillTimes: 跑完以后按逐帧记下的超阈值时刻分类 (spillEvents).
// 不改 motion-eval.js: 包住 kin.begin / kin.end (每个场景开始、结束各调一次) 和 state.step (场景每推进一帧调一次).
import { Excess, FaultDetector, gripEvents, Sensor, spillEvents } from './motion-faults.js';
import { MotionCoverage } from './motion-growth-coverage.js';

export function install(state, me) {
  const vrm = state.vrm, loco = vrm.loco, kin = vrm.kin;
  if (kin.__growth) return null;
  kin.__growth = true;
  let cur = null, coverageMode = 'normal';
  const done = {};
  const begin = kin.begin.bind(kin), end = kin.end.bind(kin), step = state.step;
  kin.begin = (label) => {
    cur = { label, t: 0, det: new FaultDetector({ keepMarks: true }), ex: new Excess(), sensor: new Sensor(loco), recs: [],
      coverage: new MotionCoverage(coverageMode) };
    return begin(label);
  };
  kin.end = (result) => {
    if (cur) done[cur.label] = cur;
    cur = null;
    return end(result);
  };
  state.step = (seconds, dt) => {
    cur?.coverage.observe(loco);
    step(seconds, dt);
    if (!cur) return;
    cur.coverage.push(loco, seconds);
    cur.t += seconds;
    const sig = cur.sensor.read(cur.t, seconds, kin.painNow || {}, {});
    for (const r of cur.det.push(sig)) cur.recs.push(r);
    cur.ex.push(sig);
  };
  const run = me.runEval;
  const runEval = async (spec) => {
    for (const k of Object.keys(done)) delete done[k];
    // 强制片段等开发诊断须显式传 diagnostic; 未识别口径也原样保留, 验收端拒绝.
    coverageMode = spec?.coverageMode ?? 'normal';
    const res = await run(spec);
    for (const r of res.results) {
      const g = done[`${r.scenario}#${r.seed}`];
      if (!g) continue;
      const recs = g.recs.slice();
      for (const s of spillEvents(r.carry?.spillTimes, g.det.marks)) recs.push({ ...s, family: 'carry', label: '洒出来了' });
      // 滑、脱手: 刚体事件也在场景里面, 事后按 carry.phys.events 的时刻分类 (脱手传感器也看得到, 这里去掉重复的)
      for (const s of gripEvents(r.carry?.phys?.events, g.det.marks)) {
        if (s.kind === 'drop' && recs.some((x) => x.kind === 'drop')) continue;
        recs.push({ ...s, family: 'carry' });
      }
      const sum = FaultDetector.summarize(recs, g.ex.values);
      r.growth = { ...sum, exposure: { ...g.det.exposure }, coverage: g.coverage.result(),
        records: recs.slice(0, 20).map((x) => ({ kind: x.kind, cls: x.cls, cause: x.cause, t: x.t, peak: x.peak ?? x.value, sev: x.sev,
          act: x.ctx?.action ? `${x.ctx.action.kind}:${x.ctx.action.clip || ''}@${x.ctx.action.f ?? ''}` : x.ctx?.mode || null })) };
    }
    return res;
  };
  return { ...me, runEval };
}
