"""假识字子进程 (只给测试用): 取图、拼字、回结果都是 wm/docparse/worker.py 里真的那一套, 只是把识字器换成"读像素当文字".
不在生产代码里放"假识字器开关": 要换识字器, 测试把命令换成这个脚本. 用法: python -I docparse_stub_worker.py [日志文件]
图是一行灰度像素, 值 = 文字的 UTF-8 字节 (见 docparse_fixtures.image_pixels). 文字里的 "|" 分成多行 (从上往下).
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wm.docparse import worker  # noqa: E402


class FakeRecognizer:
    name = "fake-ocr"
    version = "1"

    def recognize(self, img):
        raw = img.convert("L").tobytes()[:img.width].rstrip(b"\x00")
        text = raw.decode("utf-8", errors="replace")
        return [(piece, [[0, 20 * i], [100, 20 * i], [100, 20 * i + 10], [0, 20 * i + 10]], 0.9)
                for i, piece in enumerate(text.split("|")) if piece]


if __name__ == "__main__":
    if len(sys.argv) > 1:       # 记下收到的任务
        data = sys.stdin.buffer.read()
        with open(sys.argv[1], "ab") as f:
            f.write(data + b"\n")
        sys.stdin = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")
    sys.exit(worker.serve(lambda job: FakeRecognizer()))
