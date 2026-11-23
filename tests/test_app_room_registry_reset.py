"""Reset routing must invalidate every scene, while preserving legacy engines."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from wm import app as api


class Owner:
    def __init__(self):
        self.calls = []
        self.generation = 0

    def forget(self, generation):
        self.calls.append(('forget', generation))
        return ['scene-memory.json']

    def set_generation(self, generation):
        self.calls.append(('generation', generation))
        self.generation = generation


def environment(tmp_path, registry):
    room = Owner()
    engines = SimpleNamespace(room=room, memory=None)
    if registry is not None:
        engines.room_registry = registry
    app = {api.CFG: {'room': {'cognition': {'data_dir': str(tmp_path / 'experience')}},
                     'trace': {'log_dir': str(tmp_path / 'logs')}},
           api.ENGINES: engines, api.SESSIONS: set()}
    return app, room


@pytest.mark.parametrize('use_registry', [True, False])
@pytest.mark.parametrize('scope', ['page', 'experience'])
def test_reset_routes_generation_and_forget(tmp_path, use_registry, scope):
    registry = Owner() if use_registry else None
    app, room = environment(tmp_path, registry)
    result = asyncio.run(api._reset_room_learning(app, [scope]))
    assert result['generation'] == 1
    if use_registry:
        assert registry.calls == [('generation' if scope == 'page' else 'forget', 1)]
        assert room.calls == [] and room.generation == 0
    elif scope == 'experience':
        assert room.calls == [('forget', 1)]
    else:
        assert room.generation == 1


@pytest.mark.parametrize('use_registry', [True, False])
def test_delete_experience_routes_to_all_scenes(tmp_path, monkeypatch, use_registry):
    registry = Owner() if use_registry else None
    app, room = environment(tmp_path, registry)
    monkeypatch.setattr(api, 'ws_refusal', lambda request: None)

    class Request:
        method = 'POST'
        async def json(self):
            return {'action': 'delete', 'confirm': True}

    request = Request()
    request.app = app
    response = asyncio.run(api.api_experience(request))
    assert response.status == 200
    assert json.loads(response.body)['deleted'] == ['scene-memory.json']
    assert (registry or room).calls == [('forget', 1)]
    if registry:
        assert room.calls == []
