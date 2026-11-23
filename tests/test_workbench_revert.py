"""工作台改版 (2026-11-01): 对话里"撤销"一轮的文件改动、Ctrl+P 全工作区找文件. 全在 tmp_path 里."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wm.agent.governance import BlacklistStore
from wm.agent.workspace_http import find_files, revert_changes, workspace


def _session(root, events=None, status='idle'):
    return SimpleNamespace(id='a', workspace=str(root), mode='workspace', preset='standard', status=status, task=None,
                           events=list(events or []))


def _change(cid, path, before, after):
    return {'type': 'tool_result', 'id': cid, 'ok': True, 'text': 'ok',
            'change': {'path': str(path), 'before': before, 'after': after}}


def test_revert_restores_exact_bytes_for_lf_crlf_and_bom(tmp_path):
    root = tmp_path / 'ws'
    root.mkdir()
    lf, crlf, bom = root / 'a.py', root / 'b.txt', root / 'c.md'
    lf.write_bytes(b'x = 2\nprint(x)\n')
    crlf.write_bytes(b'one\r\nTWO\r\nthree\r\n')
    bom.write_bytes('﻿# 标题\n新的\n'.encode('utf-8'))
    s = _session(root, [_change('c1', lf, 'x = 1\nprint(x)\n', 'x = 2\nprint(x)\n'),
                        _change('c2', crlf, 'one\ntwo\nthree\n', 'one\nTWO\nthree\n'),
                        _change('c3', bom, '﻿# 标题\n旧的\n', '﻿# 标题\n新的\n')])
    result = revert_changes(s, ['c1', 'c2', 'c3'])
    assert result['ok'] and sorted(r['path'] for r in result['reverted']) == ['a.py', 'b.txt', 'c.md']
    assert lf.read_bytes() == b'x = 1\nprint(x)\n'
    assert crlf.read_bytes() == b'one\r\ntwo\r\nthree\r\n'
    assert bom.read_bytes() == '﻿# 标题\n旧的\n'.encode('utf-8')
    assert not list(root.glob('*.wm-revert'))


def test_revert_accepts_change_records_that_keep_crlf(tmp_path):
    """2026-11-07 起改动记录原样带 CRLF (以前统一成 \n): 核对按同一规则统一换行, 写回照文件现在的换行, 不会变成 \r\r\n."""
    root = tmp_path / 'ws'
    root.mkdir()
    crlf, mixed_lf = root / 'app.py', root / 'notes.md'
    crlf.write_bytes(b"def main():\r\n    print('hello')\r\n")
    mixed_lf.write_bytes(b'new\n')
    s = _session(root, [_change('c1', crlf, "def main():\r\n    print('hi')\r\n", "def main():\r\n    print('hello')\r\n"),
                        _change('c2', mixed_lf, 'old\n', 'new\n')])
    result = revert_changes(s, ['c1', 'c2'])
    assert result['ok'] and sorted(r['path'] for r in result['reverted']) == ['app.py', 'notes.md'], result
    assert crlf.read_bytes() == b"def main():\r\n    print('hi')\r\n"
    assert mixed_lf.read_bytes() == b'old\n'


def test_revert_checks_everything_first_and_touches_nothing_when_one_file_changed_later(tmp_path):
    root = tmp_path / 'ws'
    root.mkdir()
    a, b = root / 'a.txt', root / 'b.txt'
    a.write_text('after-a\n', encoding='utf-8')
    b.write_text('edited again by hand\n', encoding='utf-8')
    s = _session(root, [_change('c1', a, 'before-a\n', 'after-a\n'), _change('c2', b, 'before-b\n', 'after-b\n')])
    result = revert_changes(s, ['c1', 'c2'])
    assert result['ok'] is False and result['blocked'] == [{'path': 'b.txt', 'why': '之后又被改过，为了不盖掉新的内容没有撤销'}]
    assert a.read_text(encoding='utf-8') == 'after-a\n' and b.read_text(encoding='utf-8') == 'edited again by hand\n'


def test_revert_several_edits_to_one_file_and_archives_created_files(tmp_path):
    root = tmp_path / 'ws'
    (root / 'docs').mkdir(parents=True)
    f, new = root / 'f.txt', root / 'docs' / 'new.md'
    f.write_text('v3\n', encoding='utf-8')
    new.write_text('# 新文件\n', encoding='utf-8')
    s = _session(root, [_change('c1', f, 'v1\n', 'v2\n'), _change('c2', new, '', '# 新文件\n'), _change('c3', f, 'v2\n', 'v3\n')])
    result = revert_changes(s, ['c1', 'c2', 'c3'])
    assert result['ok']
    assert f.read_text(encoding='utf-8') == 'v1\n'
    assert result['reverted'] == [{'path': 'f.txt', 'calls': ['c1', 'c3']}]
    assert not new.exists()
    (archived,) = result['archived']
    assert archived['path'] == 'docs/new.md' and archived['archive'].startswith('.archive/撤销-')
    assert (root / archived['archive']).read_text(encoding='utf-8') == '# 新文件\n'


def test_revert_refuses_unknown_calls_outside_paths_blacklist_and_non_utf8(tmp_path):
    root = tmp_path / 'ws'
    root.mkdir()
    outside = tmp_path / 'outside.txt'
    outside.write_text('after\n', encoding='utf-8')
    inside = root / 'locked.txt'
    inside.write_text('after\n', encoding='utf-8')
    odd = root / 'odd.txt'
    odd.write_text('after\n', encoding='utf-8')
    s = _session(root, [_change('out', outside, 'before\n', 'after\n'), _change('lock', inside, 'before\n', 'after\n'),
                        _change('odd', odd, 'b�d\n', 'after\n'), {'type': 'tool_result', 'id': 'nochange', 'ok': True}])
    with pytest.raises(ValueError, match='找不到'):
        revert_changes(s, ['nochange'])
    with pytest.raises(ValueError):
        revert_changes(s, [])
    blacklist = BlacklistStore(tmp_path / 'state', defaults=False)
    blacklist.add({'kind': 'path', 'value': str(inside), 'operations': 'write'})
    result = revert_changes(s, ['out', 'lock', 'odd'], blacklist=blacklist)
    reasons = {row['path']: row['why'] for row in result['blocked']}
    assert result['ok'] is False and set(reasons) == {str(outside.resolve()), 'locked.txt', 'odd.txt'}
    assert reasons[str(outside.resolve())] == '不在当前工作区里' and 'UTF-8' in reasons['odd.txt']
    assert outside.read_text(encoding='utf-8') == inside.read_text(encoding='utf-8') == 'after\n'


def test_find_files_matches_terms_skips_dependency_folders_and_blacklist(tmp_path):
    root = tmp_path / 'ws'
    for name in ('src/app/main.py', 'src/app/mainframe.txt', 'docs/main-notes.md', 'node_modules/pkg/main.js',
                 '.git/objects/main', 'secret/main-key.txt', 'README.md'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('x', encoding='utf-8')
    blacklist = BlacklistStore(tmp_path / 'state', defaults=False)
    blacklist.add({'kind': 'path', 'value': str(root / 'secret'), 'operations': 'read'})
    s = _session(root)
    found = [row['path'] for row in find_files(s, 'main', blacklist=blacklist)['items']]
    assert found[0] == 'src/app/main.py' and 'docs/main-notes.md' in found and 'src/app/mainframe.txt' in found
    assert not any(p.startswith(('node_modules', '.git', 'secret')) for p in found)
    assert [row['path'] for row in find_files(s, 'APP MAIN.PY')['items']] == ['src/app/main.py']
    assert {row['path'] for row in find_files(s, '')['items']} >= {'README.md', 'src/app/main.py'}
    with pytest.raises(ValueError):
        find_files(s, 'x' * 201)


def test_http_find_and_revert_note_and_busy_refusal(tmp_path):
    root = tmp_path / 'ws'
    root.mkdir()
    f = root / 'a.txt'
    f.write_text('after\n', encoding='utf-8')
    s = _session(root, [_change('c1', f, 'before\n', 'after\n')])
    emitted, saved = [], []

    async def emit(session, event):
        emitted.append(event)
        session.events.append(event)

    hub = SimpleNamespace(get={'a': s}.get, blacklist=BlacklistStore(tmp_path / 'state', defaults=False),
                          home=tmp_path / 'agent', extensions=None, emit=emit, save=saved.append)

    async def handler(request):
        return await workspace(request, hub)

    async def main():
        app = web.Application()
        app.router.add_route('*', '/api/agent/workspace', handler)
        async with TestClient(TestServer(app)) as client:
            found = await (await client.get('/api/agent/workspace', params={'action': 'find', 'id': 'a', 'q': 'a.t'})).json()
            assert [row['path'] for row in found['items']] == ['a.txt']
            s.status = 'running'
            busy = await client.post('/api/agent/workspace', json={'action': 'revert', 'id': 'a', 'calls': ['c1']})
            assert busy.status == 400 and '做完再撤销' in (await busy.json())['error']
            assert f.read_text(encoding='utf-8') == 'after\n'
            s.status = 'idle'
            done = await (await client.post('/api/agent/workspace', json={'action': 'revert', 'id': 'a', 'calls': ['c1']})).json()
            assert done['ok'] and f.read_text(encoding='utf-8') == 'before\n'
            assert emitted and emitted[-1]['type'] == 'note' and 'a.txt' in emitted[-1]['text'] and saved == [s]
            assert emitted[-1]['reverted'] == ['c1']  # 页面靠它把那张卡片标成"已撤销"
            again = await (await client.post('/api/agent/workspace', json={'action': 'revert', 'id': 'a', 'calls': ['c1']})).json()
            assert again['ok'] is False and again['blocked'][0]['path'] == 'a.txt'
    asyncio.run(main())
