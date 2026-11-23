"""采集后的筛选 (2026-11-11 评审第 1 轮): 快照只留分析用得上的进程、服务、监听, 其余的采到就丢, 不进 Snapshot.

采集脚本一次采到全部五百多个进程名、三百多个服务、八十多个监听, 分析只用得上十来个: 认得的客户端和安全软件的进程、它们的监听、
托管它们的服务. 其余的 (用户开着什么程序、装了什么服务) 不该留着. collect() 返回前、命令行读存好的快照时都过一遍 trim():
纯函数, 不改传进来的、返回新的一份; 筛过的再筛一遍不变.

留什么 (网卡、地址、接口、路由、系统代理、环境变量代理、errors、collected_at 都原样留, 不筛):
- 监听: 属主是认得的客户端进程的; 或端口等于系统代理 (开着的)、环境变量代理里写的端口的.
- 进程: 名字在客户端表、安全软件表里的; 留下来的监听的属主; 以上这些进程的父进程和祖父进程 (看"是不是以服务方式运行"要用).
- 服务: 名字在客户端表里的 (服务名或进程名都算); 或 pid 是留下来的进程的 (托管它们的服务, 比如 nssm 托管 sing-box 的那个).
  共享的 svchost 托管的不留: 分析本来就不把它们当"服务方式运行".

筛完分析出的报告和不筛一模一样 (tests/test_net_collect.py 拿 9 份带无关进程的样本核对).
"""
from __future__ import annotations

import copy
from dataclasses import replace

from .checkup import match_process, match_service, norm_name, parse_proxy_server, security_label
from .snapshot import Process, Service, Snapshot

ANCESTORS = 2  # 留几代祖先: 父进程和祖父进程


def proxy_ports(snap: Snapshot) -> set[int]:
    """系统代理 (开着的) 和用户环境变量代理里写的端口."""
    ports: set[int] = set()
    if snap.system_proxy.enabled:
        ports |= {e.port for e in parse_proxy_server(snap.system_proxy.server) if e.port is not None}
    for name in ("http_proxy", "https_proxy", "all_proxy"):
        item = getattr(snap.env_proxy, name)
        if item.present and item.port is not None:
            ports.add(item.port)
    return ports


def _hosts_kept_process(s: Service, by_pid: dict[int, Process], keep: set[int]) -> bool:
    """服务的进程是留下来的进程 (共享的 svchost 除外)."""
    return s.pid > 0 and s.pid in keep and norm_name(by_pid[s.pid].name) != "svchost"


def trim(snap: Snapshot) -> Snapshot:
    """筛掉分析用不上的进程、服务、监听 (规则见模块说明); 返回新的一份, 传进来的不动."""
    by_pid = {p.pid: p for p in snap.processes}
    client_pids = {pid for pid, p in by_pid.items() if match_process(p.name) is not None}
    ports = proxy_ports(snap)
    listeners = [item for item in snap.listeners if item.pid in client_pids or item.port in ports]
    seeds = client_pids | {pid for pid, p in by_pid.items() if security_label(p.name) is not None}
    seeds |= {item.pid for item in listeners if item.pid in by_pid}
    keep = set(seeds)
    for pid in seeds:
        p = by_pid[pid]
        for _ in range(ANCESTORS):
            parent = by_pid.get(p.ppid)
            if parent is None or parent.pid == p.pid:
                break
            keep.add(parent.pid)
            p = parent
    processes = [p for p in snap.processes if p.pid in keep]
    services = [s for s in snap.services
                if match_service(s.name) is not None or match_process(s.name) is not None or _hosts_kept_process(s, by_pid, keep)]
    return copy.deepcopy(replace(snap, processes=processes, services=services, listeners=listeners))
