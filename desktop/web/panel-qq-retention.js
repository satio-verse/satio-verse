// Raw archive controls: missing means inherit, false is an explicit choice.
// Rendering and status polling never mutate the saved/draft settings.
export function archiveChoice(value) {
  return value === undefined ? 'inherit' : value === true ? 'on' : value === false ? 'off' : 'invalid';
}

export function archiveOptions(value, inherited = '沿用服务配置（缺省不保存）') {
  const selected = archiveChoice(value);
  const options = [['inherit', inherited], ['off', '不保存原文'], ['on', '保存原文']];
  if (selected === 'invalid') options.push(['invalid', '已有值无效，请重新选择']);
  return options.map(([id, label]) => `<option value="${id}" ${selected === id ? 'selected' : ''}${id === 'invalid' ? ' disabled' : ''}>${label}</option>`).join('');
}

export function setArchiveChoice(target, field, choice) {
  if (!target || typeof target !== 'object' || Array.isArray(target)) throw new TypeError('归档设置必须是对象');
  if (!['inherit', 'on', 'off'].includes(choice)) throw new RangeError('未知的归档选择');
  if (choice === 'inherit') delete target[field];
  else target[field] = choice === 'on';
}

export function setGroupArchive(q, choice) {
  if (!['inherit', 'on', 'off'].includes(choice)) throw new RangeError('未知的归档选择');
  if (q.group === undefined && choice === 'inherit') return;
  if (q.group === undefined) q.group = {};
  setArchiveChoice(q.group, 'log_raw', choice);
}

export function retentionSummary(status) {
  const policy = status?.retention;
  if (!policy || typeof policy.group_log_raw !== 'boolean' || typeof policy.api_log_raw !== 'boolean') {
    return '尚未收到可确认的归档策略。保存的选择不代表已生效；请等待服务回报。';
  }
  const count = Number.isInteger(policy.group_overrides) && policy.group_overrides >= 0
    ? `；单群覆盖 ${policy.group_overrides} 项（服务未提供逐群明细）` : '；单群覆盖数量未知';
  return `服务最近回报：群聊默认${policy.group_log_raw ? '保存' : '不保存'}原文${count}；访客会话${policy.api_log_raw ? '保存' : '不保存'}原文。`;
}

export function retentionDifference(q, status) {
  const p = status?.retention;
  if (!p) return '';
  const different = [[q.group?.log_raw, p.group_log_raw], [q.api_log_raw, p.api_log_raw]]
    .some(([draft, live]) => typeof draft === 'boolean' && typeof live === 'boolean' && draft !== live);
  return different ? '本机选择与服务回报不同，可能尚未保存或尚未生效。' : '';
}

export function retentionCard(q) {
  return `<div class="card">
    <h2>聊天原文归档</h2>
    <p class="hint">群聊与访客原文缺省不保存到知识库；当前聊天仍保留有限的短期上下文。开启原文归档也不会把聊天自动变成可信知识。</p>
    <div class="row"><label for="qGroupLogRaw">群聊默认</label><select id="qGroupLogRaw">${archiveOptions(q.group?.log_raw)}</select></div>
    <p class="hint">单个群可在“访问权限”列表的“原文归档”列覆盖此默认值。完全权限不会自动开启归档。</p>
    <div class="row"><label for="qApiLogRaw">第三方访客会话</label><select id="qApiLogRaw">${archiveOptions(q.api_log_raw)}</select></div>
    <p class="hint">访客接口通常无法区分群与私聊，此项控制经它转入的全部原文。QQ 好友私聊仍沿用原机制；第三方框架自己的记录不受这里控制。</p>
    <p class="hint">关闭后不再写入或回放该类原文，也不清理已有档案。明确开启后按服务配置的保留期处理。</p>
    <p class="hint" id="qRetentionStatus">等待服务回报归档策略…</p>
    <p class="hint" id="qRetentionDifference"></p>
  </div>`;
}
