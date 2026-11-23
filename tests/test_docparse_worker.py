"""识字子进程这一层 (wm/docparse/ocr.py、worker.py、component.py): 协议、超时、超内存、坏输出、识字组件没装; 解轮子的路径检查.
识字器全是假的 (tests/docparse_stub_worker.py 和下面现写的小脚本), 没有碰真 RapidOCR. 在管理员权限的会话里跑过的, 起的子进程也带管理员权限."""
import hashlib
import importlib.util
import json
import os
import sys
import textwrap
import time
import zipfile

import pytest

from docparse_fixtures import installed_ocr_dir, pdf_bytes, stub_cmd
from wm.docparse import component, ocr, router
from wm.docparse import worker as W


def script(tmp_path, body, name="w.py"):
    p = tmp_path / name
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, "-I", str(p)]


JOB = {"op": "ocr_pdf", "path": "x.pdf", "pages": [1], "ocr_dir": ""}


# ---------------------------------------------------------------- 子进程怎么失败
def test_a_hung_recognizer_is_killed_at_the_timeout(tmp_path):
    cmd = script(tmp_path, "import time; time.sleep(60)")
    t0 = time.time()
    got = ocr.run_ocr(JOB, cmd=cmd, timeout_s=1.0)
    assert got["ok"] is False and got["code"] == "timeout" and "1 秒" in got["error"] and time.time() - t0 < 10


@pytest.mark.parametrize("body,why", [
    ("import sys; sys.stdin.buffer.read(); print('not json')", "没有给出有效结果|超出资源限制"),
    ("import sys; sys.stdin.buffer.read(); print('[1, 2]')", "没有给出有效结果"),
    ("import sys; sys.exit(3)", "异常退出"),
    ("import sys; sys.stdin.buffer.read(); print('x' * (5 * 1024 * 1024))", "超出资源限制"),
])
def test_bad_output_and_crashes_become_a_plain_failure(tmp_path, body, why):
    got = ocr.run_ocr(JOB, cmd=script(tmp_path, body), timeout_s=20)
    assert got["ok"] is False and got["code"] == "failed"
    import re
    assert re.search(why, got["error"])


@pytest.mark.skipif(os.name != "nt", reason="内存上限靠 Windows 作业对象")
def test_a_recognizer_that_eats_memory_is_stopped_by_the_job_limit(tmp_path):
    cmd = script(tmp_path, "import sys; sys.stdin.buffer.read(); x = bytearray(3000 * 1024 * 1024); print('{\"ok\": true}')")
    got = ocr.run_ocr(JOB, cmd=cmd, timeout_s=30)
    assert got["ok"] is False and "超出资源限制" in got["error"]


def _job_run(alloc_mb, **job_kw):
    """起一个等着的子进程, 套上 material_readers._windows_job (job_kw 原样传), 再叫它分配 alloc_mb MB. 返回 (退出码, 输出)."""
    import subprocess
    from wm.agent.material_readers import _windows_job
    code = "import sys; sys.stdin.buffer.read(); x = bytearray(%d * 1024 * 1024); print('allocated')" % alloc_mb
    p = subprocess.Popen([sys.executable, "-I", "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    close = _windows_job(p, **job_kw)
    try:
        out, _ = p.communicate(b"go", timeout=60)
    finally:
        close()
    return p.returncode, out.decode().strip()


@pytest.mark.skipif(os.name != "nt", reason="内存上限靠 Windows 作业对象")
def test_the_job_limit_defaults_to_768_mb_and_does_not_change_when_memory_is_not_passed():
    assert _job_run(300) == (0, "allocated")
    code, out = _job_run(1000)               # 超过 768 MB: 被结束
    assert code != 0 and out == ""


@pytest.mark.skipif(os.name != "nt", reason="内存上限靠 Windows 作业对象")
def test_a_bigger_limit_takes_effect_and_the_gate_still_shuts_above_it():
    assert _job_run(1000, memory=1536 * 1024 * 1024) == (0, "allocated")        # 默认的 768 MB 下过不去的, 传了就过
    code, out = _job_run(2500, memory=1536 * 1024 * 1024)                       # 超过传进去的上限: 照样被结束
    assert code != 0 and out == ""


@pytest.mark.skipif(os.name != "nt", reason="内存上限靠 Windows 作业对象")
def test_ocr_runs_under_its_own_bigger_limit(tmp_path):
    """真 RapidOCR 量过: 提交内存峰值 804–880 MB, 超过解析进程的 768 MB; 识字用 ocr.OCR_MEMORY_BYTES (1536 MB)."""
    assert 1000 * 1024 * 1024 < ocr.OCR_MEMORY_BYTES <= 2048 * 1024 * 1024
    cmd = script(tmp_path, "import sys, json; sys.stdin.buffer.read(); x = bytearray(1100 * 1024 * 1024); print(json.dumps({'ok': True}))")
    assert ocr.run_ocr(JOB, cmd=cmd, timeout_s=30) == {"ok": True}


def test_only_one_recognizer_process_runs_at_a_time(tmp_path):
    """工作台会把连着的只读调用一起跑: 两个线程同时识字, 第二个的进程要等第一个退出才起 (一个进程峰值约 880 MB)."""
    import threading
    mark = tmp_path / "order.log"
    cmd = script(tmp_path, f"""
        import sys, time
        sys.stdin.buffer.read()
        with open({str(mark)!r}, "a") as f:
            f.write("start "); f.flush()
        time.sleep(1.0)
        with open({str(mark)!r}, "a") as f:
            f.write("end ")
        print('{{"ok": true}}')
    """)
    results = []
    threads = [threading.Thread(target=lambda: results.append(ocr.run_ocr(JOB, cmd=cmd, timeout_s=30))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert results == [{"ok": True}, {"ok": True}]
    assert mark.read_text().split() == ["start", "end", "start", "end"]       # 交错的话会是 start start end end


def test_waiting_in_the_queue_does_not_count_against_the_timeout(tmp_path):
    import threading
    slow = script(tmp_path, "import sys, time, json; sys.stdin.buffer.read(); time.sleep(1.5); print(json.dumps({'ok': True}))", name="slow.py")
    out = []
    first = threading.Thread(target=lambda: ocr.run_ocr(JOB, cmd=slow, timeout_s=30))
    first.start()
    time.sleep(0.4)                                    # 让第一个先拿到锁
    out.append(ocr.run_ocr(JOB, cmd=slow, timeout_s=3.0))     # 这个要排队 1 秒多, 自己跑 1.5 秒: 总共超过 3 秒也不该算超时
    first.join(30)
    assert out == [{"ok": True}]


def test_a_command_that_cannot_start_is_a_failure_not_an_exception(tmp_path):
    got = ocr.run_ocr(JOB, cmd=[str(tmp_path / "no-such-program.exe")], timeout_s=5)
    assert got["ok"] is False and got["code"] == "failed"


def test_failed_recognition_marks_those_pages_unread_and_does_not_retry(tmp_path):
    ocr_dir = installed_ocr_dir(tmp_path / "home")
    log = tmp_path / "tries.log"
    cmd = script(tmp_path, f"import sys; open({str(log)!r}, 'a').write('x'); sys.exit(1)")
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(pdf_bytes([("scan", "a"), ("scan", "b")]))
    res = router.parse(pdf, ocr_dir=ocr_dir, ocr_cmd=cmd)
    assert res["status"] == "unreadable" and [u["n"] for u in res["unread"]] == [1, 2]
    assert all("识字没成" in u["reason"] for u in res["unread"]) and log.read_text() == "x"     # 只起了一次
    assert "上传" not in json.dumps({k: v for k, v in res.items() if k != "cloud"}, ensure_ascii=False)


def test_a_timeout_in_the_middle_gives_the_reason_and_the_text_pages_survive(tmp_path):
    ocr_dir = installed_ocr_dir(tmp_path / "home")
    pdf = tmp_path / "m.pdf"
    pdf.write_bytes(pdf_bytes([("text", "KEEP ME"), ("scan", "x")]))
    res = router.parse(pdf, ocr_dir=ocr_dir, ocr_cmd=script(tmp_path, "import time; time.sleep(60)"), ocr_timeout_s=1.0)
    assert res["status"] == "partial" and res["parts"][0]["text"] == "KEEP ME" and "超过 1 秒限制" in res["unread"][0]["reason"]


# ---------------------------------------------------------------- 子进程协议 (真的 worker.py 里的代码, 识字器换成假的)
def run_stub(tmp_path, job, raw=None):
    import subprocess
    p = subprocess.run(stub_cmd(), input=raw if raw is not None else json.dumps(job).encode(), capture_output=True, timeout=60)
    return json.loads(p.stdout.decode("utf-8"))


def test_protocol_ocr_pdf_reports_per_page_results_and_errors(tmp_path):
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(pdf_bytes([("scan", "第一页"), ("text", "NO IMAGE HERE"), ("scan", "第三页")]))
    got = run_stub(tmp_path, {"op": "ocr_pdf", "path": str(pdf), "pages": [1, 3, 9]})
    assert got["ok"] and got["engine"] == "fake-ocr 1"
    by = {r["page"]: r for r in got["pages"]}
    assert by[1]["text"] == "第一页" and by[3]["text"] == "第三页" and by[9]["error"] == "页码超出"
    only = run_stub(tmp_path, {"op": "ocr_pdf", "path": str(pdf), "pages": [2]})["pages"][0]
    if importlib.util.find_spec("pypdfium2") is None:       # 文字页上没有嵌入图: 没装渲染组件就明说识不了; 装了 (这台电脑装着) 就渲染了再识
        assert "没有可识字的图片" in only["error"]


def test_protocol_rejects_garbage_jobs(tmp_path):
    assert run_stub(tmp_path, None, raw=b"not json")["code"] == "bad-job"
    assert run_stub(tmp_path, {"op": "format-c-drive", "path": "x"})["code"] == "bad-job"
    assert run_stub(tmp_path, {"op": "ocr_pdf"})["code"] == "bad-job"
    missing = run_stub(tmp_path, {"op": "ocr_pdf", "path": str(tmp_path / "nope.pdf"), "pages": [1]})
    assert missing["ok"] is False and missing["code"] == "failed"


def test_protocol_encrypted_pdf_is_refused(tmp_path):
    from docparse_fixtures import encrypted_pdf_bytes
    pdf = tmp_path / "e.pdf"
    pdf.write_bytes(encrypted_pdf_bytes())
    got = run_stub(tmp_path, {"op": "ocr_pdf", "path": str(pdf), "pages": [1]})
    assert got["ok"] is False and "加密" in got["error"]


def test_lines_are_put_in_reading_order():
    box = lambda x, y: [[x, y], [x + 50, y], [x + 50, y + 10], [x, y + 10]]
    lines = [("下行右", box(200, 100), 0.8), ("上行", box(10, 0), 1.0), ("下行左", box(10, 101), 0.6), ("", box(0, 0), 1.0)]
    text, conf = W.lines_to_text(lines)
    assert text == "上行\n下行左 下行右" and abs(conf - 0.8) < 1e-9
    assert W.lines_to_text([]) == ("", 0.0)


# ---------------------------------------------------------------- 识字组件: 状态和解轮子
def make_wheel(tmp_path, entries, name="demo-1.0-py3-none-any.whl"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        for entry, data in entries.items():
            if not isinstance(entry, zipfile.ZipInfo):
                info = zipfile.ZipInfo("placeholder")
                info.filename = entry          # 不经过 ZipInfo 构造函数的分隔符改写: 反斜杠、.. 原样写进去
                entry = info
            z.writestr(entry, data)
    return p


GOOD = {"demo/__init__.py": "X = 1\n", "demo/data/model.onnx": b"\x00\x01", "demo-1.0.dist-info/METADATA": "Name: demo\n", "tiny.py": "Y = 2\n"}


def test_component_state_follows_the_marker_and_site_dir(tmp_path):
    root = tmp_path / "ocr"
    assert component.state(root)["installed"] is False and "还没装" in component.state(root)["reason"]
    (root / "site").mkdir(parents=True)
    assert "没装完整" in component.state(root)["reason"]
    installed_ocr_dir(root)
    assert component.state(root)["installed"] is True
    (root / component.MARK).write_text("{not json", encoding="utf-8")
    assert component.state(root)["installed"] is False


def test_unpack_a_good_wheel_and_check_the_hash(tmp_path):
    wheel = make_wheel(tmp_path, GOOD)
    site = tmp_path / "ocr" / "site"
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    tops = component.unpack_wheel(wheel, site, expected_sha256=digest)
    assert tops == ["demo", "demo-1.0.dist-info", "tiny.py"]
    assert (site / "demo" / "__init__.py").read_text() == "X = 1\n" and (site / "demo" / "data" / "model.onnx").read_bytes() == b"\x00\x01"
    assert not [p for p in (tmp_path / "ocr").iterdir() if p.name.startswith(".stage")]       # 暂停目录收干净了
    with pytest.raises(component.WheelError, match="已经有 demo"):          # 不覆盖
        component.unpack_wheel(wheel, site)
    other = tmp_path / "ocr2" / "site"
    with pytest.raises(component.WheelError, match="SHA-256 对不上"):
        component.unpack_wheel(wheel, other, expected_sha256="0" * 64)
    assert not other.exists() or not any(other.iterdir())


@pytest.mark.parametrize("bad,why", [
    ("../evil.py", r"\.\. 路径"),
    ("demo/../../evil.py", r"\.\. 路径"),
    ("/abs/evil.py", "绝对路径"),
    ("C:/evil.py", "绝对路径"),
    ("..\\evil.py", r"\.\. 路径"),       # Windows 上 zipfile 读出来时把反斜杠当分隔符, 照样被 .. 拦下
    ("demo-1.0.data/scripts/run.exe", "不收"),
    ("README.txt", "不是包目录也不是模块"),
    ("not a package/x.py", "不像一个包"),
])
def test_unpack_refuses_unsafe_or_odd_wheels_and_writes_nothing(tmp_path, bad, why):
    wheel = make_wheel(tmp_path, {"demo/__init__.py": "X = 1\n", bad: "boom"})
    site = tmp_path / "ocr" / "site"
    with pytest.raises(component.WheelError, match=why):
        component.unpack_wheel(wheel, site)
    assert not site.exists() or not any(site.iterdir())
    assert not (tmp_path / "evil.py").exists() and not (tmp_path / "ocr" / "evil.py").exists()


def test_unpack_refuses_symlinks_and_non_zip(tmp_path):
    info = zipfile.ZipInfo("demo/link")
    info.external_attr = 0o120777 << 16
    wheel = make_wheel(tmp_path, {"demo/__init__.py": "", info: "target"})
    with pytest.raises(component.WheelError, match="符号链接"):
        component.unpack_wheel(wheel, tmp_path / "s1")
    junk = tmp_path / "junk.whl"
    junk.write_bytes(b"not a zip")
    with pytest.raises(component.WheelError, match="不是合法的 zip"):
        component.unpack_wheel(junk, tmp_path / "s2")
