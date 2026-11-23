"""三个自带的渠道适配器 (聊天2): qq-onebot / weixin-ilink / local-guest. 接口见 wm/channels/adapter.py, 清单在 manifests/.

适配器往核心交东西只有 Host 的三个口; 核心里的信任档、访客会话、清洗和外发检查、白名单、限频额度、草稿、日志它们碰不到.
"""
ADAPTER_IDS = ("qq-onebot", "weixin-ilink", "local-guest")
