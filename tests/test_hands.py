"""她的手: 底层点按只算坐标、检查屏幕、发 SetCursorPos + SendInput; 用替身 user32, 不动真的鼠标."""
import ctypes

import pytest

from wm import hands as H


class FakeUser32:
    def __init__(self, rect=(0, 0, 1920, 1080)):
        self.rect = rect
        self.cursor = None
        self.flags = []
        self.cursor_ok = True
        self.send_ok = True

    def GetSystemMetrics(self, index):
        if index in H.VIRTUAL_SCREEN:
            return self.rect[H.VIRTUAL_SCREEN.index(index)]
        return {0: self.rect[2], 1: self.rect[3]}.get(index, 0)

    def SetCursorPos(self, x, y):
        if not self.cursor_ok:
            return 0
        self.cursor = (x, y)
        return 1

    def SendInput(self, n, pointer, size):
        assert n == 1 and size == ctypes.sizeof(H._INPUT)
        self.flags.append(pointer._obj.mi.dwFlags)
        return 1 if self.send_ok else 0


def fake(monkeypatch, rect=(0, 0, 1920, 1080)):
    u = FakeUser32(rect)
    monkeypatch.setattr(H, "_user32", lambda: u)
    return u


def test_click_moves_the_pointer_then_presses_and_releases(monkeypatch):
    u = fake(monkeypatch)
    out = H.click(123, 456)
    assert u.cursor == (123, 456)
    assert u.flags == [0x0002, 0x0004]  # LEFT DOWN / LEFT UP
    assert "左键" in out and "一下" in out and "点好了" in out


def test_right_click_and_double_click(monkeypatch):
    u = fake(monkeypatch)
    H.click(10, 20, button="right", count=2)
    assert u.cursor == (10, 20)
    assert u.flags == [0x0008, 0x0010, 0x0008, 0x0010]  # RIGHT DOWN / UP, 两遍
    assert H.Click(1, 2, "left", 2).shown() == "在屏幕 (1, 2) 用左键点两下"


def test_parse_rejects_bad_coordinates_button_and_count():
    assert H.parse_click("12", 34) == H.Click(12, 34)
    for args in [(True, 1), ("x", 1), (1, None), (1, 2, "wheel"), (1, 2, "left", 0),
                 (1, 2, "left", 4), (10 ** 6, 1)]:
        with pytest.raises(ValueError):
            H.parse_click(*args)


def test_click_checks_the_whole_virtual_screen(monkeypatch):
    fake(monkeypatch, rect=(-100, -50, 800, 600))  # 左边、上边还有一块副屏
    H.click(-100, -50)
    H.click(699, 549)
    with pytest.raises(ValueError, match="不在屏幕上"):
        H.click(700, 0)
    with pytest.raises(ValueError, match="不在屏幕上"):
        H.click(-101, 0)


def test_click_reports_when_the_system_refuses(monkeypatch):
    u = fake(monkeypatch)
    u.cursor_ok = False
    with pytest.raises(OSError, match="挪不过去"):
        H.click(1, 2)
    u.cursor_ok, u.send_ok = True, False
    with pytest.raises(OSError, match="没送出去"):
        H.click(1, 2)


def test_click_is_only_for_windows(monkeypatch):
    monkeypatch.setattr(H.sys, "platform", "linux")
    with pytest.raises(OSError, match="Windows"):
        H.click(1, 2)
