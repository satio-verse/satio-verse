"""Minimal deployable public registry + contribution collector.

Run behind a TLS reverse proxy. Author credentials are provisioned by the operator.
No training jobs, third-party uploads, analytics tracking or package execution.
"""
from __future__ import annotations
import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import web
from wm.platform_services import canonical, digest, read, identifier, redact
from wm.agent.extensions import _atomic, ExtensionRegistry


def create_app(root: Path, public_url: str, authors: dict, receipt_secret: bytes):
    """authors maps SHA256(bearer token) -> stable author ID."""
    if len(receipt_secret) < 32:
        raise ValueError('Receipt secret must contain at least 32 bytes')
    root = Path(root)
    app = web.Application(client_max_size=2 * 1024 * 1024)
    registry = ExtensionRegistry(root / 'validation', settings_path=False)

    def author(request):
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        who = authors.get(digest(token.encode()))
        if not who:
            raise web.HTTPUnauthorized(text='需要有效作者令牌')
        return identifier(who)

    def delete_token(item_id):
        return hmac.new(receipt_secret, item_id.encode(), hashlib.sha256).hexdigest()

    async def index(request):
        rows = []
        for file in sorted((root / 'packages').glob('*.json')):
            package = read(file, {})
            spec = package['spec']
            rows.append({'id': spec['id'], 'name': spec['name'], 'description': spec['description'],
                         'author': package['author'], 'version': package['version'], 'sha256': digest(canonical(package)),
                         'url': public_url.rstrip('/') + '/packages/' + file.name})
        return web.json_response({'worldmodel_index': 1, 'packages': rows})

    async def package_get(request):
        name = request.match_info['name']
        if not re.fullmatch(r'[a-zA-Z0-9._-]+\.json', name):
            raise web.HTTPBadRequest()
        file = root / 'packages' / name
        if not file.is_file():
            raise web.HTTPNotFound()
        return web.json_response(read(file, {}))

    async def publish(request):
        who = author(request)
        try:
            package = await request.json()
            if not isinstance(package, dict) or package.get('worldmodel_package') != 1 or not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?', package.get('version', '')):
                raise ValueError('无效插件包或版本')
            package_id = identifier(package['spec']['id'])
            owner = read(root / 'owners' / (package_id + '.json'), None)
            if owner and owner['author'] != who:
                raise web.HTTPForbidden(text='不能覆盖其他作者的插件')
            # Native package validator checks relative paths, syntax, file count and size.
            registry.create(package['spec'], by='registry-validation')
            package['author'] = who
            name = package_id + '-' + package['version'] + '.json'
            target = root / 'packages' / name
            if target.exists() and canonical(read(target, {})) != canonical(package):
                raise web.HTTPConflict(text='已发布的版本不可覆盖，请提高版本号')
            _atomic(root / 'owners' / (package_id + '.json'), {'author': who})
            _atomic(target, package)
            return web.json_response({'published': True, 'url': public_url.rstrip('/') + '/packages/' + name,
                                      'sha256': digest(canonical(package)), 'author': who})
        except (ValueError, KeyError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    async def collect(request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError('需要 JSON 对象')
            item_id = identifier(body['client_id'])
            if not re.fullmatch('[a-f0-9]{32}', item_id) or not re.fullmatch('[a-f0-9]{64}', body.get('client_secret', '')):
                raise ValueError('需要随机贡献编号和上传凭据')
            client_hash = digest(body['client_secret'].encode())
            payload = body['payload']
            if not isinstance(payload, dict) or payload.get('schema') != 1 or payload.get('kind') not in ('training', 'diagnostics'):
                raise ValueError('无效贡献格式')
            if digest(canonical(payload)) != body['sha256']:
                raise ValueError('贡献哈希不符')
            if payload['kind'] == 'training':
                if set(payload) - {'schema', 'kind', 'messages'} or not isinstance(payload.get('messages'), list):
                    raise ValueError('训练贡献只接受显式预览的消息')
                for message in payload['messages']:
                    if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant') or set(message) - {'role', 'text', 'feedback'}:
                        raise ValueError('贡献中包含非对话消息')
            elif set(payload) - {'schema', 'kind', 'stats'}:
                raise ValueError('诊断贡献只接受统计值')
            deleted = root / 'deleted' / (item_id + '.json')
            if deleted.exists():
                raise web.HTTPConflict(text='此贡献已撤回，不能重新上传')
            path = root / 'contributions' / (item_id + '.json')
            existing = read(path, None)
            if existing and (existing['sha256'] != body['sha256'] or not hmac.compare_digest(existing.get('client_hash', ''), client_hash)):
                raise web.HTTPConflict(text='贡献编号已存在')
            _atomic(path, {'payload': redact(payload), 'sha256': body['sha256'], 'client_hash': client_hash,
                           'received': time.time(), 'training_used': False})
            return web.json_response({'id': item_id, 'delete_token': delete_token(item_id), 'training_used': False})
        except (ValueError, KeyError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    async def remove(request):
        try:
            item_id = identifier(request.match_info['id'])
        except ValueError:
            raise web.HTTPBadRequest()
        supplied = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if not hmac.compare_digest(supplied, delete_token(item_id)):
            raise web.HTTPUnauthorized()
        (root / 'contributions' / (item_id + '.json')).unlink(missing_ok=True)
        _atomic(root / 'deleted' / (item_id + '.json'), {'deleted': time.time()})
        return web.json_response({'id': item_id, 'deleted': True, 'training_used': False})

    app.router.add_get('/index.json', index)
    app.router.add_get('/packages/{name}', package_get)
    app.router.add_post('/packages', publish)
    app.router.add_post('/contributions', collect)
    app.router.add_delete('/contributions/{id}', remove)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--public-url', required=True)
    parser.add_argument('--authors', type=Path, required=True, help='JSON SHA256(token) to stable author ID')
    parser.add_argument('--port', type=int, default=8792)
    args = parser.parse_args()
    secret = os.environ.get('WM_RECEIPT_SECRET', '').encode()
    web.run_app(create_app(args.home, args.public_url, read(args.authors, {}), secret), host='127.0.0.1', port=args.port)
