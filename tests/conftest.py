"""全部测试共用:
- 去掉环境变量里的 DeepSeek key. 账本 (wm/ledger.py) 没有别的 key 时会拿它去查真的余额, 测试里不该连外网;
- 微信 (wm/channels/weixin.py) 绑好的账号放在临时目录: 你真的扫码绑过微信时, 测试起的服务端不该拿你的 token 去收消息;
- 看守给正式服务端的环境变量不带进来."""
import pytest


@pytest.fixture(autouse=True)
def _no_real_deepseek_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _no_supervisor_env(monkeypatch):
    """看守给正式服务端的环境变量 (心跳文件、存申请的文件) 不带进测试: 门禁在正式服务端里跑 pytest 时, 测试起的服务端
    不该往正式服务端的心跳里写、读写你真的申请 (wm/workbench.py 的 SUPERVISOR_ENV)."""
    from wm import workbench
    for k in workbench.SUPERVISOR_ENV:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _no_loopback_capture(monkeypatch):
    """外放回声的参考 (wm/loopback.py, 系统声音回环) 测试里不真的录: 测试起的服务端不该去听你电脑正在放的声音.
    要测录音本身的测试自己把它打开."""
    from wm import loopback
    monkeypatch.setattr(loopback, "ALLOW_CAPTURE", False)


@pytest.fixture(autouse=True)
def _weixin_home(monkeypatch, tmp_path_factory):
    from wm.channels import weixin
    monkeypatch.setattr(weixin, "HOME", tmp_path_factory.mktemp("weixin"))


@pytest.fixture(autouse=True)
def _vault_home(monkeypatch, tmp_path_factory):
    """账号保管箱 (wm/channels/vault.py) 放临时目录: 测试起的渠道管理器不该读到、更不该动你真的 ~/.worldmodel/vault 里的密码;
    B 站读 Cookie 的保管箱挂钩 (bili._vault) 每个测试都从没有开始."""
    from wm.channels import bili, vault
    monkeypatch.setattr(vault, "HOME", tmp_path_factory.mktemp("vault"))
    monkeypatch.setattr(bili, "_vault", None)


@pytest.fixture(autouse=True)
def _agent_home(monkeypatch, tmp_path_factory):
    """工作台的会话 (wm/agent/core.py) 也放临时目录, 不写进你真的 ~/.worldmodel/agent."""
    from wm.agent import core
    monkeypatch.setattr(core, "HOME", tmp_path_factory.mktemp("agent"))
    from wm.agent import extensions
    monkeypatch.setattr(extensions, 'DEFAULT_DESKTOP_DISCOVERY', False)


@pytest.fixture(autouse=True)
def _services_home(monkeypatch, tmp_path_factory):
    """桌宠管服务 (wm/services.py) 的状态和请求也放临时目录: 测试起的服务端不该读到、更不该去停你真的服务."""
    from wm import services
    home = tmp_path_factory.mktemp("services")
    monkeypatch.setattr(services, "HOME", home)
    monkeypatch.setattr(services.read_state, "__defaults__", (home,))
    monkeypatch.setattr(services.request, "__defaults__", (None, home))
    monkeypatch.setattr(services.up, "__defaults__", (home,))


@pytest.fixture(autouse=True)
def _no_download_service(monkeypatch):
    """下载队列的服务 (wm/downloads) 是另一个进程: 一般的测试不该顺手把它起出来 (要测它的测试自己打开, 见 test_downloads_service.py)."""
    from wm.downloads import client
    monkeypatch.setattr(client, "SPAWN", False)
