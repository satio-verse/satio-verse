"""访问本机服务 (127.0.0.1 上的服务端、本机模型、嵌入服务) 一律直连, 不走代理. 只用标准库.

环境变量里有 http_proxy 而没有 no_proxy 时 (装了代理工具的机器上常见), urllib 连 127.0.0.1 也送去代理, 代理多数回 502、有时直接断开.
2026-11-11: 看守 (tools/serve.py) 因此一直查不到 /api/status, 每 607 秒把好好的服务端当成"没起来"杀一次, 第一次还把刚合进来的
两次改动退回了; 服务监控 (wm/services.py) 查不到本机模型的 /health, 每 3 分钟重启它一次; 嵌入服务的探测回的是代理的 502.
在 Claude 会话里看不出来: 会话的环境里带着 NO_PROXY. 所以看本机服务好不好的地方都用这里的 open_direct, 不用 urllib.request.urlopen.
"""
from __future__ import annotations

import urllib.request
from urllib.parse import urlparse

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
# 空的代理表: 不读环境变量, 也不读 Windows 的系统代理 (ProxyHandler 不给参数时才去读)
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_local(url: str) -> bool:
    """地址是不是这台电脑自己 (127.0.0.1 / localhost / ::1)."""
    try:
        return (urlparse(url).hostname or "") in LOCAL_HOSTS
    except ValueError:
        return False


def open_direct(req, timeout: float = 5.0):
    """直连打开, 不走任何代理. req 是地址或 urllib.request.Request; 返回值和 urllib.request.urlopen 的一样 (可以 with)."""
    return _DIRECT.open(req, timeout=timeout)


def urlopen(req, timeout: float = 5.0):
    """本机地址直连; 别的地址照 urllib 的老规矩 (环境变量、系统代理). 给地址可能配成别的机器的地方用 (嵌入服务)."""
    url = req if isinstance(req, str) else req.full_url
    if is_local(url):
        return open_direct(req, timeout)
    return urllib.request.urlopen(req, timeout=timeout)


PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy")  # 环境里有其中一个 (名字不分大小写、值不空) = 这台电脑让程序走代理


def with_local_bypass(env: dict) -> dict:
    """给子进程用的环境: 里面有代理变量时, 保证 no_proxy 里有本机地址 (已有的条目照留); 不改传进来的字典.
    子进程里认环境变量的程序 (urllib、requests、httpx、curl…) 访问本机服务就不会被送去代理. 我们自己的探测用 open_direct,
    本来就不怕; 这是给服务端底下别的东西 (门禁的 pytest、她跑的命令、MCP 服务、语音合成) 垫的一层.
    没有代理变量时原样返回, 不凭空加: 环境里只有 no_proxy 时 urllib 整个不看 Windows 的系统代理, 上外网反而不走代理了
    (wm/agent/tools.py 的 _proxy 说的那回事). no_proxy 已经是 * (什么都不走代理) 的也不动.
    变量名沿用环境里已有的那个: Windows 上名字不分大小写, NO_PROXY 和 no_proxy 都给会重复."""
    if not any(v for k, v in env.items() if k.lower() in PROXY_VARS):
        return env
    out = dict(env)
    for key in [k for k in env if k.lower() == "no_proxy"] or ["NO_PROXY"]:
        have = [h.strip() for h in str(out.get(key) or "").split(",") if h.strip()]
        seen = {h.lower() for h in have}
        add = [h for h in LOCAL_HOSTS if h not in seen]
        if add and "*" not in seen:  # 已经齐了的一个字节都不动
            out[key] = ",".join(have + add)
    return out
