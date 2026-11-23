"""QQ 登录窗口上的二维码 (wm/channels/qq_window.py): 用测试自己开的一个几乎透明的小窗口当替身, 不碰真的 QQ 窗口."""
import os
import time

import pytest

from wm.channels import qq_window as QW

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
TEXTS = ["https://example.test/login?k=TEST-QR-1234567890", "https://qm.qq.com/q/AbCdEfGh?x=" + "y" * 120, "短文本 ok"]


@pytest.mark.parametrize("text", TEXTS)
def test_render_then_decode_roundtrip(text):
    png = QW.render_png(text)
    assert png.startswith(b"\x89PNG")
    img = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert QW.decode(img) == text
    shrunk = cv2.resize(img, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)  # 窗口里的二维码比重画的小
    if len(text) < 80:
        assert QW.decode(shrunk) == text
    inverted = cv2.bitwise_not(img)  # 深色主题
    assert QW.decode(inverted) == text


def test_noise_and_empty_images_decode_to_nothing():
    rng = np.random.default_rng(7)
    assert QW.decode(rng.integers(0, 255, (300, 300, 3), dtype=np.uint8)) is None
    assert QW.decode(np.full((200, 200, 3), 255, dtype=np.uint8)) is None
    assert QW.decode(None) is None and QW.decode(np.zeros((0, 0, 3), dtype=np.uint8)) is None


@pytest.fixture(scope="module")
def tk_root():
    """一个藏起来的 Tk 根窗口给整个文件用 (反复建、销毁 Tk 在同一个进程里偶尔会失败)."""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("没有桌面可用")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:
        pass


@pytest.fixture
def tk_window(tk_root):
    import tkinter as tk
    PIL_ImageTk = pytest.importorskip("PIL.ImageTk")
    from PIL import Image
    import qrcode
    made = []

    def make(text, size=(360, 420), title="test-qr-window"):
        win = tk.Toplevel(tk_root)
        made.append(win)
        win.title(title)
        win.geometry(f"{size[0]}x{size[1]}+60+60")
        win.attributes("-alpha", 0.03)  # 几乎看不见, 但在屏幕上, 一会儿就关
        img = qrcode.make(text).convert("RGB").resize((300, 300), Image.NEAREST)
        photo = PIL_ImageTk.PhotoImage(img, master=win)
        label = tk.Label(win, image=photo, bg="white")
        label.image = photo
        label.pack(padx=30, pady=40)
        win.update()
        time.sleep(0.4)
        win.update()
        return win
    yield make
    for w in reversed(made):
        try:
            w.destroy()
        except Exception:
            pass


def test_a_login_sized_window_of_the_given_process_is_read_and_redrawn(tk_window):
    text = TEXTS[0]
    tk_window(text)
    me = os.getpid()
    wins = QW.candidate_windows([me])
    assert wins and all(w["pid"] == me for w in wins)
    shot = QW.snapshot_qr([me])
    assert shot is not None and shot.text == text and shot.pid == me and shot.png.startswith(b"\x89PNG")
    assert time.time() - shot.at < 30
    assert QW.snapshot_qr([me + 1_000_000]) is None  # 别的进程的窗口不看
    assert QW.snapshot_qr([]) is None


def test_big_or_minimised_windows_are_not_candidates(tk_window):
    win = tk_window(TEXTS[0], size=(1200, 1100), title="test-qr-big")  # 比登录窗口大: 是聊天主窗口那种, 不碰
    me = os.getpid()
    assert QW.candidate_windows([me]) == [] and QW.snapshot_qr([me]) is None
    win.geometry("360x420+60+60")
    win.update()
    time.sleep(0.2)
    assert QW.candidate_windows([me])
    win.iconify()
    win.update()
    time.sleep(0.2)
    assert QW.candidate_windows([me]) == []  # 最小化的截不到, 也不去动它


def test_a_window_with_no_qr_gives_nothing(tk_window):
    tk = pytest.importorskip("tkinter")
    root = tk_window("占位", size=(360, 420), title="test-no-qr")
    for child in root.winfo_children():
        child.destroy()
    tk.Label(root, text="请点击登录", bg="white", fg="black").pack(padx=60, pady=100)
    root.update()
    time.sleep(0.3)
    root.update()
    assert QW.candidate_windows([os.getpid()]) and QW.snapshot_qr([os.getpid()]) is None
