"""文档解析 (2026-11-15 用户定的工具1; 知识库《六个工具类插件对照与取舍》第 2 节、《底层终端（插件宿主）方案》"和各专题对齐"
里"同一件事几个后端"). 计划: backups/tool1-docparse/计划.md.

做成"工具"型插件, 不是服务 (下载队列才是服务: 它要常驻、断点存盘; 文档解析无状态、一次一份). 引擎在这个包里 (不 import wm.agent 里
除 material_readers 以外的东西), 工作台这边的工具层是 wm/agent/docparse.py (她的两个工具、权限、MANIFEST).

三档后端 ("解析后端", 清单里 provides: doc-parse):
  1. builtin   现有的 material_readers: 文字层 / Office / OOXML / PDF 文字层 …, 一行没改, 只是在外面包了一层 (builtin.py).
  2. local-ocr 本机识字 (RapidOCR, 可选组件, 文件不出电脑): 只在有文字层读不出来 (扫描页) 或者文件本身是图片时才走 (ocr.py,
               worker.py 是识字子进程, component.py 管组件装在哪、装没装).
  3. cloud     云端 MinerU (工具3, 这一轮不做): 只有一份声明 (cloud.py), 路由里没有任何走它的分支. 本机读不了就老老实实报"读不了",
               不会自动改走云端; 以后实现时必须每个文件带主人当次的同意.

文件: __init__.py (常量、BackendInfo、后端登记) · builtin.py (第一档) · ocr.py (第二档) · worker.py (识字子进程) ·
component.py (识字组件) · cloud.py (第三档的位置) · router.py (parse: 三档路由、范围、字数).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import ROOT

DEFAULT_MAX_CHARS = 20_000      # 一次最多给她多少字 (整段整段放)
MAX_CHARS_CAP = 60_000
OCR_PAGES_DEFAULT = 10          # 一次最多识几页
OCR_PAGES_CAP = 30
UNREAD_SHOWN = 40               # 结果里最多列几条"没读"
CLOUD_NOTE = "云端解析还没有开通。本机读不了的文件不会自动上传。"
OCR_DIR = ROOT / "runtime" / "ocr"   # 识字组件装在这 (点头之前不存在)


class DocParseError(ValueError):
    """不给做的事 (路径不对、不是文件、参数不合法). 话是给她和主人看的."""


@dataclass(frozen=True)
class BackendInfo:
    """一个解析后端的自我介绍. upload: 文件会不会离开本机; auto: 能不能被自动选; needs_consent: 每个文件要不要主人当次点头."""
    id: str
    name: str
    kind: str                    # builtin | local-ocr | cloud
    upload: bool = False
    auto: bool = True
    needs_consent: bool = False
    available: bool = True
    reason_unavailable: str = ""
    detail: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "upload": self.upload, "auto": self.auto,
                "needs_consent": self.needs_consent, "available": self.available,
                "reason_unavailable": self.reason_unavailable, "detail": self.detail}


# 额外登记的后端. 路由只会调 builtin 和 local-ocr 两档; 登记别的 (尤其 kind=cloud) 只是让 doc_backends 能列出来.
# 测试用它登记一个假的云端后端, 证明不管什么情形它都不会被调用.
_EXTRA: list = []


def register_backend(backend) -> None:
    """backend 要有 info (BackendInfo). 同 id 的换掉."""
    unregister_backend(backend.info.id)
    _EXTRA.append(backend)


def unregister_backend(backend_id: str) -> None:
    _EXTRA[:] = [b for b in _EXTRA if b.info.id != backend_id]


def extra_backends() -> list:
    return list(_EXTRA)

