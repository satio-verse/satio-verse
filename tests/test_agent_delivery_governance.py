"""Delivery evidence defaults and one-time upgrades; all files are temporary."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from wm.agent.governance import DEFAULT_RULES_VERSION, BlacklistStore, _validate


def newer_count(store):
    """Rules added after v2: v3 (2026-11-11 插件宿主第 0 步) write-protected extension/MCP control files and elevation
    command rules, v4 (第三小步) the server's own state files, v5 (S1 心跳) the heartbeat ledger directory, v6 (平台化
    第三步①) the third-party plugin directory (counted from the store so later additions to those lists don't break this
    file)."""
    return (len(store._extension_defaults()) + len(store._elevation_defaults()) + len(store._state_defaults())
            + len(store._heartbeat_defaults()) + len(store._plugins_defaults()))


def state_of(store):
    return json.loads(store.path.read_text('utf-8'))


def as_legacy_default_profile(tmp_path):
    store = BlacklistStore(tmp_path / 'agent')
    state = state_of(store)
    # A pre-v1 profile: neither the v1 evidence rules, the v2 control rules (hooks.json, AGENTS.md), the v3
    # extension/elevation rules, the v4 state-file rules, the v5 heartbeat directory nor the v6 plugin directory existed.
    targets = {str(store.home / name) for name in ('deliveries', 'runs', 'hooks.json', 'AGENTS.md')}
    newer = {(rule['kind'], _validate(rule)['value'])
             for rule in [*store._extension_defaults(), *store._elevation_defaults(), *store._state_defaults(),
                          *store._heartbeat_defaults(), *store._plugins_defaults()]}
    state['rules'] = [rule for rule in state['rules']
                      if rule['value'] not in targets and (rule['kind'], rule['value']) not in newer]
    state.pop('default_rules_version')
    store.path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    return store, state


def test_fresh_default_protection_blocks_evidence_writes_but_allows_reads_and_outputs(tmp_path):
    store = BlacklistStore(tmp_path / 'agent')
    for name in ('deliveries', 'runs'):
        path = store.home / name / 'result.json'
        rule = next(rule for rule in store.list() if rule['value'] == str(store.home / name))
        assert rule['origin'] == 'default' and rule['operations'] == 'write'
        assert store.check('write_file', {'path': str(path)}, tmp_path).how == 'deny'
        assert store.check('read_file', {'path': str(path)}, tmp_path) is None
    assert store.check('write_file', {'path': str(tmp_path / 'project' / 'report.md')}, tmp_path) is None
    assert store.snapshot()['revision'] == 1


def test_old_default_profile_migrates_once_with_single_revision_increment(tmp_path):
    old, previous = as_legacy_default_profile(tmp_path)
    old_ids = {rule['id'] for rule in previous['rules']}
    migrated = BlacklistStore(old.home)
    state = state_of(migrated)
    assert state['version'] == 1 and state['default_rules_version'] == DEFAULT_RULES_VERSION >= 3
    assert state['revision'] == previous['revision'] + 1
    assert len(state['rules']) == len(previous['rules']) + 4 + newer_count(old)
    assert old_ids <= {rule['id'] for rule in state['rules']}
    audit = migrated.audit()[0]
    assert audit['action'] == 'migrate_defaults' and len(audit['added']) == 4 + newer_count(old) and not audit['skipped']
    before = migrated.path.read_bytes()
    BlacklistStore(old.home).snapshot()
    assert migrated.path.read_bytes() == before
    assert set(migrated.snapshot()) == {'version', 'revision', 'global_unrestricted', 'rules', 'audit'}


def test_removing_new_default_does_not_resurrect_even_if_audit_is_compacted(tmp_path):
    old, _ = as_legacy_default_profile(tmp_path)
    store = BlacklistStore(old.home)
    rule = next(rule for rule in store.list() if rule['value'] == str(store.home / 'deliveries'))
    store.remove(rule['id'])
    state = state_of(store)
    state['audit'] = []
    store.path.write_text(json.dumps(state), encoding='utf-8')
    before = store.path.read_bytes()
    again = BlacklistStore(old.home)
    assert again.check('write_file', {'path': str(old.home / 'deliveries' / 'x.json')}, tmp_path) is None
    assert again.path.read_bytes() == before


@pytest.mark.parametrize('choice', ['removed', 'disabled', 'narrowed'])
def test_legacy_user_choices_are_not_overridden_by_new_overlap_defaults(tmp_path, choice):
    old, _ = as_legacy_default_profile(tmp_path)
    # defaults=False edits existing legacy data without initiating migrations.
    editor = BlacklistStore(old.home, defaults=False)
    target = str(old.home / 'deliveries')
    rule = editor.add({'kind': 'path', 'value': target, 'operations': 'write'})
    if choice == 'removed':
        editor.remove(rule['id'])
    elif choice == 'disabled':
        editor.update(rule['id'], {'enabled': False})
    else:
        editor.update(rule['id'], {'operations': 'read'})
    migrated = BlacklistStore(old.home)
    assert migrated.check('write_file', {'path': target + '/x.json'}, tmp_path) is None
    assert migrated.check('write_file', {'path': str(old.home / 'runs' / 'x.json')}, tmp_path).how == 'deny'
    audit = migrated.audit()[0]
    assert audit['action'] == 'migrate_defaults' and len(audit['added']) == 3 + newer_count(old) and len(audit['skipped']) == 1


def test_existing_covering_rule_is_preserved_without_duplicate_or_source_rewrite(tmp_path):
    old, _ = as_legacy_default_profile(tmp_path)
    editor = BlacklistStore(old.home, defaults=False)
    existing = editor.add({'kind': 'path', 'value': str(old.home / 'deliveries'), 'operations': 'all', 'reason': '自己设置'}, by='assistant')
    migrated = BlacklistStore(old.home)
    matching = [r for r in migrated.list() if r['value'] == existing['value']]
    assert matching == [existing]


def test_defaults_false_never_migrates_legacy_files_or_new_opt_out_stores(tmp_path):
    old, _ = as_legacy_default_profile(tmp_path)
    before = old.path.read_bytes()
    reader = BlacklistStore(old.home, defaults=False)
    assert reader.path.read_bytes() == before
    assert not any(r['value'] == str(old.home / 'deliveries') for r in reader.list())
    opted_out = BlacklistStore(tmp_path / 'no-defaults', defaults=False)
    before = opted_out.path.read_bytes()
    assert BlacklistStore(opted_out.home).list() == []
    assert opted_out.path.read_bytes() == before


def test_truly_legacy_custom_store_has_no_default_profile_and_stays_untouched(tmp_path):
    store = BlacklistStore(tmp_path / 'custom', defaults=False)
    store.add({'value': '用户自己的偏好'})
    state = state_of(store)
    state.pop('default_rules_version')
    store.path.write_text(json.dumps(state), encoding='utf-8')
    before = store.path.read_bytes()
    assert len(BlacklistStore(store.home).list()) == 1
    assert store.path.read_bytes() == before


def test_user_cleared_default_profile_remains_empty_and_only_marks_migration(tmp_path):
    old, _ = as_legacy_default_profile(tmp_path)
    editor = BlacklistStore(old.home, defaults=False)
    for rule in editor.list():
        editor.remove(rule['id'])
    revision = editor.snapshot()['revision']
    migrated = BlacklistStore(old.home)
    assert migrated.list() == [] and migrated.snapshot()['revision'] == revision + 1
    assert len(migrated.audit()[0]['skipped']) == 4 + newer_count(old)
    before = migrated.path.read_bytes()
    BlacklistStore(old.home)
    assert migrated.path.read_bytes() == before


def test_failed_migration_save_preserves_original_file_and_can_retry(tmp_path, monkeypatch):
    from wm.agent import governance
    old, previous = as_legacy_default_profile(tmp_path)
    before = old.path.read_bytes()
    original = governance.os.replace
    def fail(*_):
        raise OSError('simulated atomic-save failure')
    monkeypatch.setattr(governance.os, 'replace', fail)
    with pytest.raises(OSError):
        BlacklistStore(old.home)
    assert old.path.read_bytes() == before and not list(old.home.glob('*.tmp'))
    monkeypatch.setattr(governance.os, 'replace', original)
    assert BlacklistStore(old.home).snapshot()['revision'] == previous['revision'] + 1


def test_parallel_instances_only_migrate_once(tmp_path):
    old, previous = as_legacy_default_profile(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        snapshots = list(pool.map(lambda _: BlacklistStore(old.home).snapshot(), range(8)))
    assert all(state['revision'] == previous['revision'] + 1 for state in snapshots)
    assert sum(event['action'] == 'migrate_defaults' for event in snapshots[-1]['audit']) == 1


@pytest.mark.parametrize('version', [None, True, -1, '1'])
def test_invalid_default_migration_metadata_cannot_reset_migration(tmp_path, version):
    store = BlacklistStore(tmp_path / 'agent')
    state = state_of(store)
    state['default_rules_version'] = version
    store.path.write_text(json.dumps(state), encoding='utf-8')
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match='读取失败'):
        BlacklistStore(store.home)
    assert store.path.read_bytes() == before
