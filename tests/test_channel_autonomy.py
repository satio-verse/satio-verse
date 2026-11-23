"""No platform accounts, network, real settings or model calls."""
import copy
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from wm.agent.governance import BlacklistStore
from wm.agent.life import LifeConflict, LifeSettings
from wm.agent.life_http import dispatch
from wm.channels.manager import ChannelManager, parse_conf


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def channel(tmp_path):
    manager = ChannelManager({'channels': {'weixin': {'home': str(tmp_path / 'weixin')}}}, SimpleNamespace(memory=None, llm=None))
    manager.conf = parse_conf({'enabled': True, 'self_id': 11, 'owner_id': 22,
        'convs': [{'type': 'group', 'id': 33, 'name': 'core', 'core': True}, {'type': 'group', 'id': 44}],
        'proactive': {'enabled': True}})[0]
    manager.clock = lambda: time.time()
    manager.verified = True
    manager.governance = BlacklistStore(tmp_path / 'agent', defaults=False)
    manager.bind_life(tmp_path / 'agent', manager.governance)
    manager.notify = AsyncMock()
    manager._diary = AsyncMock()
    manager._doing = AsyncMock()
    manager.client = SimpleNamespace(call=AsyncMock(return_value={'message_id': 1}))
    return manager


def test_full_access_defaults_to_all_with_blocklist(channel):
    assert not channel._allowed(('group', 55))
    channel.governance.set_global_unrestricted(True)
    assert channel._allowed(('group', 55))
    assert channel._allowed(('private', 66))
    channel.conf['block'].add(66)
    channel.conf['blocked_groups'].add(55)
    assert not channel._allowed(('private', 66))
    assert not channel._allowed(('group', 55))


def test_full_access_respects_explicit_subset(channel):
    channel.governance.set_global_unrestricted(True)
    channel.conf['access_scope'] = 'selected'
    assert channel._allowed(('group', 33))
    assert not channel._allowed(('group', 55))
    channel.conf['access_scope'] = 'core'
    assert channel._allowed(('group', 33))
    assert not channel._allowed(('group', 44))
    assert channel._allowed(('private', 22))


def test_all_scope_does_not_imply_full_execution(channel):
    channel.conf['access_scope'] = 'all'
    assert channel._allowed(('private', 66))
    assert not channel._unrestricted()
    assert channel._conv_info(('private', 66))['send'] == 'draft'


@pytest.mark.anyio
async def test_full_send_new_recipient_and_revoke(channel):
    channel.governance.set_global_unrestricted(True)
    conv = channel._conv(('private', 66))
    assert await channel._send(conv, ['hello'])
    channel.governance.set_global_unrestricted(False)
    assert not await channel._send(conv, ['blocked'])
    assert channel.client.call.await_count == 1


@pytest.mark.anyio
async def test_send_checks_pause_and_explicit_rules(channel):
    conv = channel._conv(('group', 33))
    channel.governance.set_global_unrestricted(True)
    channel.paused = True
    assert not await channel._send(conv, ['hello'])
    channel.paused = False
    channel.governance.add({'kind': 'tool', 'value': 'channel_send', 'reason': 'stop channel'})
    assert not await channel._send(conv, ['hello'])
    channel.client.call.assert_not_awaited()


@pytest.mark.anyio
async def test_full_delivery_skips_draft_and_normal_restores_it(channel):
    channel._send = AsyncMock(return_value=True)
    channel._draft = AsyncMock()
    conv = channel._conv(('group', 33))
    reply = {'reply': 'hello', 'tokens': 4}
    rec = {'conv': 'group:33'}
    assert await channel._deliver(conv, reply, rec, time.monotonic()) == 'draft'
    channel.governance.set_global_unrestricted(True)
    assert await channel._deliver(conv, reply, rec, time.monotonic()) == 'sent'
    channel._draft.assert_awaited_once()
    channel._send.assert_awaited_once()


@pytest.mark.anyio
async def test_full_proactive_ignores_counts_but_not_quiet(channel):
    now = datetime.now()
    channel.pro_day = now.date().isoformat()
    channel.pro_count = channel.unanswered = 100000
    channel._reach = AsyncMock(return_value='sent')
    channel._live = AsyncMock()
    channel.governance.set_global_unrestricted(True)
    await channel._maybe_proactive(now)
    channel._reach.assert_awaited_once()
    channel.conf['proactive']['quiet'] = (0, 1440)
    await channel._maybe_proactive(now)
    channel._reach.assert_awaited_once()


@pytest.mark.anyio
async def test_full_bili_ignores_per_day_and_draft(channel):
    channel.governance.set_global_unrestricted(True)
    channel.conf['bili_comment']['send'] = 'ask'
    channel.bili_count = 10000
    channel._post_bili = AsyncMock(return_value=True)
    assert await channel._bili_out({'text': 'hello', 'title': 'test'}, ('bili_comment', 'BV123'), 'test') == 'sent'
    channel._post_bili.assert_awaited_once()


@pytest.mark.anyio
async def test_qzone_last_boundary_checks_blacklist(channel):
    channel.governance.set_global_unrestricted(True)
    channel.governance.add({'kind': 'domain', 'value': 'qzone.qq.com', 'reason': 'no posts'})
    assert not await channel._post_qzone('hello')
    channel.client.call.assert_not_awaited()


def test_life_apply_adoption_and_restart(channel):
    life = channel.life_settings
    plan = life.preview({'proactive.idle_h': 1.5, 'proactive.quiet': '23:00-08:00'})
    state = life.apply({'proactive.idle_h': 1.5, 'proactive.quiet': '23:00-08:00'}, '尝试较短间隔', plan['revision'], 'assistant')
    assert state['values']['proactive.idle_h'] == 1.5
    assert channel.conf['proactive']['quiet'] == (1380, 480)
    assert not state['evidence']
    life.consumed('proactive', 'test-read')
    assert life.state()['evidence']['proactive.idle_h']['value'] == 1.5
    restored = LifeSettings(life.path.parent, channel)
    assert restored.overlay(channel._life_base_conf)['proactive']['idle_h'] == 1.5
    assert not restored.state()['evidence']


def test_life_revision_and_restore_conflict(channel):
    life = channel.life_settings
    first = life.apply({'proactive.idle_h': 1}, 'one', 0)
    rid = first['history'][0]['id']
    with pytest.raises(LifeConflict):
        life.apply({'proactive.idle_h': 2}, 'stale', 0)
    life.apply({'proactive.idle_h': 2}, 'two', 1)
    with pytest.raises(LifeConflict):
        life.rollback(rid, 2, 'rollback', preview=False)


def test_life_rollback_and_observe(channel):
    life = channel.life_settings
    before = life.state()['values']['proactive.idle_h']
    state = dispatch(life, {'action': 'apply', 'changes': {'proactive.idle_h': 1}, 'reason': 'trial', 'expected_revision': 0}, 'assistant')
    rid = state['history'][0]['id']
    plan = life.rollback(rid, 1, 'restore', preview=True)
    assert plan['changes'][0]['after'] == before
    life.observe(rid, '脚本验证，不是模型偏好', 'user')
    state = life.rollback(rid, 2, '恢复', preview=False)
    assert state['values']['proactive.idle_h'] == before
    assert state['history'][1]['observations'][0]['text'] == '脚本验证，不是模型偏好'
    assert 'proactive.idle_h' not in state['overrides']


def test_life_restore_empty_quiet_window(channel):
    life = channel.life_settings
    state = life.apply({'proactive.quiet': '23:00-08:00'}, '试用', 0)
    state = life.rollback(state['history'][0]['id'], 1, '恢复', preview=False)
    assert state['values']['proactive.quiet'] == ''
    assert channel.conf['proactive']['quiet'] is None


def test_life_replans_window_without_erasing_usage(channel):
    channel.qz_day, channel.qz_plan = 'today', [datetime.now()]
    channel.budget.used = 17
    channel.life_settings.apply({'qzone.window': '20:00-23:00'}, '换一个时间', 0)
    assert channel.qz_day == '' and not channel.qz_plan
    assert channel.budget.used == 17


def test_full_computer_prompt_does_not_restore_old_approval_gate(channel):
    channel.governance.set_global_unrestricted(True)
    text = channel._task_prompt('整理文件', 'self')
    assert '完全权限' in text and '黑名单' in text
    assert '要点击或输入时会先问主人' not in text


def test_computer_prompt_checks_black_screen_before_analyzing(channel):
    """截屏、看图前先确认画面不是全黑的: 任务单里要写清楚, 全黑就先等, 别急着分析、别照着黑屏点."""
    for by in ('owner', 'self'):
        text = channel._task_prompt('看一眼屏幕', by)
        assert '画面是不是黑的' in text and '别急着分析' in text and '也别照着黑屏点' in text


@pytest.mark.anyio
async def test_pause_between_message_segments_stops_send(channel, monkeypatch):
    import asyncio
    conv = channel._conv(('group', 33))
    async def pause(_):
        channel.paused = True
    monkeypatch.setattr(asyncio, 'sleep', pause)
    assert not await channel._send(conv, ['first', 'second'])
    assert channel.client.call.await_count == 1


@pytest.mark.parametrize('changes', [{'token': 'secret'}, {'daily_tokens': 0}, {'proactive.idle_h': float('nan')}, {'proactive.enabled': 1}, {'proactive.quiet': 'wrong'}, {'global_unrestricted': True}])
def test_life_rejects_nonlife_and_invalid_values(channel, changes):
    with pytest.raises(ValueError):
        channel.life_settings.preview(changes)


def test_life_failed_disk_does_not_adopt(channel, monkeypatch):
    life = channel.life_settings
    before = copy.deepcopy(channel.conf)
    def fail(*_): raise OSError('disk')
    monkeypatch.setattr(life, '_save', fail)
    with pytest.raises(OSError):
        life.apply({'proactive.idle_h': 1}, 'trial', 0)
    assert channel.conf == before
