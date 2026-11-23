"""Provision local author credentials for the deployable community service.

Credentials are written to private files, never printed or transmitted.
"""
import argparse
import json
import os
import secrets
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wm.platform_services import identifier, digest, read
from wm.agent.extensions import _atomic


def provision(private_dir: Path, author: str):
    author = identifier(author)
    private_dir.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        private_dir.chmod(0o700)
    authors_file = private_dir / 'authors.json'
    token_file = private_dir / (author + '-token.txt')
    if token_file.exists():
        raise ValueError('此作者的令牌文件已存在，未覆盖；轮换时先移除旧哈希并备份令牌')
    authors = read(authors_file, {})
    if not isinstance(authors, dict) or author in authors.values():
        raise ValueError('作者列表无效，或该作者已存在；未覆盖原认证')
    token = secrets.token_urlsafe(48)
    with token_file.open('x', encoding='utf-8') as stream:
        stream.write(token)
    authors[digest(token.encode())] = author
    _atomic(authors_file, authors)
    receipt = private_dir / 'receipt-secret.txt'
    if not receipt.exists():
        with receipt.open('x', encoding='utf-8') as stream:
            stream.write(secrets.token_hex(32))
    if os.name != 'nt':
        for file in (token_file, authors_file, receipt):
            file.chmod(0o600)
    return {'author': author, 'author_token_file': str(token_file), 'authors_file': str(authors_file),
            'receipt_secret_file': str(receipt), 'notice': '只给该作者提供其令牌；保留删除回执密钥，勿公开 private 目录。'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-dir', required=True, type=Path)
    parser.add_argument('--author', required=True)
    args = parser.parse_args()
    print(json.dumps(provision(args.private_dir, args.author), ensure_ascii=False))
