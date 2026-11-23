// A terminal reply and the physical execution lease are separate: an unconfirmed
// cancellation may reply after a bounded wait, but cannot admit another action.
export class RoomActionRequests {
  constructor({ enqueue, cancel, timeoutMs = 180000, cleanupTimeoutMs = 45000, schedule = (fn, ms) => setTimeout(fn, ms), unschedule = id => clearTimeout(id) }) {
    Object.assign(this, { enqueue, cancel, timeoutMs, cleanupTimeoutMs, schedule, unschedule });
    this.active = null;
    this.cache = new Map();
    this.seq = 0;
  }
  get blocked() { return !!this.active?.cancelled || this.active?.phase === 'blocked'; }
  _trim() {
    while (this.cache.size > 64) {
      const oldest = this.cache.keys().next().value;
      if (oldest === this.active?.id) {
        const current = this.cache.get(oldest); this.cache.delete(oldest); this.cache.set(oldest, current);
      } else this.cache.delete(oldest);
    }
  }
  request(act, { requestId } = {}) {
    const id = String(requestId || `local-${++this.seq}`);
    if (this.cache.has(id)) {
      const old = this.cache.get(id);
      return old.act === act ? old.promise : Promise.resolve({ ok: false, act, started: false, completed: false, reason: '请求编号已用于另一动作' });
    }
    if (this.active) {
      const reason = this.blocked ? '上一件请求仍在收尾，尚未确认结束' : '上一件请求正在执行，请等待完成或先停止';
      const promise = Promise.resolve({ ok: false, act, requestId: id, started: false, completed: false, reason });
      this.cache.set(id, { id, act, promise }); this._trim();
      return promise;
    }
    let resolve;
    const promise = new Promise(r => { resolve = r; });
    const entry = { id, act, resolve, promise, started: false, phase: 'queued' };
    this.cache.set(id, entry);
    this.active = entry; this._trim();
    entry.timer = this.schedule(() => this._cancel(entry, '动作超时，正在收尾'), this.timeoutMs);
    try {
      if (!this.enqueue(act)) this.finish(entry, { ok: false, reason: '当前场景不支持或所需物品不足' });
    } catch (error) { this.finish(entry, { ok: false, reason: error.message }); }
    return promise;
  }
  begin(act) {
    const entry = this.active;
    if (!entry || entry.act !== act || entry.phase !== 'queued' || entry.cancelled) return null;
    entry.started = true; entry.phase = 'running';
    return entry;
  }
  _clearTimers(entry) {
    this.unschedule(entry.timer); this.unschedule(entry.cleanupTimer);
    entry.timer = entry.cleanupTimer = undefined;
  }
  _reply(entry, result) {
    if (!entry.receipt) {
      entry.receipt = { ...result, act: entry.act, requestId: entry.id, started: entry.started, completed: result.ok === true };
      entry.resolve(entry.receipt);
    }
    return entry.receipt;
  }
  finish(entry, result) {
    if (!entry || this.active !== entry) return null;
    this._clearTimers(entry);
    const confirmed = result.cleanupConfirmed === undefined || result.cleanupConfirmed === true;
    const ok = result.ok === true && !entry.cancelled && confirmed;
    const reason = !confirmed ? (result.reason || '动作收尾未确认') : entry.cancelled ? entry.cancelReason : result.reason;
    const final = { ...result, ok, ...(reason && { reason }), cancelled: !!entry.cancelled, cleanupConfirmed: confirmed,
      outcome: confirmed ? (entry.cancelled ? 'cancelled' : ok ? 'completed' : 'failed') : (entry.cancelled ? 'cancel_unconfirmed' : 'failed') };
    entry.phase = confirmed ? 'terminal' : 'blocked';
    if (confirmed) this.active = null;
    // A late confirmation releases the lease, never rewrites an already delivered reply.
    return this._reply(entry, final);
  }
  _cancel(entry, reason) {
    if (!entry || this.active !== entry || entry.cancelled || entry.phase === 'blocked') return;
    entry.cancelled = true; entry.cancelReason = reason; entry.phase = 'cancelling';
    this.unschedule(entry.timer); entry.timer = undefined;
    try { this.cancel?.(entry.act, entry); }
    catch (error) {
      entry.phase = 'blocked';
      this._reply(entry, { ok: false, cancelled: true, cleanupConfirmed: false, outcome: 'cancel_unconfirmed',
        reason: `${reason}；停止回调失败，尚未确认收尾: ${error.message}` });
      return;
    }
    if (this.active !== entry) return; // A synchronous adapter may have confirmed cleanup.
    if (!entry.started) { this.finish(entry, { ok: false, reason }); return; }
    entry.cleanupTimer = this.schedule(() => {
      if (this.active !== entry || entry.phase !== 'cancelling') return;
      entry.phase = 'blocked';
      this._reply(entry, { ok: false, cancelled: true, cleanupConfirmed: false, outcome: 'cancel_unconfirmed',
        reason: `${reason}；收尾等待超时，尚未确认结束，已暂停后续活动` });
    }, this.cleanupTimeoutMs);
  }
  stop(reason = '请求已停止') { this._cancel(this.active, reason); }
}
