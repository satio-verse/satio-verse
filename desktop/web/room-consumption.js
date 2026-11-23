// Internal game-state settlement, never a perception API or medical model.
export const CONSUMPTION = Object.freeze({ waterPerMl: 0.12, coffeePerMl: 0.04, coffeeMax: 6, halfLife: 1800 });
const idOK = (s) => typeof s === 'string' && s.length > 0 && s.length <= 200 && !['__proto__', 'constructor', 'prototype'].includes(s);
const clone = (x) => JSON.parse(JSON.stringify(x));
export const freshLedger = () => ({ version: 1, streams: {}, preparations: {}, totalMl: 0, coffee: { amount: 0, at: 0 } });
export function coffeeAmount(ledger, now) {
  const c = ledger?.coffee;
  return c && Number.isFinite(c.amount) && Number.isFinite(c.at)
    ? Math.min(CONSUMPTION.coffeeMax, Math.max(0, c.amount)) * 2 ** (-Math.max(0, now - c.at) / CONSUMPTION.halfLife) : 0;
}
export function validReceipt(e) {
  return e?.version === 1 && idOK(e.sourceId) && Number.isSafeInteger(e.sequence) && e.sequence > 0 &&
    idOK(e.sceneId) && idOK(e.cup) && ['water', 'coffee', 'milk'].includes(e.kind) &&
    Number.isFinite(e.ml) && e.ml > 0 && e.ml <= 1000 && Number.isFinite(e.temp) && e.temp >= -20 && e.temp <= 150 &&
    Number.isFinite(e.at) && e.at >= 0;
}
export function settleLiquid(state, e, { enabled, now }) {
  if (!validReceipt(e)) return { ok: false, reason: 'invalid-receipt' };
  const ledger = state.consumption || freshLedger(), last = ledger.streams[e.sourceId] || 0;
  if (e.sequence <= last) return { ok: true, duplicate: true, id: `${e.sourceId}:${e.sequence}` };
  if (e.sequence !== last + 1) return { ok: false, reason: 'sequence-gap' };
  if (!last && Object.keys(ledger.streams).length >= 2048) return { ok: false, reason: 'ledger-full' };
  const next = clone(state); next.consumption = clone(ledger);
  next.consumption.streams[e.sourceId] = e.sequence;
  // Disabled capability still acknowledges the receipt: enabling later cannot reward an old sip.
  if (enabled) {
    next.v.water = Math.min(100, Math.max(0, next.v.water + e.ml * CONSUMPTION.waterPerMl));
    next.consumption.totalMl += e.ml;
    const current = coffeeAmount(ledger, now);
    const dose = e.kind === 'coffee' ? e.ml * CONSUMPTION.coffeePerMl * 2 ** (-Math.max(0, now - e.at) / CONSUMPTION.halfLife) : 0;
    next.consumption.coffee = { amount: Math.min(CONSUMPTION.coffeeMax, current + dose), at: now };
  }
  return { ok: true, id: `${e.sourceId}:${e.sequence}`, next, appliedMl: enabled ? e.ml : 0 };
}
export function settlePreparation(state, e, enabled) {
  if (!idOK(e?.id) || !['beans', 'drinks', 'milk'].includes(e.stock) || !Number.isFinite(e.preparedMl) || e.preparedMl <= 0)
    return { ok: false, reason: 'invalid-preparation' };
  const ledger = state.consumption || freshLedger(), previous = ledger.preparations[e.id];
  if (previous) return previous === e.stock ? { ok: true, duplicate: true } : { ok: false, reason: 'preparation-conflict' };
  if (!enabled || !(state.stock[e.stock] > 0)) return { ok: false, reason: 'unavailable-stock' };
  if (Object.keys(ledger.preparations).length >= 4096) return { ok: false, reason: 'ledger-full' };
  const next = clone(state); next.consumption = clone(ledger);
  next.consumption.preparations[e.id] = e.stock; next.stock[e.stock]--;
  return { ok: true, next };
}

// Solve the same clipped cylinder capacity as the physical liquid model. Keep the
// existing wrist-angle budget; an unreachable last sip must not be forced.
export function sipTarget(cup, capacityMl, maxAngle = 1.2) {
  if (!(cup?.ml > 0.000001) || !(cup.R > 0) || !(cup.H > 0)) return { angle: 0, reachable: false, reason: 'empty' };
  let lo = 0, hi = Math.PI / 2 - 0.0001;
  for (let i = 0; i < 42; i++) {
    const mid = (lo + hi) / 2;
    if (capacityMl(cup.R, cup.H, Math.tan(mid)) > cup.ml) lo = mid; else hi = mid;
  }
  const angle = hi + 0.025;
  return angle <= maxAngle ? { angle, critical: hi, reachable: true }
    : { angle: 0, critical: hi, reachable: false, reason: 'wrist-budget' };
}

export class ConsumptionOutbox {
  constructor(saved, newId = () => crypto.randomUUID()) {
    this.state = saved ? clone(saved) : { version: 1, sourceId: newId(), sequence: 0, pending: [], preparations: [] };
    const s = this.state;
    if (s.version !== 1 || !idOK(s.sourceId) || !Number.isSafeInteger(s.sequence) || s.sequence < 0 ||
        !Array.isArray(s.pending) || !Array.isArray(s.preparations) || s.pending.some((e, i) => !validReceipt(e) ||
          e.sourceId !== s.sourceId || e.sequence > s.sequence || (i && e.sequence !== s.pending[i - 1].sequence + 1)))
      throw new Error('Invalid consumption outbox');
    this.seen = new WeakSet();
  }
  drink(event, kind, sceneId, at) {
    if (event?.type !== 'drink' || this.seen.has(event)) return null;
    const s = this.state, e = { version: 1, sourceId: s.sourceId, sequence: s.sequence + 1, sceneId, cup: event.cup, kind, ml: event.ml, temp: event.temp, at };
    if (!validReceipt(e) || s.pending.length >= 4096) throw new Error('Invalid or full consumption outbox');
    this.seen.add(event); s.sequence++; s.pending.push(e); return e;
  }
  // Save the removed world volume and pending receipts FIRST. A failed world or
  // Needs save never acknowledges the receipt. Replays retain their exact IDs.
  flush(persistWorld, needs) {
    if (!persistWorld(this.state)) return false;
    if (!needs || typeof needs.prepareStock !== 'function' || typeof needs.consumeLiquid !== 'function')
      return this.state.pending.length === 0 && this.state.preparations.length === 0;
    while (this.state.preparations.length) {
      if (!needs.prepareStock(this.state.preparations[0]).ok) return false;
      this.state.preparations.shift();
    }
    while (this.state.pending.length) {
      if (!needs.consumeLiquid(this.state.pending[0]).ok) return false;
      this.state.pending.shift();
    }
    return true;
  }
}
