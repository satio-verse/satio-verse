"""Capture the real trial launcher; never start a server or read production state."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from wm import workbench as W


@pytest.mark.parametrize('healthy', [True, False], ids=['ready', 'timeout'])
def test_trial_disables_resident_and_uses_disposable_agent_home(tmp_path, monkeypatch, healthy):
    project = tmp_path / 'candidate'
    (project / 'wm').mkdir(parents=True)
    (project / 'wm' / '__main__.py').write_text('# Trial entry point marker only.\n', encoding='utf-8')
    production = tmp_path / 'production-home'
    production.mkdir()
    sentinel = production / 'resident.json'
    sentinel.write_text('{"goal":"must remain untouched"}', encoding='utf-8')
    monkeypatch.setattr(W, 'HOME', production)

    captured = {}
    real_temporary_directory = W.tempfile.TemporaryDirectory

    def temporary_directory():
        # The production code still creates/cleans a real isolated directory.
        handle = real_temporary_directory(prefix='trial-', dir=tmp_path)
        captured['temporary'] = Path(handle.name)
        return handle

    class Process:
        def __init__(self):
            self.terminated = False
            self.waited = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            assert timeout == 10 and self.terminated
            self.waited = True
            return 0

    process = Process()

    def popen(argv, **kwargs):
        captured.update(argv=list(argv), kwargs=kwargs)
        assert Path(kwargs['stdout'].name).parent == captured['temporary']
        kwargs['stdout'].write(b'trial-only-log\n')
        return process

    statuses = iter([False, healthy])  # Port probe, then this trial's readiness.
    probes = []

    def status_ok(port, timeout):
        probes.append((port, timeout))
        return next(statuses)

    ticks = iter([10.0, 11.0, 200.0, 201.0])
    monkeypatch.setattr(W.tempfile, 'TemporaryDirectory', temporary_directory)
    monkeypatch.setattr(W.subprocess, 'Popen', popen)
    monkeypatch.setattr(W, 'status_ok', status_ok)
    monkeypatch.setattr(W, 'time', SimpleNamespace(time=lambda: next(ticks), sleep=lambda _: None))

    ok, description = W.trial(project, port=19876, timeout=120)
    assert ok is healthy
    assert probes == [(19876, 1), (19876, 2)]
    argv = captured['argv']
    settings = [argv[index + 1] for index, word in enumerate(argv) if word == '--set']
    values = dict(setting.split('=', 1) for setting in settings)
    assert values['agent.resident_enabled'] == 'false'
    assert sum(setting.startswith('agent.resident_enabled=') for setting in settings) == 1
    assert sum(setting.startswith('agent.home=') for setting in settings) == 1
    trial_home = Path(values['agent.home'])
    assert trial_home == captured['temporary'] / 'agent'
    assert trial_home != production and not trial_home.is_relative_to(production)
    assert trial_home != Path.home() / '.worldmodel' / 'agent'
    assert Path(values['trace.log_dir']) == captured['temporary'] / 'logs'
    assert values['channels.qq.enabled'] == 'false'
    assert values['memory.vault'] == ''
    assert captured['kwargs']['cwd'] == str(project)
    assert process.terminated and process.waited
    assert not captured['temporary'].exists()
    assert sentinel.read_text(encoding='utf-8') == '{"goal":"must remain untouched"}'
    assert sorted(path.name for path in production.iterdir()) == ['resident.json']
    if not healthy:
        assert 'trial-only-log' in description
