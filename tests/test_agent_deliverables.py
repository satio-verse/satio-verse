"""File deliverables use disposable workspaces; no model or real command execution."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

import pytest

from wm.agent.deliverables import DeliveryStore


@pytest.fixture
def delivery(tmp_path):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    return DeliveryStore(tmp_path / 'agent'), workspace


def add_text(store, workspace, name='result.txt', text='成果正文', run='run-1', session='parent'):
    path = workspace / name
    path.write_text(text, encoding='utf-8')
    return store.register(session, run, workspace, name, label='结果文件')


def test_register_preserves_stable_file_id_and_stores_changed_snapshot_history(delivery):
    store, workspace = delivery
    item = add_text(store, workspace)
    assert item['kind'] == 'text' and item['label'] == '结果文件'
    assert item['registered']['sha256'] == hashlib.sha256('成果正文'.encode()).hexdigest()
    assert item['registered']['size'] == len('成果正文'.encode())
    assert item['registered']['mtime_ns'] > 0 and '_roots' not in item
    assert store.register('parent', 'run-1', workspace, 'result.txt')['id'] == item['id']
    assert DeliveryStore(store.home.parent).list('parent', 'run-1')['total'] == 1
    (workspace / 'result.txt').write_text('新版本', encoding='utf-8')
    second = store.register('parent', 'run-1', workspace, './result.txt')
    assert second['id'] == item['id'] and store.list('parent', 'run-1')['total'] == 1
    assert second['registered']['sha256'] != item['registered']['sha256']
    assert second['versions'][0]['sha256'] == item['registered']['sha256']
    assert second['created'] == item['created'] and second['updated'] >= item['updated']
    assert second['label'] == item['label']  # Automatic re-registration preserves the human label.
    assert store.inspect('parent', second['id'])['changed'] is False


def test_repeated_edits_use_one_slot_and_keep_only_twenty_previous_fingerprints(delivery, monkeypatch):
    from wm.agent import deliverables
    store, workspace = delivery
    monkeypatch.setattr(deliverables, 'MAX_ARTIFACTS', 1)
    item = add_text(store, workspace)
    hashes = [item['registered']['sha256']]
    for n in range(25):
        (workspace / 'result.txt').write_text(str(n), encoding='utf-8')
        current = store.register('parent', 'run-1', workspace, 'result.txt')
        assert current['id'] == item['id']
        hashes.append(current['registered']['sha256'])
    assert store.list('parent', 'run-1')['total'] == 1
    assert [version['sha256'] for version in current['versions']] == hashes[-21:-1]
    before = store.home.joinpath('parent', 'run-1.json').read_bytes()
    store.register('parent', 'run-1', workspace, 'result.txt')
    assert store.home.joinpath('parent', 'run-1.json').read_bytes() == before
    with pytest.raises(ValueError, match='最多登记'):
        add_text(store, workspace, 'another.txt')


def test_legacy_duplicate_ids_keep_old_references_and_update_newest_entry(delivery):
    store, workspace = delivery
    old = add_text(store, workspace)
    store.review('parent', 'run-1', accepted=True, artifact_ids=[old['id']])
    manifest = next(store.home.rglob('*.json'))
    state = json.loads(manifest.read_text('utf-8'))
    newer = json.loads(json.dumps(state['artifacts'][0]))
    newer.update(id='a-run-1-' + 'f' * 16, created=old['created'] + 1, updated=old['created'] + 1)
    state['artifacts'].append(newer)
    manifest.write_text(json.dumps(state), encoding='utf-8')
    (workspace / 'result.txt').write_text('new delivery', encoding='utf-8')
    current = store.register('parent', 'run-1', workspace, 'result.txt')
    assert current['id'] == newer['id'] and store.list('parent', 'run-1')['total'] == 2
    assert store.inspect('parent', old['id'])['registered'] == old['registered']
    assert store.state('parent', 'run-1', refresh=True)['reviews'][0]['stale'] is True


@pytest.mark.parametrize('filename,data,kind,mime', [
    ('picture.png', b'\x89PNG\r\n\x1a\n' + bytes(32), 'image', 'image/png'),
    ('report.pdf', b'%PDF-1.4\nexample', 'pdf', 'application/pdf'),
    ('binary.bin', b'\x00\x01\x02', 'file', 'application/octet-stream'),
])
def test_nontext_files_are_registered_without_copying_binary_content(delivery, filename, data, kind, mime):
    store, workspace = delivery
    (workspace / filename).write_bytes(data)
    item = store.register('parent', 'run-1', workspace, filename)
    assert (item['kind'], item['mime']) == (kind, mime)
    view = store.read('parent', item['id'])
    assert view['preview'] is None and view['status'] == 'current'
    assert [path.suffix for path in store.home.rglob('*') if path.is_file()] == ['.json']
    with pytest.raises(ValueError, match='实际类型'):
        store.register('parent', 'run-1', workspace, filename, kind='text')


def test_traversal_outside_workspace_and_wrong_owner_are_rejected(delivery, tmp_path):
    store, workspace = delivery
    outside = tmp_path / 'private.txt'
    outside.write_text('PRIVATE')
    for path in ['../private.txt', outside]:
        with pytest.raises(PermissionError, match='超出'):
            store.register('parent', 'run-1', workspace, path)
    with pytest.raises(OSError):
        store.register('parent', 'run-1', workspace, 'missing.txt')
    with pytest.raises((OSError, ValueError)):
        store.register('parent', 'run-1', workspace, '.')
    item = add_text(store, workspace)
    for session, run in [('other', None), ('parent', 'other-run')]:
        with pytest.raises(ValueError, match='不属于|不存在'):
            store.read(session, item['id'], run_id=run)
    for session in ['../parent', 'a/b', 'a\\b', '']:
        with pytest.raises(ValueError):
            store.list(session)
    with pytest.raises(ValueError):
        store.inspect('parent', '../run-1')


def test_caller_can_explicitly_allow_child_worktree_but_not_other_directories(delivery, tmp_path):
    store, workspace = delivery
    child = tmp_path / 'managed-child'
    child.mkdir()
    path = child / 'changes.md'
    path.write_text('独立工作副本中的成果', encoding='utf-8')
    with pytest.raises(PermissionError):
        store.register('parent', 'run-1', workspace, path)
    item = store.register('parent', 'run-1', workspace, path, allowed_roots=[child])
    assert store.read('parent', item['id'])['preview']['text'] == '独立工作副本中的成果'


def test_stored_path_outside_registered_roots_is_never_previewed(delivery, tmp_path):
    store, workspace = delivery
    item = add_text(store, workspace)
    outside = tmp_path / 'private.txt'
    outside.write_text('OUTSIDE SECRET')
    manifest = next(store.home.rglob('*.json'))
    state = json.loads(manifest.read_text('utf-8'))
    state['artifacts'][0]['path'] = str(outside)
    manifest.write_text(json.dumps(state), encoding='utf-8')
    view = store.read('parent', item['id'])
    assert view['status'] == 'unavailable' and view['preview'] is None and view['current'] is None
    assert 'OUTSIDE SECRET' not in json.dumps(view)


def test_symlink_cannot_register_or_later_preview_outside_allowed_roots(delivery, tmp_path):
    store, workspace = delivery
    outside = tmp_path / 'private.txt'
    outside.write_text('PRIVATE OUTSIDE CONTENT')
    link = workspace / 'link.txt'
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip('Account cannot create disposable symlinks')
    with pytest.raises(PermissionError):
        store.register('parent', 'run-1', workspace, link)
    link.unlink()
    inside = workspace / 'inside.txt'
    inside.write_text('public')
    link.symlink_to(inside)
    item = store.register('parent', 'run-1', workspace, link)
    link.unlink()
    link.symlink_to(outside)
    view = store.read('parent', item['id'])
    assert view['status'] == 'unavailable' and view['preview'] is None and view['current'] is None
    assert 'PRIVATE OUTSIDE CONTENT' not in json.dumps(view)


def test_preview_is_bounded_paged_and_reports_invalid_utf8(delivery):
    store, workspace = delivery
    text = '中文任务成果\n' * 300_000
    path = workspace / 'large.txt'
    path.write_bytes(b'\xef\xbb\xbf' + text.encode('utf-8') + b'\xff')
    item = store.register('parent', 'run-1', workspace, path)
    view = store.read('parent', item['id'], offset=1024, limit=75)
    assert view['preview']['text'] == text[1024:1099]
    assert view['preview']['next_offset'] == 1099 and view['preview']['truncated']
    assert view['preview']['invalid_bytes'] == 1 and view['preview']['encoding_note']
    end = store.read('parent', item['id'], offset=len(text), limit=75)
    assert end['preview']['text'] == '\ufffd' and end['preview']['next_offset'] is None
    manifest = next(store.home.rglob('*.json'))
    assert manifest.stat().st_size < 5000 and path.stat().st_size > 3_000_000
    assert text[:200] not in manifest.read_text('utf-8')
    with pytest.raises(ValueError):
        store.read('parent', item['id'], limit=20001)
    with pytest.raises(ValueError):
        store.read('parent', item['id'], offset=-1)


def test_inspect_reports_changed_or_missing_without_rewriting_registered_hash(delivery):
    store, workspace = delivery
    item = add_text(store, workspace)
    original = item['registered']['sha256']
    (workspace / 'result.txt').write_text('改变后的正文', encoding='utf-8')
    view = store.read('parent', item['id'])
    assert view['status'] == 'changed' and view['current']['sha256'] != original
    assert view['registered']['sha256'] == original and view['preview']['text'] == '改变后的正文'
    (workspace / 'result.txt').unlink()
    missing = store.inspect('parent', item['id'])
    assert missing['status'] == 'missing' and missing['current'] is None
    assert missing['registered']['sha256'] == original


def test_execution_evidence_cannot_convert_model_text_or_unknown_exit_into_success(delivery):
    store, workspace = delivery
    item = add_text(store, workspace)
    with pytest.raises(ValueError):
        store.record_evidence('parent', 'run-1', tool_call_id='t', command='pytest', exit_code='0')
    with pytest.raises(ValueError):
        store.record_evidence('parent', 'run-1', tool_call_id='t', command='pytest', exit_code=True)
    for code, status in [(0, 'succeeded'), (1, 'failed'), (None, 'incomplete')]:
        evidence = store.record_evidence('parent', 'run-1', tool_call_id=f't-{code}', command='python -m pytest',
                                        exit_code=code, output='模型声称全部通过', artifact_ids=[item['id']])
        assert evidence['status'] == status and evidence['scope'] == 'command_execution_only'
        assert evidence['source'] == 'tool_result' and evidence['artifact_snapshots'][0]['sha256'] == item['registered']['sha256']
    state = store.state('parent', 'run-1')
    assert len(state['evidence']) == 3 and not state['reviews']
    assert 'accepted' not in state['artifacts'][0]
    with pytest.raises(ValueError, match='不属于'):
        store.record_evidence('parent', 'other-run', tool_call_id='t', command='pytest', exit_code=0, artifact_ids=[item['id']])


def test_output_excerpt_keeps_both_ends_with_explicit_truncation(delivery):
    store, _ = delivery
    evidence = store.record_evidence('parent', 'run-1', tool_call_id='real-id', command='pytest', exit_code=1,
                                    output='START' + 'x' * 100000 + 'FINAL ERROR')
    assert evidence['output'].startswith('START') and evidence['output'].endswith('FINAL ERROR')
    assert evidence['output_truncated'] and len(evidence['output']) < 6100


def test_human_review_binds_current_snapshot_and_becomes_stale_when_file_changes(delivery):
    store, workspace = delivery
    item = add_text(store, workspace)
    accepted = store.review('parent', 'run-1', accepted=True, note='人工核对内容符合要求')
    assert accepted['source'] == 'user' and accepted['accepted'] is True
    assert accepted['artifact_snapshots'][0]['sha256'] == item['registered']['sha256']
    assert store.state('parent', 'run-1', refresh=True)['reviews'][0]['stale'] is False
    (workspace / 'result.txt').write_text('changed', encoding='utf-8')
    assert store.state('parent', 'run-1', refresh=True)['reviews'][0]['stale'] is True
    renewed = store.review('parent', 'run-1', accepted=True, artifact_ids=[item['id']])
    assert renewed['artifact_snapshots'][0]['status'] == 'changed'
    assert renewed['artifact_snapshots'][0]['sha256'] != item['registered']['sha256']
    state = store.state('parent', 'run-1', refresh=True)
    assert [r['stale'] for r in state['reviews']] == [True, False]
    (workspace / 'result.txt').unlink()
    with pytest.raises(ValueError, match='不能登记为已验收'):
        store.review('parent', 'run-1', accepted=True)
    rejected = store.review('parent', 'run-1', accepted=False, note='文件已丢失')
    assert rejected['accepted'] is False and rejected['artifact_snapshots'][0]['status'] == 'missing'


def test_pagination_and_returned_objects_do_not_mutate_metadata(delivery):
    store, workspace = delivery
    first = add_text(store, workspace, 'a.txt', run='run-1')
    second = add_text(store, workspace, 'b.txt', run='run-2')
    page = store.list('parent', limit=1)
    assert page['total'] == 2 and page['items'][0]['id'] == second['id'] and page['next_offset'] == 1
    tail = store.list('parent', offset=1, limit=1)
    assert tail['items'][0]['id'] == first['id'] and tail['next_offset'] is None
    page['items'][0]['registered']['sha256'] = 'tampered'
    assert store.inspect('parent', second['id'])['registered']['sha256'] != 'tampered'
    assert store.list('other')['total'] == 0
    with pytest.raises(ValueError):
        store.list('parent', limit=101)


def test_review_rejects_changed_preview_version_and_exact_artifact_set_is_required(delivery):
    store, workspace = delivery
    item = add_text(store, workspace)
    expected = {item['id']: item['registered']['sha256']}
    accepted = store.review('parent', 'run-1', accepted=True, expected_versions=expected)
    evidence = store.record_evidence('parent', 'run-1', tool_call_id='real-tool', command='pytest', exit_code=0,
                                    artifact_ids=[item['id']])
    assert accepted['accepted'] is True
    (workspace / 'result.txt').write_text('changed after preview', encoding='utf-8')
    updated = store.register('parent', 'run-1', workspace, 'result.txt')
    assert updated['id'] == item['id']
    state = store.state('parent', 'run-1', refresh=True)
    assert state['reviews'][0]['stale'] is True
    assert state['reviews'][0]['artifact_snapshots'] == accepted['artifact_snapshots']
    assert state['evidence'][0]['artifact_snapshots'] == evidence['artifact_snapshots']
    for stale in [expected, {}, {**expected, 'foreign-artifact': 'a' * 64}, {item['id']: None}]:
        with pytest.raises(ValueError, match='成果已变化，请重新预览后验收'):
            store.review('parent', 'run-1', accepted=True, expected_versions=stale)
    assert len(store.state('parent', 'run-1')['reviews']) == 1
    current = store.read('parent', item['id'])['current']['sha256']
    renewed = store.review('parent', 'run-1', accepted=True, expected_versions={item['id']: current})
    assert renewed['artifact_snapshots'][0]['sha256'] == current


def test_metadata_corruption_and_failed_atomic_save_never_erase_old_records(delivery, monkeypatch):
    from wm.agent import deliverables
    store, workspace = delivery
    add_text(store, workspace)
    manifest = next(store.home.rglob('*.json'))
    before = manifest.read_bytes()
    def failed_replace(*_):
        raise OSError('simulated write failure')
    monkeypatch.setattr(deliverables.os, 'replace', failed_replace)
    with pytest.raises(OSError):
        store.review('parent', 'run-1', accepted=True)
    assert manifest.read_bytes() == before and not list(store.home.rglob('*.tmp'))
    manifest.write_text('{"corrupt": true}', encoding='utf-8')
    with pytest.raises(ValueError, match='无法读取'):
        store.list('parent', 'run-1')
    assert 'corrupt' in manifest.read_text('utf-8')


def test_two_instances_preserve_concurrent_deliverable_registration(delivery):
    store, workspace = delivery
    peer = DeliveryStore(store.home.parent)
    for n in range(12):
        (workspace / f'{n}.txt').write_text(str(n))
    def register(n):
        return (store if n % 2 else peer).register('parent', 'run-1', workspace, f'{n}.txt')
    with ThreadPoolExecutor(max_workers=4) as pool:
        items = list(pool.map(register, range(12)))
    assert len({item['id'] for item in items}) == 12
    assert store.list('parent', 'run-1')['total'] == 12
