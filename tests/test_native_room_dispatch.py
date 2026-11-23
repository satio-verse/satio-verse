"""Compile the actual queued UI callback against a deterministic mock event queue."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_native_room_dispatch_rechecks_pending_identity(tmp_path):
    compiler = shutil.which("g++")
    if not compiler:
        pytest.skip("g++ required for native callback regression")
    root = Path(__file__).resolve().parents[1]
    source = (root / "desktop/host/src/tools.cpp").read_text(encoding="utf-8")
    start = source.index("    app_.ui([this, m, id", source.index("void ToolHub::builtin_run"))
    end = source.index("    });", start) + len("    });")
    callback = source[start:end]
    code = r'''
#include <cassert>
#include <functional>
#include <iostream>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <vector>
using ULONGLONG = unsigned long long;
struct Json { std::string dump() const { return "room-do"; } };
std::string widen(const std::string& s) { return s; }
struct App {
  struct Web { int posted = 0; void post_json(const std::string&) { ++posted; } } web;
  std::vector<std::function<void()>> queued;
  void ui(std::function<void()> f) { queued.push_back(f); }
  void drain() { auto work = queued; queued.clear(); for (auto& f : work) f(); }
};
struct Harness {
  struct Pending { ULONGLONG t0; };
  App app_;
  std::mutex mu_;
  std::map<std::string, Pending> pending_;
  std::set<std::string> stopped_;
  int failed = 0;
  void on_builtin_done(const std::string& id, bool ok, const std::string&) {
    assert(!ok); ++failed; pending_.erase(id); stopped_.erase(id);
  }
  void queue(const std::string& id, ULONGLONG t0) {
    Json m;
''' + callback + r'''
  }
};
int main() {
  { // Normal delivery still posts exactly once.
    Harness h; h.pending_["x"] = {10}; h.queue("x", 10); h.app_.drain();
    assert(h.app_.web.posted == 1 && h.failed == 0);
  }
  { // Stop already settled: reply erased both pending and stopped before UI execution.
    Harness h; h.pending_["x"] = {10}; h.queue("x", 10);
    h.stopped_.insert("x"); h.on_builtin_done("x", false, "cancelled");
    h.app_.drain(); assert(h.app_.web.posted == 0 && h.failed == 1);
  }
  { // Stop marked but not settled yet.
    Harness h; h.pending_["x"] = {10}; h.queue("x", 10); h.stopped_.insert("x");
    h.app_.drain(); assert(h.app_.web.posted == 0 && h.failed == 1);
  }
  { // A stale queued callback cannot post or settle a newer request with the same id.
    Harness h; h.pending_["x"] = {10}; h.queue("x", 10);
    h.pending_["x"] = {11}; h.queue("x", 11); h.app_.drain();
    assert(h.app_.web.posted == 1 && h.failed == 0 && h.pending_.at("x").t0 == 11);
  }
  std::cout << "actual queued native callback: 4 lifecycle cases passed\n";
}
'''
    cpp = tmp_path / "room_dispatch.cpp"
    exe = tmp_path / "room_dispatch.exe"
    cpp.write_text(code, encoding="utf-8")
    built = subprocess.run([compiler, "-std=c++17", "-finput-charset=UTF-8", "-fexec-charset=UTF-8",
                            str(cpp), "-o", str(exe)], capture_output=True, text=True)
    assert built.returncode == 0, built.stdout + built.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 lifecycle cases passed" in result.stdout
