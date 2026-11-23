"""Local-first contribution, Creator distribution and staged releases.

No background upload, automatic consent, package execution or live native replacement.
Network destinations and release trust are explicitly configured by the user/operator.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .agent import install_gate
from .agent.extensions import _atomic, _read_json, _relative, _within, ID

MAX_WIRE = 24 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read(path, default):
    if not path.exists():
        return copy.deepcopy(default)
    if path.stat().st_size > MAX_WIRE:
        raise ValueError('平台状态文件过大')
    return json.loads(path.read_text('utf-8'))


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,80}', value):
        raise ValueError('无效编号')
    return value


def https_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError('需要有效 HTTPS 地址')
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.fragment:
        raise ValueError('远程地址必须使用 HTTPS，且不包含凭据或片段')
    return url


async def remote_json(url, *, method='GET', body=None, token='', limit=MAX_WIRE):
    """Pin resolved public IPs to the connector; never follow cross-host redirects."""
    import asyncio
    import aiohttp
    from aiohttp.abc import AbstractResolver
    https_url(url)
    p = urlsplit(url)
    infos = await asyncio.get_running_loop().getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
    addresses = {(family, address[0]) for family, _, _, _, address in infos}
    if not addresses or any(not ipaddress.ip_address(address).is_global for _, address in addresses):
        raise ValueError('远程服务必须解析到公网地址')
    class PinnedResolver(AbstractResolver):
        async def resolve(self, host, port=0, family=socket.AF_INET):
            if host != p.hostname:
                raise ValueError('服务域名变化')
            return [{'hostname': host, 'host': address, 'port': port, 'family': f,
                     'proto': 0, 'flags': socket.AI_NUMERICHOST} for f, address in addresses]
        async def close(self):
            pass
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PinnedResolver()),
                                    timeout=aiohttp.ClientTimeout(total=30)) as client:
        async with client.request(method, url, json=body, headers=headers, allow_redirects=False) as response:
            if response.status not in (200, 201):
                raise ValueError(f'远程服务返回 HTTP {response.status}')
            raw = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw) > limit:
                    raise ValueError('远程响应超过大小上限')
            return json.loads(raw)


def redact(value):
    """Conservative structured filtering plus visible text redaction; user still previews."""
    if isinstance(value, dict):
        return {str(k): ('[已移除]' if re.search(r'(?i)token|secret|password|cookie|authorization|api.?key', str(k)) else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value[:200]]
    if not isinstance(value, str):
        return value
    text = value[:100000]
    patterns = [r'(?i)(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}',
                r'(?i)\bBearer\s+[A-Za-z0-9._~-]+',
                r'(?i)\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*[^\s,;]+',
                r'(?i)\b[A-Z]:\\[^\r\n<>"|]+', r'/Users/[^\s]+', r'/home/[^\s]+',
                r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', r'(?<!\d)1[3-9]\d{9}(?!\d)']
    for pattern in patterns:
        text = re.sub(pattern, '[已脱敏]', text)
    return text


class ContributionStore:
    def __init__(self, home):
        self.root = Path(home) / 'platform' / 'contributions'

    def settings(self):
        return read(self.root / 'settings.json', {'diagnostics': False, 'training': False, 'endpoint': '', 'revision': 0})

    def configure(self, values):
        state = self.settings()
        for key in ('diagnostics', 'training'):
            if key in values:
                if not isinstance(values[key], bool):
                    raise ValueError('同意选项必须为布尔值')
                state[key] = values[key]
        if 'endpoint' in values:
            state['endpoint'] = https_url(values['endpoint']) if values['endpoint'] else ''
        state['revision'] += 1
        state['updated'] = time.time()
        _atomic(self.root / 'settings.json', state)
        # Revocation also removes payload from all unsent contributions of that class.
        for item in self.items():
            if item['state'] == 'queued' and not state[item['kind']]:
                self.withdraw(item['id'])
        return state

    def preview(self, session, kind, event_id=None):
        if kind not in ('training', 'diagnostics'):
            raise ValueError('未知贡献类型')
        if kind == 'diagnostics':
            stats = getattr(session, 'stats', {}) or {}
            payload = {'schema': 1, 'kind': kind, 'stats': {k: stats[k] for k in
                       ('rounds', 'steps', 'input', 'output', 'cache_hit', 'cost') if k in stats}}
        else:
            events = [e for e in session.events if e.get('type') in ('user', 'assistant') and e.get('text')]
            if event_id:
                position = next((i for i, e in enumerate(events) if str(e.get('message_id') or e.get('id')) == event_id), None)
                if position is None:
                    raise ValueError('没有找到此消息')
                start = position
                if events[position]['type'] == 'assistant':
                    start = next((i for i in range(position - 1, -1, -1) if events[i]['type'] == 'user'), position)
                events = events[start:position + 1]
            payload = {'schema': 1, 'kind': kind, 'messages': [
                {'role': e['type'], 'text': redact(e['text'][:4000]), 'feedback': redact(e.get('feedback'))}
                for e in events[-100:]]}
            while len(canonical(payload)) > 1024 * 1024 and len(payload['messages']) > 1:
                payload['messages'].pop(0)
        return {'payload': payload, 'event_id': event_id, 'sha256': digest(canonical(payload)),
                'notice': '请检查脱敏结果；最多显示最近 100 条正文，每条最多 4000 字。未包含附件、工具输出与系统提示；通用脱敏无法识别所有隐私。'}

    def queue(self, preview, expected_hash):
        payload = preview['payload']
        if digest(canonical(payload)) != expected_hash:
            raise ValueError('预览内容已变化，请重新检查后提交')
        settings = self.settings()
        if not settings[payload['kind']]:
            raise ValueError('请先单独开启此类型的数据贡献')
        item = {'id': uuid.uuid4().hex, 'kind': payload['kind'], 'state': 'queued',
                'created': time.time(), 'consent_revision': settings['revision'],
                'payload': payload, 'sha256': expected_hash}
        _atomic(self.root / 'items' / (item['id'] + '.json'), item)
        return item

    def items(self):
        return [read(p, {}) for p in sorted((self.root / 'items').glob('*.json'))][-200:]

    def item(self, item_id):
        item = read(self.root / 'items' / (identifier(item_id) + '.json'), None)
        if item is None:
            raise ValueError('没有这份贡献')
        return item

    def withdraw(self, item_id):
        item = self.item(item_id)
        if item['state'] == 'sent':
            raise ValueError('此贡献已上传，请使用远程删除并等待删除回执')
        item.pop('payload', None)
        item.pop('upload_secret', None)
        item.update(state='withdrawn', withdrawn=time.time())
        _atomic(self.root / 'items' / (item['id'] + '.json'), item)
        return item

    async def send(self, item_id, transport=remote_json):
        item, settings = self.item(item_id), self.settings()
        if item['state'] != 'queued' or not settings[item['kind']]:
            raise ValueError('贡献已撤回、已发送，或当前未授权')
        if not settings['endpoint']:
            raise ValueError('尚未配置数据接收服务；内容仍保存在本机')
        # Persist a per-upload secret before sending, so retries are idempotent but
        # someone who knows the public sample and client ID cannot claim its receipt.
        item.setdefault('upload_secret', uuid.uuid4().hex + uuid.uuid4().hex)
        _atomic(self.root / 'items' / (item['id'] + '.json'), item)
        receipt = await transport(settings['endpoint'].rstrip('/') + '/contributions', method='POST',
                                  body={'client_id': item['id'], 'client_secret': item['upload_secret'],
                                        'sha256': item['sha256'], 'payload': item['payload']})
        if not isinstance(receipt, dict) or not receipt.get('id') or not receipt.get('delete_token'):
            raise ValueError('接收服务没有返回可撤回的有效回执')
        identifier(receipt['id'])
        if not isinstance(receipt['delete_token'], str) or not re.fullmatch('[A-Za-z0-9_-]{32,512}', receipt['delete_token']):
            raise ValueError('接收服务返回了无效删除令牌')
        item.pop('payload', None)
        item.pop('upload_secret', None)
        item.update(state='sent', sent=time.time(), receipt=receipt, endpoint=settings['endpoint'])
        _atomic(self.root / 'items' / (item['id'] + '.json'), item)
        return item

    async def delete_remote(self, item_id, transport=remote_json):
        item = self.item(item_id)
        if item['state'] != 'sent':
            return self.withdraw(item_id)
        receipt = item['receipt']
        result = await transport(item['endpoint'].rstrip('/') + '/contributions/' + identifier(receipt['id']),
                                 method='DELETE', token=receipt['delete_token'])
        if not isinstance(result, dict) or result.get('deleted') is not True:
            raise ValueError('远程服务未确认删除')
        item.update(state='deleted', deleted=time.time())
        item['receipt'] = {'id': receipt['id'], 'deleted': True}
        _atomic(self.root / 'items' / (item['id'] + '.json'), item)
        return item

    def state(self):
        return {'settings': self.settings(), 'items': [{k: v for k, v in row.items() if k not in ('payload', 'receipt', 'upload_secret')}
                                                     for row in self.items()]}


class CommunityStore:
    def __init__(self, home, extensions):
        self.root = Path(home) / 'platform' / 'community'
        self.extensions = extensions

    def export(self, package_id, author, version, revision=None):
        if not isinstance(author, str) or not author.strip() or len(author) > 80:
            raise ValueError('请填写作者名（1～80 字）')
        if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?', version):
            raise ValueError('版本格式为 1.0.0 或 1.0.0-beta')
        _, manifest, folder = self.extensions._revision(package_id, revision)
        spec = {k: manifest[k] for k in ('id', 'name', 'description', 'mcp', 'ui')}
        spec['files'] = {name: (folder / name).read_text('utf-8') for name in manifest['sha256']}
        if manifest.get('validation', {}).get('kind') == 'plugin' and install_gate.MANIFEST_NAME in spec['files']:
            try:  # 世界插件把清单带回去，别人装的时候才认得出它是世界
                spec['plugin'] = json.loads(spec['files'][install_gate.MANIFEST_NAME])
            except ValueError:
                pass
        package = {'worldmodel_package': 1, 'author': author.strip(), 'version': version,
                   'revision': manifest['revision'], 'spec': spec}
        blob = canonical(package)
        row = {'id': package_id, 'name': spec['name'], 'description': spec['description'],
               'author': author.strip(), 'version': version, 'sha256': digest(blob), 'bytes': len(blob),
               'file': f'{package_id}-{version}.json', 'scope': 'local', 'created': time.time()}
        target = self.root / 'exports' / row['file']
        if target.exists() and target.read_bytes() != blob:
            raise ValueError('该版本已经导出过不同内容，请提高版本号')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        index = read(self.root / 'exports' / 'index.json', {'worldmodel_index': 1, 'packages': []})
        index['packages'] = [p for p in index['packages'] if (p['id'], p['version']) != (package_id, version)] + [row]
        _atomic(self.root / 'exports' / 'index.json', index)
        return {'package': package, 'entry': row, 'index': index, 'path': str(target),
                'published': False, 'notice': '已生成发布包，尚未上传公共社区。'}

    def sources(self):
        return read(self.root / 'sources.json', [])

    def add_source(self, source):
        if not isinstance(source, str) or not source.strip():
            raise ValueError('请填写索引 URL 或本机 index.json 绝对路径')
        if source.startswith('https://'):
            https_url(source)
        elif not Path(source).is_absolute() or not Path(source).is_file():
            raise ValueError('本地索引需要存在的绝对文件路径')
        sources = self.sources()
        if source not in [s['source'] for s in sources]:
            sources.append({'id': uuid.uuid4().hex, 'source': source})
            _atomic(self.root / 'sources.json', sources)
        return sources

    async def refresh(self, source_id, transport=remote_json):
        source = next((s['source'] for s in self.sources() if s['id'] == source_id), None)
        if source is None:
            raise ValueError('没有这个索引源')
        index = await transport(source) if source.startswith('https://') else read(Path(source), {})
        if not isinstance(index, dict) or index.get('worldmodel_index') != 1 or not isinstance(index.get('packages'), list) or len(index['packages']) > 1000:
            raise ValueError('不是 WorldModel 插件索引')
        entries = []
        for raw in index['packages']:
            if not isinstance(raw, dict) or not ID.fullmatch(str(raw.get('id', ''))) or not re.fullmatch('[a-f0-9]{64}', str(raw.get('sha256', ''))):
                raise ValueError('插件索引包含无效编号或哈希')
            item = {k: raw[k] for k in ('id', 'name', 'description', 'author', 'version', 'sha256') if k in raw}
            item['source_id'] = source_id
            if source.startswith('https://'):
                item['url'] = https_url(raw.get('url', ''))
            else:
                name = _relative(raw.get('file', ''))
                file = Path(source).parent / name
                if not _within(file, Path(source).parent):
                    raise ValueError('插件文件越出索引目录')
                item['file'] = str(file)
            entries.append(item)
        _atomic(self.root / 'cache' / (identifier(source_id) + '.json'), entries)
        return entries

    def entries(self):
        return [entry for source in self.sources() for entry in read(self.root / 'cache' / (source['id'] + '.json'), [])]

    async def _fetch(self, source_id, package_id, version, transport):
        """取包并核对哈希/编号/版本。装和预览都走这里，两边看到的是同一份东西。"""
        entry = next((e for e in self.entries() if e['source_id'] == source_id and e['id'] == package_id and e['version'] == version), None)
        if entry is None:
            raise ValueError('请先刷新索引并选择插件版本')
        package = await transport(entry['url']) if 'url' in entry else read(Path(entry['file']), {})
        if not isinstance(package, dict) or digest(canonical(package)) != entry['sha256'] or package.get('worldmodel_package') != 1:
            raise ValueError('插件包与索引哈希不符')
        if package.get('spec', {}).get('id') != package_id or package.get('version') != version:
            raise ValueError('插件包编号或版本不符')
        return entry, package

    async def inspect(self, source_id, package_id, version, transport=remote_json):
        """装之前先看契约：这个包会要什么、加什么动作、能不能过。不写盘、不执行。"""
        _, package = await self._fetch(source_id, package_id, version, transport)
        spec = package.get('spec') if isinstance(package.get('spec'), dict) else {}
        files = spec.get('files') if isinstance(spec.get('files'), dict) else None
        report = install_gate.inspect(spec, source='third_party', files=files)
        return {'id': package_id, 'version': version, 'author': package.get('author'), 'report': report}

    async def install(self, source_id, package_id, version, transport=remote_json):
        _, package = await self._fetch(source_id, package_id, version, transport)
        spec = package.get('spec') if isinstance(package.get('spec'), dict) else {}
        files = spec.get('files') if isinstance(spec.get('files'), dict) else None
        report = install_gate.inspect(spec, source='third_party', files=files)
        if not report['ok']:
            raise ValueError('这个包不符合%s，没装：\n%s' % (install_gate.CONTRACT_NAME, report['errors_text']))
        result = self.extensions.create(spec, by='community:' + str(package.get('author', ''))[:60])
        plugin = None
        if report['kind'] == 'plugin':
            plugin = install_gate.install_plugin_package(spec, source='third_party')
        notice = '已导入 Creator 草稿；请检查后在我的扩展中启用。'
        if plugin is not None:
            notice = ('已装进插件宿主（%s），默认停着；到插件页打开它。'
                      % ('授权档 ' + str(plugin.get('tier')) if plugin.get('ok') else plugin.get('why') or '没装进宿主'))
        return {'installed': result, 'enabled': False, 'kind': report['kind'],
                'contract': report, 'plugin': plugin, 'notice': notice}

    async def publish(self, package_id, version, endpoint, token, transport=remote_json):
        if not token:
            raise ValueError('发布需要社区作者令牌')
        identifier(package_id)
        if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?', version):
            raise ValueError('版本无效')
        package = read(self.root / 'exports' / f'{package_id}-{version}.json', None)
        if package is None:
            raise ValueError('请先预览并导出发布包')
        result = await transport(https_url(endpoint).rstrip('/') + '/packages', method='POST', body=package, token=token)
        if not isinstance(result, dict) or result.get('published') is not True or not result.get('url'):
            raise ValueError('服务未确认公开发布')
        return result

    def state(self):
        return {'sources': self.sources(), 'entries': self.entries(),
                'exports': read(self.root / 'exports' / 'index.json', {'packages': []})['packages'],
                'public_service': 'not_configured', 'creator': self.extensions._state()['packages']}


class ReleaseUpdater:
    """Small signed/pinned bundles, journaled file swap and conservative rollback.

    Native/backend files can only be applied by the offline CLI, after processes stop.
    Components retain their existing independent large-file download manager.
    """
    def __init__(self, home, install_root, busy=lambda: False):
        self.root = Path(home) / 'platform' / 'updates'
        self.install_root = Path(install_root).resolve()
        self.busy = busy

    def state(self):
        state = read(self.root / 'state.json', {'state': 'idle', 'history': []})
        state['trust'] = read(self.root / 'trust.json', {'manifest_sha256': '', 'source': '', 'public_key': ''})
        return state

    def trust(self, source, sha256='', public_key=''):
        if not all(isinstance(value, str) for value in (source, sha256, public_key)):
            raise ValueError('更新来源、哈希和公钥必须为文字')
        if source.startswith('https://'):
            https_url(source)
        elif not Path(source).is_absolute() or not Path(source).is_file():
            raise ValueError('更新清单需要 HTTPS 地址或存在的绝对文件路径')
        if not re.fullmatch('[a-f0-9]{64}', sha256 or '') and not public_key:
            raise ValueError('请提供从发布方独立取得的清单 SHA256 或 Ed25519 公钥')
        _atomic(self.root / 'trust.json', {'source': source, 'manifest_sha256': sha256, 'public_key': public_key})

    def verify(self, bundle):
        if not isinstance(bundle, dict) or bundle.get('worldmodel_release') != 1:
            raise ValueError('无效更新包')
        trust = self.state()['trust']
        signed = {k: v for k, v in bundle.items() if k != 'signature'}
        raw = canonical(signed)
        valid = bool(trust['manifest_sha256']) and digest(raw) == trust['manifest_sha256']
        if not valid and trust['public_key']:
            try:
                from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
                Ed25519PublicKey.from_public_bytes(base64.b64decode(trust['public_key'], validate=True)).verify(
                    base64.b64decode(bundle.get('signature', ''), validate=True), raw)
                valid = True
            except ImportError as exc:
                raise ValueError('签名验证需要安装 cryptography；没有降级为未验证更新') from exc
            except Exception as exc:
                raise ValueError('更新包签名无效') from exc
        if not valid:
            raise ValueError('更新包不匹配已信任清单')
        version = bundle.get('version')
        if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', version):
            raise ValueError('更新版本无效')
        files = bundle.get('files')
        if not isinstance(files, list) or not files or len(files) > 500:
            raise ValueError('更新文件清单无效')
        total, seen, decoded, restart = 0, set(), {}, False
        for item in files:
            name = _relative(item['path'])
            if name.casefold() in seen:
                raise ValueError('更新包含重名文件')
            seen.add(name.casefold())
            hot = name.startswith('desktop/web/') and Path(name).suffix.lower() in ('.js', '.css', '.html', '.json', '.svg')
            cold = name.startswith('wm/') and name.endswith('.py') or name.startswith('desktop/build/bin/') and name.endswith(('.exe', '.dll'))
            if not hot and not cold:
                raise ValueError('更新文件不属于界面、后端或桌面程序')
            restart |= not hot
            content = base64.b64decode(item['content'], validate=True)
            total += len(content)
            if total > 16 * 1024 * 1024 or digest(content) != item['sha256']:
                raise ValueError('更新文件超限或哈希不符')
            decoded[name] = content
        return decoded, restart

    async def stage_source(self, transport=remote_json):
        source = self.state()['trust']['source']
        if not source:
            raise ValueError('尚未配置可信更新来源')
        bundle = await transport(source) if source.startswith('https://') else read(Path(source), {})
        self.verify(bundle)
        _atomic(self.root / 'staged.json', bundle)
        state = self.state()
        state.pop('trust', None)
        state.update(state='staged', version=bundle['version'], requires_restart=self.verify(bundle)[1],
                     files=[{'path': f['path'], 'sha256': f['sha256']} for f in bundle['files']])
        _atomic(self.root / 'state.json', state)
        return state

    def target(self, name):
        target = self.install_root / _relative(name)
        if not _within(target, self.install_root):
            raise ValueError('更新目标越出安装目录')
        current = target
        while current != self.install_root:
            if current.is_symlink() or getattr(current, 'is_junction', lambda: False)():
                raise ValueError('更新目标不能经过链接目录')
            current = current.parent
        return target

    def apply(self, offline=False):
        if self.busy():
            raise ValueError('仍有任务正在执行，更新已暂存；空闲后重试')
        if self.state().get('state') != 'staged':
            raise ValueError('没有待应用的新更新；请先检查并暂存')
        bundle = read(self.root / 'staged.json', {})
        files, restart = self.verify(bundle)
        if restart and not offline:
            return {'state': 'restart_required', 'version': bundle['version'], 'applied': False,
                    'notice': '含后端或桌面程序，请停止程序后使用离线更新工具；不会覆盖运行中的进程。'}
        if (self.root / 'journal.json').exists():
            raise ValueError('存在未完成的更新，请先恢复或回滚')
        records = []
        for name, content in files.items():
            target = self.target(name)
            old = target.read_bytes() if target.exists() else None
            records.append({'path': name, 'before': None if old is None else base64.b64encode(old).decode(),
                            'after_sha256': digest(content)})
        journal = {'id': uuid.uuid4().hex, 'version': bundle['version'], 'files': records, 'created': time.time()}
        _atomic(self.root / 'journal.json', journal)
        try:
            for name, content in files.items():
                target = self.target(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(target.name + '.update-' + journal['id'])
                try:
                    temp.write_bytes(content)
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
            _atomic(self.root / 'backups' / (journal['id'] + '.json'), journal)
            state = self.state()
            state.pop('trust', None)
            state.update(state='applied', version=bundle['version'], applied=time.time(), reload_required=True)
            state['history'] = (state.get('history', []) + [{'id': journal['id'], 'version': bundle['version']}])[-20:]
            _atomic(self.root / 'state.json', state)
            (self.root / 'journal.json').unlink()
            return state
        except Exception:
            self.restore(journal, check_current=False)
            raise

    def restore(self, journal, check_current=True):
        if self.busy():
            raise ValueError('任务执行中无法回滚')
        # Check every file before changing any: do not overwrite later user edits.
        targets = [(record, self.target(record['path'])) for record in journal['files']]
        for record, path in targets:
            current = path.read_bytes() if path.is_file() else None
            is_after = current is not None and digest(current) == record['after_sha256']
            before = None if record['before'] is None else base64.b64decode(record['before'])
            if not is_after and (check_current or current != before):
                raise ValueError('安装文件后来被修改，未覆盖这些改动；请手动处理')
        for record, path in reversed(targets):
            if record['before'] is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(path.name + '.rollback-' + journal['id'])
                try:
                    temp.write_bytes(base64.b64decode(record['before']))
                    os.replace(temp, path)
                finally:
                    temp.unlink(missing_ok=True)
        (self.root / 'journal.json').unlink(missing_ok=True)
        return {'state': 'rolled_back', 'version': journal['version']}

    def rollback(self, offline=False):
        state = self.state()
        pending = read(self.root / 'journal.json', None)
        if pending is None and not state.get('history'):
            raise ValueError('没有可回滚更新')
        journal = pending or read(self.root / 'backups' / (state['history'][-1]['id'] + '.json'), None)
        if not offline and any(not f['path'].startswith('desktop/web/') for f in journal['files']):
            raise ValueError('后端和主程序更新需要停止程序后离线回滚')
        result = self.restore(journal, check_current=pending is None)
        state.pop('trust', None)
        state.update(result)
        if pending is None:
            state['history'].pop()
        _atomic(self.root / 'state.json', state)
        return result
