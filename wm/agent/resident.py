"""Durable local responsibilities, independent of any connected workbench window.

SQLite is the authority; a claimed round is never replayed after a process crash.
Only explicit responsibilities are scheduled. This does not import third-party reminders.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import math
import os
import secrets
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)
STATES = {'scheduled', 'running', 'paused', 'waiting', 'blocked', 'completed', 'interrupted'}


def text(value, limit, label):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{label}不能为空，最多 {limit} 字')
    return value.strip()


def number(value, label):
    if isinstance(value, bool):
        raise ValueError(f'{label}须为非负数字')
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label}须为非负数字') from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f'{label}须为非负数字')
    return result


class Resident:
    def __init__(self, hub, clock=time.time):
        self.hub, self.clock = hub, clock
        self.path = Path(hub.home) / 'resident.sqlite3'
        self._loop = None
        self._workers = {}
        self._closing = False
        self._owner = None
        # 有回报的时候 (做完了、等用户、卡住了、她说这一轮有值得马上告诉用户的进展) 叫这些 (async, 参数是这件事的记录).
        # QQ / 微信那边 (wm/channels/wbthreads.py) 挂在这里, 把回报发到用户在的聊天里
        self.watchers = []

    def _acquire_owner(self):
        """One runner per data directory, including accidental second processes."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.with_suffix('.lock').open('a+b')
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError('已有持续任务调度器使用此数据目录') from exc
        self._owner = handle

    def _release_owner(self):
        if self._owner:
            self._owner.close()  # Closing releases the OS lock, including after a crash.
            self._owner = None

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS responsibilities (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS events (cursor INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT NOT NULL, data TEXT NOT NULL)')
        return db

    def get(self, gid):
        with contextlib.closing(self.connect()) as db:
            row = db.execute('SELECT data FROM responsibilities WHERE id=?', (str(gid),)).fetchone()
        if not row:
            raise ValueError('没有这个持续任务')
        return json.loads(row['data'])

    def state(self, after=0):
        after = max(0, int(after))
        with contextlib.closing(self.connect()) as db:
            items = [json.loads(r[0]) for r in db.execute('SELECT data FROM responsibilities')]
            rows = list(db.execute('SELECT cursor,data FROM events WHERE cursor>? ORDER BY cursor LIMIT 100', (after,)))
            latest = db.execute('SELECT COALESCE(MAX(cursor),0) FROM events').fetchone()[0]
        return {'items': sorted(items, key=lambda r: -r['updated']),
                'events': [dict(json.loads(r['data']), cursor=r['cursor']) for r in rows],
                'next_cursor': rows[-1]['cursor'] if rows else after, 'latest_cursor': latest,
                'runner_online': self._loop is not None and not self._loop.done(),
                'location': 'local', 'requires_computer_online': True}

    def _change(self, gid, change, expected=None):
        with contextlib.closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM responsibilities WHERE id=?', (gid,)).fetchone()
            value = json.loads(row[0]) if row else None
            if value is None:
                raise ValueError('没有这个持续任务')
            if expected is not None and (isinstance(expected, bool) or expected != value['revision']):
                raise ValueError('任务状态已更新，请刷新后重试')
            previous = copy.deepcopy(value)
            change(value)
            value.update(revision=value['revision'] + 1, updated=self.clock())
            db.execute('UPDATE responsibilities SET data=? WHERE id=?', (json.dumps(value, ensure_ascii=False), gid))
            if (previous['state'], previous.get('summary')) != (value['state'], value.get('summary')):
                self._event(db, value)
            return value

    def _event(self, db, value):
        event = {k: value[k] for k in ('id', 'session_id', 'title', 'state', 'summary', 'updated', 'revision')}
        db.execute('INSERT INTO events(goal_id,data) VALUES (?,?)', (value['id'], json.dumps(event, ensure_ascii=False)))

    def create(self, source_id, objective, *, title='', next_at=0, budget_cny=0, origin=''):
        """source_id 为空: 用户在 QQ / 微信 / 桌宠上交来的 (origin 写从哪儿), 用默认工作区和权限档."""
        objective = text(objective, 12000, '目标')
        due, budget = number(next_at, '执行时间'), number(budget_cny, '目标预算')
        source = self.hub._managed_session(source_id) if source_id else None
        if source is not None and (source.parent_id or source.archived):
            raise ValueError('请从未归档的主会话建立持续任务')
        if title:
            title = text(title, 100, '标题')
        if source is None:
            session = self.hub.new_session()
        else:
            session = self.hub.new_session(source.workspace, source.mode, source.model)
            session.preset, session.effort, session.web = source.preset, source.effort, source.web
            session.time_budget_s, session.max_steps = source.time_budget_s, source.max_steps
            session.materials = [{**copy.deepcopy(x), 'owner': x.get('owner', source.id)} for x in source.materials]
        session.title = title or objective.splitlines()[0][:60]
        self.hub.save(session)
        now = self.clock()
        value = dict(id=secrets.token_hex(12), source_id=source.id if source else '', session_id=session.id,
                     title=session.title, objective=objective, state='scheduled', next_at=max(now, due),
                     budget_cny=budget, spent=0.0, rounds=0, revision=1, created=now, updated=now,
                     summary='已加入后台任务', requested=None, run_id='', origin=str(origin or '')[:20], notes=[], tell='')
        with contextlib.closing(self.connect()) as db, db:
            db.execute('INSERT INTO responsibilities VALUES (?,?)', (value['id'], json.dumps(value, ensure_ascii=False)))
            self._event(db, value)
        return value

    async def control(self, gid, action, expected):
        if action not in {'pause', 'resume', 'complete'}:
            raise ValueError('未知持续任务操作')
        def apply(value):
            if action == 'resume' and value['state'] in {'running', 'scheduled', 'completed'}:
                raise ValueError('当前状态不需要继续')
            value.update(state={'pause': 'paused', 'resume': 'scheduled', 'complete': 'completed'}[action],
                         next_at=self.clock(), requested=None,
                         summary={'pause': '你暂停了任务', 'resume': '等待继续；先核对实际产物', 'complete': '你标记了完成'}[action])
        value = self._change(gid, apply, expected)
        if action != 'resume' and gid in self._workers:
            await self.hub.stop(value['session_id'])
        return value

    def for_session(self, sid):
        return next((x for x in self.state()['items'] if x['session_id'] == sid), None)

    def _spent(self, session):
        """Dedicated session accounting includes every descendant and survives restart."""
        if session is None:
            return 0.0
        teams = getattr(self.hub, 'teams', None)
        own = number(session.stats.get('cost', 0), '会话费用')
        children = number(teams.spent(session.id), '子任务费用') if teams else 0
        return round(own + children, 6)

    def report(self, session, state, summary, delay_s=0, tell=''):
        """tell: 这一轮有值得马上告诉用户的进展 (比如盯的东西有变化了), 一两句; 这一轮结束后交给 watchers 发出去."""
        item = self.for_session(session.id)
        if not item:
            raise ValueError('当前会话未绑定持续任务；由用户在持续任务页创建')
        if state not in {'progress', 'waiting', 'blocked', 'completed'}:
            raise ValueError('状态应为 progress/waiting/blocked/completed')
        summary, delay = text(summary, 2000, '进展依据'), number(delay_s, '下次等待秒数')
        tell = str(tell or '').strip()[:300]
        def apply(value):
            if (value['state'] != 'running' or session.stop_event.is_set()
                    or not value.get('run_id') or value['run_id'] != session.run.get('id')):
                raise ValueError('任务已暂停或没有正在运行的轮次')
            value.update(requested='scheduled' if state == 'progress' else state,
                         next_at=self.clock() + max(1, delay), summary=summary, tell=tell)
        return self._change(item['id'], apply)

    def answer(self, gid, note):
        """用户后来补充的话 (QQ 上引用这件事的回报再说的): 记下来, 下一轮开头交给她; 在等用户、卡住、停在半路、暂停了的,
        接着排下一轮. 这一轮正在做的由调用方直接加到这一轮 (hub.steer), 不走这里."""
        note = text(note, 2000, '补充')
        def apply(value):
            value['notes'] = ((value.get('notes') or []) + [{'text': note, 'at': self.clock()}])[-5:]
            if value['state'] in {'waiting', 'blocked', 'interrupted', 'paused'}:
                value.update(state='scheduled', next_at=self.clock(), requested=None, summary='用户补充了要求，接着做')
            elif value['state'] == 'scheduled':  # 还没到下一轮: 提前, 用户的话不等几个小时
                value['next_at'] = min(value['next_at'], self.clock())
        return self._change(str(gid), apply)

    async def _tell(self, item):
        for watch in list(self.watchers):
            try:
                await watch(copy.deepcopy(item))
            except Exception:  # noqa: BLE001 (回报没发成不影响持续任务本身)
                log.exception('持续任务的回报没发成')

    async def start(self):
        if self._loop is not None and not self._loop.done():
            return
        self._acquire_owner()
        try:
            self._closing = False
            for item in self.state()['items']:
                if item['state'] == 'running':
                    spent = self._spent(self.hub.get(item['session_id']))
                    self._change(item['id'], lambda v: v.update(state='interrupted', requested=None, spent=max(v['spent'], spent),
                        summary='上次执行期间服务退出；操作结果可能未知，请核对后继续'))
            self._loop = asyncio.create_task(self._run_loop())
        except BaseException:
            self._release_owner()
            raise

    async def close(self):
        self._closing = True
        if self._loop:
            self._loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop
        for gid, worker in list(self._workers.items()):
            item = self.get(gid)
            if item['state'] == 'running':
                self._change(gid, lambda v: v.update(state='interrupted', requested=None, summary='服务已停止；下次先核对进度再继续'))
            await self.hub.stop(item['session_id'])
            worker.cancel()
        if self._workers:
            await asyncio.gather(*list(self._workers.values()), return_exceptions=True)
        self._workers.clear()
        self._release_owner()

    async def _run_loop(self):
        while not self._closing:
            try:
                await self.tick()
            except Exception:
                log.exception('持续任务调度失败')
            await asyncio.sleep(2)

    async def tick(self):
        # Fair, single-round dispatch. User conversation and subagents remain independent.
        if self._closing or self._workers:
            return
        due = sorted((x for x in self.state()['items'] if x['state'] == 'scheduled' and x['next_at'] <= self.clock()), key=lambda x: x['next_at'])
        for item in due:
            session = self.hub.get(item['session_id'])
            if session and session.status != 'idle':
                continue
            def claim(value):
                if value['state'] != 'scheduled':
                    raise ValueError('任务已被处理')
                value.update(state='running', requested=None)
            self._change(item['id'], claim, item['revision'])
            worker = asyncio.create_task(self._execute(item['id']))
            self._workers[item['id']] = worker
            def finished(task, gid=item['id']):
                self._workers.pop(gid, None)
                if not task.cancelled() and task.exception() is not None:
                    log.error('持续任务执行异常：%s', gid, exc_info=task.exception())
            worker.add_done_callback(finished)
            break

    async def _execute(self, gid):
        session = None
        try:
            item = self.get(gid)
            session = self.hub.get(item['session_id'])
            if item['state'] != 'running':
                return
            if not session or session.archived:
                await self._tell(self._change(gid, lambda v: v.update(state='blocked', summary='任务会话已归档或不存在')))
                return
            spent = max(item['spent'], self._spent(session))
            self._change(gid, lambda v: v.update(spent=spent))
            remaining = item['budget_cny'] - spent if item['budget_cny'] else None
            if remaining is not None and remaining <= 0:
                await self._tell(self._change(gid, lambda v: v.update(state='blocked', summary='达到你设置的目标总预算')))
                return
            session.budget_cny = remaining if remaining is not None else 0
            prompt = ('继续用户明确建立的持续任务。先核对上轮记录与实际产物，禁止盲目重放结果未知的外部操作。'
                      '执行本轮可推进的工作，完成前用 resident_update 报告进展：全部完成用 completed；'
                      '要等用户信息用 waiting；外部条件阻塞用 blocked；还能继续或定时检查用 progress 并给 delay_s。'
                      '不要为了续行编造目标，不把一轮回复结束当整个任务完成。'
                      '这一轮有值得马上告诉用户的进展（比如盯的东西有变化了），在 resident_update 里写 tell（一两句，会转给用户）；没有就别写。'
                      '每轮开头用 task_plan 列整个目标的步骤和状态（不只这一轮），用户随时能看到做到哪一步。'
                      + ('用户后来补充的话在 owner_notes 里，照新的来。' if item.get('notes') else '') + '\n'
                      + json.dumps({**{k: item[k] for k in ('objective', 'summary', 'rounds')},
                                    **({'owner_notes': [n['text'] for n in item['notes']]} if item.get('notes') else {})},
                                   ensure_ascii=False))
            reason = await self.hub.send(session.id, prompt, preserve_materials=True)
            if reason:
                raise ValueError(reason)
            # _next_queued may replace session.run before this task returns.
            # Keep the run object actually owned by this responsibility.
            run = session.run
            run_id = session.run.get('id', '')
            self._change(gid, lambda v: v.update(run_id=run_id, notes=[], tell=''))  # 补充的话这一轮已经交给她了
            task = session.task
            if task:
                await asyncio.shield(task)
            def finish(value):
                value['spent'] = max(value['spent'], self._spent(session))
                value['rounds'] += 1
                if value['state'] != 'running':
                    return
                if session.run is not run:
                    value.update(state='waiting', summary='会话已收到新的要求；自动续行已暂停，请核对后继续')
                elif run.get('state') != 'completed':
                    value.update(state='blocked', summary=str(run.get('reason') or '本轮未完成，请检查会话')[:2000])
                elif value.get('requested'):
                    value['state'] = value['requested']
                else:
                    value.update(state='waiting', summary='本轮已结束，但未声明目标是否完成；请查看会话并决定下一步')
                value['requested'] = None
            final = self._change(gid, finish)
            if final['state'] in {'completed', 'waiting', 'blocked'} or final.get('tell'):
                await self._tell(final)
            if final['state'] in {'completed', 'waiting', 'blocked'}:
                session.unread = True
                self.hub.save(session)
                channels = getattr(self.hub.e, 'channels', None)
                if channels:
                    await channels.notify({'type': 'channel_notice', 'channel': 'workbench', 'kind': 'resident',
                        'text': f"持续任务「{final['title']}」：{final['summary']}", 'session_id': session.id})
        except asyncio.CancelledError:
            if not self._closing and self.get(gid)['state'] == 'running':
                self._change(gid, lambda v: v.update(state='interrupted', requested=None,
                    spent=max(v['spent'], self._spent(session)), summary='本轮被取消；请检查实际产物后继续'))
            raise
        except Exception as exc:
            if self.get(gid)['state'] == 'running':
                await self._tell(self._change(gid, lambda v: v.update(state='blocked', requested=None,
                    spent=max(v['spent'], self._spent(session)), summary=f'执行未开始或失败：{str(exc)[:300]}')))

