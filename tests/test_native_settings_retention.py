"""Actual native channel mapper -> backend parser, with synthetic settings only."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from wm.channels.manager import ConfigError, parse_conf


@pytest.fixture(scope="module")
def native_map(tmp_path_factory):
    compiler = shutil.which("g++")
    if not compiler:
        pytest.skip("g++ required for native settings regression")
    root = Path(__file__).resolve().parents[1]
    source = (root / "desktop/host/src/settings.cpp").read_text(encoding="utf-8")
    start = source.index("Json channel_config(const Json& s) {")
    end = source.index("\nJson mcp_server(", start)
    mapper = source[start:end]
    # The mapper and JSON implementation are production code. Unrelated model/key lookup
    # is stubbed, so this executable cannot access user settings, credentials or a network.
    harness = r'''
#include "json.h"
#include <iostream>
#include <iterator>
#include <stdexcept>
std::string trim(const std::string& s) { return s; }
bool unprotect(const std::string&, std::string&) { throw std::runtime_error("no credentials in fixture"); }
const Json* find_by(const Json&, const char*, const std::string&) { return nullptr; }
Json model_of(const Json&, const Json&) { return Json::object().set("provider", "local"); }
''' + mapper + r'''
int main() {
    std::string text((std::istreambuf_iterator<char>(std::cin)), std::istreambuf_iterator<char>());
    Json settings;
    if (!Json::parse(text, settings)) return 2;
    std::cout << channel_config(settings).dump();
}
'''
    tmp = tmp_path_factory.mktemp("native-retention")
    cpp, exe = tmp / "mapper.cpp", tmp / "mapper.exe"
    cpp.write_text(harness, encoding="utf-8")
    built = subprocess.run([
        compiler, "-std=c++17", "-finput-charset=UTF-8", "-fexec-charset=UTF-8",
        "-I", str(root / "desktop/host/src"), str(cpp),
        str(root / "desktop/host/src/json.cpp"), "-o", str(exe),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert built.returncode == 0, built.stdout + built.stderr

    def run(qq):
        result = subprocess.run([str(exe)], input=json.dumps({"qq": qq}),
                                capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return run


def test_missing_retention_values_remain_inherited(native_map):
    out = native_map({"group_send": "draft", "private_send": "auto"})
    assert "log_raw" not in out["group"] and "api_log_raw" not in out
    assert out["group"]["send"] == "draft" and out["private"]["send"] == "auto"
    conf, _ = parse_conf(out)
    assert conf["group"]["log_raw"] is False and conf["api_log_raw"] is False


@pytest.mark.parametrize("enabled", [False, True])
def test_explicit_bools_survive_native_and_backend(native_map, enabled):
    out = native_map({"group": {"log_raw": enabled}, "api_log_raw": enabled,
                      "convs": [{"type": "group", "id": 9001, "log_raw": enabled}]})
    assert out["group"]["log_raw"] is enabled and out["api_log_raw"] is enabled
    assert out["convs"][0]["log_raw"] is enabled
    conf, _ = parse_conf(out)
    assert conf["group"]["log_raw"] is enabled and conf["api_log_raw"] is enabled
    assert conf["group_log_overrides"][9001] is enabled


def test_disabled_group_keeps_explicit_off(native_map):
    out = native_map({"group": {"log_raw": True}, "convs": [
        {"type": "group", "id": 9001, "enabled": False, "log_raw": False},
        {"type": "group", "id": 9002},
    ]})
    conf, _ = parse_conf(out)
    assert conf["group"]["log_raw"] is True
    assert conf["group_log_overrides"][9001] is False
    assert ("group", 9001) not in conf["convs"]
    assert "log_raw" not in out["convs"][1]


@pytest.mark.parametrize("value", ["false", 0, None, [], {}])
@pytest.mark.parametrize("location", ["group", "api", "conv"])
def test_invalid_values_are_forwarded_for_explicit_rejection(native_map, value, location):
    qq = {"group": {"log_raw": value}} if location == "group" else (
        {"api_log_raw": value} if location == "api" else
        {"convs": [{"type": "group", "id": 9001, "log_raw": value}]})
    out = native_map(qq)
    got = out["group"]["log_raw"] if location == "group" else (
        out["api_log_raw"] if location == "api" else out["convs"][0]["log_raw"])
    assert got == value and type(got) is type(value)
    with pytest.raises(ConfigError):
        parse_conf(out)


def test_invalid_private_override_is_not_silently_erased(native_map):
    out = native_map({"convs": [{"type": "private", "id": 9001, "log_raw": True}]})
    with pytest.raises(ConfigError):
        parse_conf(out)
