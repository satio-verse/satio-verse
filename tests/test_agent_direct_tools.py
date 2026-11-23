"""扩展工具直通：快照必须绑定当前启用的版本，只读模式只放只读工具，审批策略跟权限模式一致。"""
import json
from types import SimpleNamespace

from wm.agent.studio import StudioRuntime


class Presets:
    def get(self, name):
        return {'tools': ['*']}


class Teams:
    mode = 'unrestricted'

    def effective_mode(self, s):
        return self.mode


class Blacklist:
    def check(self, name, args, path):
        return None


class Hub:
    def __init__(self, home):
        self.home = home
        self.presets = Presets()
        self.teams = Teams()
        self.blacklist = Blacklist()
        self.extensions = SimpleNamespace(server=lambda sid: {'command': 'python', 'args': ['server.py'], 'cwd': str(home)})


class FakeEvent:
    def is_set(self):
        return False


class Session:
    preset = 'standard'
    workspace = r'E:\tmp'
    stop_event = FakeEvent()


def build(tmp_path, revision='r1', active='r1', enabled=True, tools=None):
    root = tmp_path / 'extensions'
    root.mkdir()
    (root / 'registry.json').write_text(json.dumps({'version': 1, 'packages': {'demo': {
        'id': 'demo', 'enabled': enabled, 'active_revision': active, 'latest_revision': active,
        'revisions': [active]}}}), encoding='utf-8')
    (root / 'direct-tools.json').write_text(json.dumps({'servers': {'creator:demo': {
        'revision': revision, 'label': '演示', 'tools': tools or [
            {'name': 'demo_read', 'description': '只读演示', 'inputSchema': {'type': 'object', 'properties': {}},
             'read_only': True, 'label': '只读演示'},
            {'name': 'demo_write', 'description': '写入演示', 'inputSchema': {'type': 'object', 'properties': {}},
             'read_only': False, 'label': '写入演示'}]}}}), encoding='utf-8')
    return Hub(tmp_path)


def test_snapshot_must_match_active_revision(tmp_path):
    hub = build(tmp_path, revision='old')
    studio = StudioRuntime(hub)
    assert 'demo_read' not in studio.names(Session())
    assert not [x for x in studio.tools(Session()) if x['function']['name'].startswith('demo_')]


def test_disabled_extension_exposes_nothing(tmp_path):
    hub = build(tmp_path, enabled=False)
    studio = StudioRuntime(hub)
    assert 'demo_read' not in studio.names(Session())


def test_tools_and_labels(tmp_path):
    studio = StudioRuntime(build(tmp_path))
    specs = {x['function']['name']: x['function'] for x in studio.tools(Session())}
    assert 'demo_read' in specs and 'demo_write' in specs
    assert specs['demo_read']['parameters']['type'] == 'object'
    assert studio.direct.label('demo_read') == '只读演示'
    assert studio.direct.server_for('demo_write') == 'creator:demo'


def test_read_mode_only_exposes_read_only_tools(tmp_path):
    hub = build(tmp_path)
    hub.teams.mode = 'read'
    studio = StudioRuntime(hub)
    names = studio.names(Session())
    assert 'demo_read' in names and 'demo_write' not in names


def test_decision_follows_permission_mode(tmp_path):
    hub = build(tmp_path)
    studio = StudioRuntime(hub)
    assert studio.decision(Session(), 'demo_read', {}).how == 'auto'
    assert studio.decision(Session(), 'demo_write', {}).how == 'auto'
    hub.teams.mode = 'ask'
    assert studio.decision(Session(), 'demo_read', {}).how == 'auto'
    assert studio.decision(Session(), 'demo_write', {}).how == 'ask'


def test_broken_snapshot_is_ignored(tmp_path):
    hub = build(tmp_path)
    (tmp_path / 'extensions' / 'direct-tools.json').write_text('{不是 JSON', encoding='utf-8')
    studio = StudioRuntime(hub)
    assert 'demo_read' not in studio.names(Session())
