"""第三档 (云端 MinerU, 工具3) 的位置. 这一轮不做: 这里只有一份声明, 没有网络代码, 路由 (router.py) 里没有走它的分支.

以后实现时的规矩 (决定原文: 《等你拍板的事》工具3):
- 整份文件传到 mineru.net, 每个文件单独问主人, 本机读不了也不会自动改走云端; 失败不自动转别的.
- Key 主人自己申请、放保管箱, 第一版做成自带的插件 (宿主1), 启动时把 Key 交给它; Key 不经过这个包, 输出里不许有.
- 实现时在 __init__.register_backend 登记一个 kind=cloud 的后端, 并在 router 里加一个"显式要求才用"的分支: 必须带
  consent(info, path) 回调, 回调返回 True (这个文件, 这一次) 才调; 第一、第二档的代码不用碰.
"""
from __future__ import annotations

from . import BackendInfo, CLOUD_NOTE

INFO = BackendInfo(id="mineru-cloud", name="云端 MinerU", kind="cloud", upload=True, auto=False, needs_consent=True,
                   available=False, reason_unavailable="还没有开通", detail=CLOUD_NOTE)
