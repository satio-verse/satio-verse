"""Reproducibly package only the platform deployment source whitelist."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    'wm/__init__.py', 'wm/config.py', 'wm/agent/__init__.py', 'wm/agent/extensions.py',
    'wm/agent/mcp_runtime.py', 'wm/agent/install_gate.py', 'wm/agent/plugin_manifest.py',
    'wm/platform_services.py',
    'tools/community_server.py', 'tools/release_bundle.py', 'tools/platform_admin.py', 'tools/sign_release.py',
    'deploy/requirements-platform.txt', 'deploy/requirements-signatures.txt',
    'deploy/start-platform.ps1', 'deploy/start-platform.sh', 'deploy/nginx-platform.conf.example',
    'docs/platform-publishing.md',
)


def build(output: Path):
    files = {name: (ROOT / name).read_bytes() for name in FILES}
    files['README.md'] = ('# WorldModel 平台部署包\n\n'
        '先阅读 [完整部署说明](docs/platform-publishing.md)。\n\n'
        '- 社区发布、公开索引与贡献接收服务：`tools/community_server.py`\n'
        '- 初始化作者与私有凭据：`tools/platform_admin.py`（不会打印密钥）\n'
        '- 应用更新构建／离线替换／回滚：`tools/release_bundle.py`\n'
        '- 可选 Ed25519 签名：`tools/sign_release.py`\n'
        '- Python 依赖及 Windows/Linux 启动示例：`deploy/`\n\n'
        '本包不含任何密钥、用户数据、模型或桌宠程序。社区当前尚未公开部署。'
        '收集训练候选不等于执行训练，服务不会训练模型。\n').encode('utf-8')
    files['CONTENTS.sha256'] = ''.join(hashlib.sha256(data).hexdigest() + '  ' + name + '\n'
                                     for name, data in sorted(files.items())).encode('utf-8')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            info=zipfile.ZipInfo(name,date_time=(2026,10,29,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o100644 << 16
            archive.writestr(info,data)
    return {'path': str(output), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'bytes': output.stat().st_size, 'files': sorted(files), 'secrets_or_user_data': False}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(build(args.output),ensure_ascii=False))
