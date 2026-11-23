"""Inspectable, reversible choices consumed by the real channel scheduler.

This module records choices and consumer reads, never inferred wishes or feelings.
Credentials, recipient access, permission modes and monetary budgets stay outside it.
"""
from __future__ import annotations

import copy
import json
import math
import os
import threading
import time
import uuid
from pathlib import Path


CATALOG = {
    'proactive.enabled': {'label': '主动联系', 'type': 'boolean', 'effect': '是否主动联系已配置的主人', 'cost': '开启后可能调用模型和发送消息'},
    'proactive.idle_h': {'label': '安静多久再联系', 'type': 'number', 'min': .001, 'max': 72, 'unit': '小时', 'effect': '两次主动联系机会之间的安静时间', 'cost': '间隔越短，模型机会越多'},
    'proactive.quiet': {'label': '免打扰时间', 'type': 'window', 'effect': '此时段不主动联系，可继续独处活动', 'cost': '不会增加费用'},
    'proactive.life': {'label': '独处活动', 'type': 'boolean', 'effect': '无法联系时是否继续安排自己的活动', 'cost': '活动可能调用模型；文字设想不算实际完成'},
    'proactive.video': {'label': '浏览视频', 'type': 'boolean', 'effect': '独处活动是否包含读取 B 站视频', 'cost': '读取、理解视频会使用网络和模型'},
    'proactive.tick_s': {'label': '检查生活机会', 'type': 'number', 'min': 1, 'max': 3600, 'unit': '秒', 'effect': '调度器多久检查一次机会；不代表每次都调用模型', 'cost': '较短间隔增加检查频率'},
    'proactive.wander_min': {'label': '多久出去逛逛', 'type': 'number', 'min': 0, 'max': 1440, 'unit': '分钟', 'effect': '每隔多久自己去 B 站、评论区这些地方待一会儿；0 = 不逛', 'cost': '逛的时候会读视频、调模型，按用量计费'},
    'qzone.enabled': {'label': '空间动态', 'type': 'boolean', 'effect': '是否在已授权账号安排空间动态', 'cost': '写作使用模型，发送仍取决于账号与权限状态'},
    'qzone.window': {'label': '动态活动时间', 'type': 'window', 'effect': '在此时段安排动态机会', 'cost': '本身不会增加次数'},
    'qzone.reply': {'label': '回复空间评论', 'type': 'boolean', 'effect': '是否检查自己动态下面的评论', 'cost': '回复可能调用模型和发送消息'},
    'qzone.reply_check_s': {'label': '检查评论间隔', 'type': 'number', 'min': 1, 'max': 86400, 'unit': '秒', 'effect': '隔多久检查评论', 'cost': '间隔越短，平台查询越频繁'},
    'group.look_gap_s': {'label': '群消息观察间隔', 'type': 'number', 'min': 0, 'max': 3600, 'unit': '秒', 'effect': '无人点名时批量观察群消息的间隔', 'cost': '减少间隔会增加模型调用'},
}


class LifeConflict(ValueError):
    pass


def get_field(conf, key):
    group, field = key.split('.', 1)
    return copy.deepcopy((conf.get(group) or {}).get(field))


def wire_value(value):
    if value is None:
        return ''
    if isinstance(value, tuple) and len(value) == 2:
        return '-'.join(f'{int(x)//60:02}:{int(x)%60:02}' for x in value)
    return copy.deepcopy(value)


def validate(changes):
    if not isinstance(changes, dict) or not changes:
        raise ValueError('请选择至少一项生活设置')
    out = {}
    for key, value in changes.items():
        spec = CATALOG.get(key)
        if spec is None:
            raise ValueError(f'不能通过生活接口修改 {key}')
        if spec['type'] == 'boolean':
            if type(value) is not bool:
                raise ValueError(f'{key} 必须是开关')
        elif spec['type'] == 'number':
            if type(value) not in (int, float) or not math.isfinite(value) or not spec['min'] <= value <= spec['max']:
                raise ValueError(f'{key} 超出有效范围')
        else:
            from ..channels.manager import _window
            if not isinstance(value, str):
                raise ValueError(f'{key} 必须是时间段文字')
            _window(value, spec['label'])
        out[key] = value
    return out


class LifeSettings:
    def __init__(self, home: Path, manager):
        self.path = Path(home) / 'life.json'
        self.manager = manager
        self._lock = threading.RLock()
        self.evidence = {}

    def _read(self):
        if not self.path.exists():
            return {'version': 1, 'revision': 0, 'overrides': {}, 'history': []}
        if self.path.stat().st_size > 2_000_000:
            raise ValueError('生活设置文件过大')
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if data.get('version') != 1 or type(data.get('revision')) is not int or not isinstance(data.get('history'), list):
            raise ValueError('生活设置文件格式不正确')
        if data.get('overrides'):
            validate(data['overrides'])
        return data

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f'.life-{uuid.uuid4().hex}.tmp')
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def overlay(self, conf):
        data = self._read()
        out = copy.deepcopy(conf)
        for key, value in data['overrides'].items():
            group, field = key.split('.', 1)
            if CATALOG[key]['type'] == 'window':
                from ..channels.manager import _window
                value = _window(value, CATALOG[key]['label'])
            out.setdefault(group, {})[field] = copy.deepcopy(value)
        return out

    def consumed(self, group, consumer):
        """Called at the scheduler read, not merely on successful config saving."""
        data = self._read()
        for key in CATALOG:
            if key.startswith(group + '.'):
                self.evidence[key] = {'revision': data['revision'], 'value': wire_value(get_field(self.manager.conf or {}, key)),
                                      'consumer': consumer, 'time': time.time()}

    def _adopt(self, changes):
        self.manager.conf = self.overlay(self.manager._life_base_conf or self.manager.conf)
        if {'qzone.window', 'qzone.enabled'} & set(changes):
            # Existing planned timestamps were generated using the old window.
            # Recompute opportunities without resetting usage or posting anything.
            self.manager.qz_day, self.manager.qz_plan = '', []
        self.evidence.clear()

    def state(self):
        with self._lock:
            data = self._read()
            values = {key: wire_value(get_field(self.manager.conf or {}, key)) for key in CATALOG}
            return {'revision': data['revision'], 'values': values, 'overrides': data['overrides'],
                    'catalog': [{'key': key, **spec} for key, spec in CATALOG.items()],
                    'evidence': copy.deepcopy(self.evidence), 'history': list(reversed(data['history'][-100:])),
                    'connected': bool(self.manager.conf), 'paused': bool(self.manager.paused),
                    'permission': 'unrestricted' if self.manager._unrestricted() else 'configured',
                    'note': '采用证据表示调度器读到了值，不代表已发消息、已浏览视频或已形成偏好。'}

    def preview(self, changes):
        changes = validate(changes)
        data = self._read()
        return {'revision': data['revision'], 'changes': [{'key': key, 'before': wire_value(get_field(self.manager.conf or {}, key)),
                    'after': value, **CATALOG[key]} for key, value in changes.items()]}

    def apply(self, changes, reason, expected_revision, actor='user'):
        with self._lock:
            changes = validate(changes)
            if not self.manager.conf:
                raise ValueError('渠道配置尚未加载，暂不能调整生活设置')
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                raise ValueError('请填写调整原因（最多 2000 字）')
            if actor not in {'user', 'assistant'}:
                raise ValueError('修改来源无效')
            data = self._read()
            if type(expected_revision) is not int or expected_revision != data['revision']:
                raise LifeConflict('生活设置已变化，请重新预览')
            before = {key: wire_value(get_field(self.manager.conf, key)) for key in changes}
            record = {'id': uuid.uuid4().hex, 'time': time.time(), 'actor': actor, 'reason': reason.strip(),
                      'before': before, 'after': changes, 'previous_overrides': {key: data['overrides'].get(key) for key in changes},
                      'revision': data['revision'] + 1, 'observations': []}
            data['revision'] += 1
            data['overrides'].update(changes)
            data['history'] = [*data['history'][-199:], record]
            self._save(data)
            self._adopt(changes)
            return self.state()

    def observe(self, change_id, text, actor='user'):
        with self._lock:
            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                raise ValueError('观察内容应为 1 到 2000 字')
            data = self._read()
            record = next((x for x in data['history'] if x['id'] == change_id), None)
            if not record:
                raise ValueError('没有这次调整记录')
            record['observations'] = [*record['observations'][-19:], {'text': text.strip(), 'actor': actor, 'time': time.time()}]
            data['revision'] += 1
            self._save(data)
            return self.state()

    def rollback(self, change_id, expected_revision, reason, *, preview=False, actor='user'):
        with self._lock:
            data = self._read()
            record = next((x for x in data['history'] if x['id'] == change_id), None)
            if not record:
                raise ValueError('没有这次调整记录')
            for key, value in record['after'].items():
                if wire_value(get_field(self.manager.conf or {}, key)) != value:
                    raise LifeConflict('这次调整的字段后来又发生了变化，不能覆盖新的选择')
            if preview:
                return self.preview(record['before'])
            if type(expected_revision) is not int or expected_revision != data['revision']:
                raise LifeConflict('生活设置已变化，请重新预览')
            # Restore previous inheritance as well as the visible value. A later
            # normal settings change must not be masked by an obsolete override.
            restored = copy.deepcopy(data)
            for key in record['after']:
                previous = record.get('previous_overrides', {}).get(key)
                if previous is None:
                    restored['overrides'].pop(key, None)
                else:
                    restored['overrides'][key] = previous
            restored['revision'] += 1
            restored['history'] = [*data['history'][-199:], {'id': uuid.uuid4().hex, 'time': time.time(),
                'actor': actor, 'reason': str(reason or f'恢复 {change_id}')[:2000], 'restores': change_id,
                'before': record['after'], 'after': record['before'], 'revision': restored['revision'],
                'previous_overrides': {key: data['overrides'].get(key) for key in record['after']}, 'observations': []}]
            self._save(restored)
            self._adopt(record['before'])
            return self.state()
