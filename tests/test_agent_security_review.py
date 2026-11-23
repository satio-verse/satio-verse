"""Offline preset and bundled-skill integration; no samples or services run."""
import json
from pathlib import Path

import pytest

from wm.agent.core import AgentHub, Session
from wm.agent.extensions import ExtensionRegistry


@pytest.mark.parametrize('mode', ['read', 'unrestricted'])
def test_review_selection_preserves_session_authorization(tmp_path, mode):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    hub = AgentHub({'agent': {'workspace': str(workspace), 'extensions': {'settings_path': False}}},
                   home=tmp_path / 'agent')
    session = hub.new_session()
    hub.settings(session.id, mode=mode)
    hub.blacklist.add({'kind': 'path', 'value': str(workspace / 'private'),
                       'reason': 'test', 'operations': 'read'})
    before_tools, before_rules = hub._offered(session), hub.blacklist.list()

    hub.settings(session.id, preset='security-review')

    assert session.mode == mode
    assert hub._offered(session) == before_tools
    assert hub.blacklist.list() == before_rules
    assert hub.state()['presets']['security-review'] == '安全审查'
    assert hub.presets.get('security-review')['instructions'] in hub._system(session)
    assert Session.from_json(session.to_json()).preset == 'security-review'


def test_bundled_review_skill_respects_persona_binding(tmp_path):
    settings = tmp_path / 'pet-settings.json'
    profile = {'active': 'reviewer', 'personas': [{'id': 'reviewer', 'skills': []}]}
    settings.write_text(json.dumps(profile), encoding='utf-8')
    skills = Path(__file__).resolve().parents[1] / 'desktop' / 'skills'
    registry = ExtensionRegistry(tmp_path / 'agent', settings_path=settings,
                                 skill_roots=[skills], self_mcp_root=False)
    assert 'security-review' not in {row['id'] for row in registry.skill_index()}

    profile['personas'][0]['skills'].append('security-review')
    settings.write_text(json.dumps(profile), encoding='utf-8')
    skill = registry.read_skill('security-review')

    assert skill['name'] == 'security-review'
    assert skill['description'] and skill['source'] == 'desktop'
    assert Path(skill['path']).name == 'SKILL.md'
