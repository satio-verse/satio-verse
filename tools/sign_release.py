"""Optional Ed25519 release signing; requires cryptography only when actually used."""
import argparse
import base64
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wm.platform_services import canonical, read


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    keygen = sub.add_parser('keygen')
    keygen.add_argument('--private-key', type=Path, required=True)
    keygen.add_argument('--public-key', type=Path, required=True)
    sign = sub.add_parser('sign')
    sign.add_argument('--private-key', type=Path, required=True)
    sign.add_argument('--input', type=Path, required=True)
    sign.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        parser.error('签名需要 pip install -r deploy/requirements-signatures.txt；也可使用已支持的固定清单 SHA256 更新方式。')
    if args.action == 'keygen':
        if args.private_key.exists() or args.public_key.exists():
            parser.error('密钥目标已存在，未覆盖')
        key = Ed25519PrivateKey.generate()
        private = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        for path, value in ((args.private_key, private), (args.public_key, public)):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x', encoding='ascii') as stream:
                stream.write(base64.b64encode(value).decode())
        if os.name != 'nt':
            args.private_key.chmod(0o600)
        print('已写入密钥文件；私钥仅供发布者保管，客户端只配置公钥。')
    else:
        if args.output.exists():
            parser.error('输出文件已存在，未覆盖')
        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(args.private_key.read_text('ascii').strip(), validate=True))
        value = read(args.input, {})
        if not isinstance(value, dict) or value.get('worldmodel_release') != 1:
            parser.error('输入不是 WorldModel 更新包')
        value.pop('signature', None)
        value['signature'] = base64.b64encode(key.sign(canonical(value))).decode()
        args.output.write_bytes(canonical(value))
        print('已生成带签名更新包。')


if __name__ == '__main__':
    main()
