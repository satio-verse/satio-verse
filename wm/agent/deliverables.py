"""Run-bound file deliverables, execution evidence and human acceptance snapshots.

Only metadata and bounded text excerpts are stored. The caller supplies the task's
allowed workspace/worktree roots; model tools must not expose evidence or review
writers. A successful command is evidence of its exit code, not task acceptance.
"""
from __future__ import annotations

import codecs
import copy
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path

MAX_ARTIFACTS, MAX_EVIDENCE, MAX_REVIEWS = 100, 100, 100
MAX_VERSIONS = 20
MAX_METADATA_BYTES = 2_000_000
_LOCKS: dict[str, threading.RLock] = {}
_LOCK_GUARD = threading.Lock()
_TEXT = {'.txt', '.md', '.csv', '.tsv', '.json', '.jsonl', '.yaml', '.yml', '.toml', '.xml', '.html',
         '.py', '.js', '.ts', '.tsx', '.jsx', '.css', '.svg', '.sql', '.log', '.ini', '.rs', '.go', '.cpp', '.h'}


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
        raise ValueError('无效的会话或任务编号')
    return value


def _text(value, limit, name):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError(f'{name}必须是最多 {limit} 字的文字')
    return value.strip()


def _kind(path, header):
    if header.startswith(b'%PDF-'):
        return 'pdf', 'application/pdf'
    for prefix, mime in [(b'\x89PNG\r\n\x1a\n', 'image/png'), (b'\xff\xd8\xff', 'image/jpeg'),
                         (b'GIF87a', 'image/gif'), (b'GIF89a', 'image/gif'), (b'BM', 'image/bmp')]:
        if header.startswith(prefix):
            return 'image', mime
    if header.startswith(b'RIFF') and header[8:12] == b'WEBP':
        return 'image', 'image/webp'
    if path.suffix.lower() in _TEXT and b'\x00' not in header:
        return 'text', 'text/plain; charset=utf-8'
    return 'file', 'application/octet-stream'


def _within(path, roots):
    return any(path == root or path.is_relative_to(root) for root in roots)


def _capture(path, roots, *, preview=False, offset=0, limit=12000):
    """Hash a stable regular file while keeping at most one text page in memory."""
    source = Path(path)
    resolved = source.resolve(strict=True)
    if not _within(resolved, [Path(root) for root in roots]):
        raise PermissionError('成果路径已超出本任务允许的工作区或工作副本')
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise ValueError('只能登记普通文件')
    with resolved.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('只能登记普通文件')
        # Detect a retarget/replacement between resolution and opening, before reading.
        after_open = source.resolve(strict=True)
        current = after_open.stat()
        if after_open != resolved or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError('成果路径正在变化，请稍后重试')
        digest = hashlib.sha256()
        decoder = codecs.getincrementaldecoder('utf-8-sig')('surrogateescape')
        pieces, seen, kept, invalid_bytes = [], 0, 0, 0
        replacements = {code: 0xfffd for code in range(0xdc80, 0xdd00)}
        kind, mime = 'file', 'application/octet-stream'
        first = True
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            if first:
                kind, mime = _kind(resolved, chunk[:8192])
                first = False
            if preview and kind == 'text':
                text = decoder.decode(chunk)
                invalid_bytes += sum(0xdc80 <= ord(char) <= 0xdcff for char in text)
                text = text.translate(replacements)
                start = max(0, offset - seen)
                if start < len(text) and kept < limit:
                    part = text[start:start + limit - kept]
                    pieces.append(part)
                    kept += len(part)
                seen += len(text)
        if first:
            kind, mime = _kind(resolved, b'')
        if preview and kind == 'text':
            tail = decoder.decode(b'', final=True)
            invalid_bytes += sum(0xdc80 <= ord(char) <= 0xdcff for char in tail)
            tail = tail.translate(replacements)
            start = max(0, offset - seen)
            if start < len(tail) and kept < limit:
                pieces.append(tail[start:start + limit - kept])
            seen += len(tail)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('成果文件在读取期间发生变化，请稍后重试')
    fingerprint = {'size': after.st_size, 'mtime': after.st_mtime, 'mtime_ns': after.st_mtime_ns,
                   'sha256': digest.hexdigest()}
    page = None
    if preview and kind == 'text':
        text = ''.join(pieces)
        more = offset + len(text) < seen
        page = {'text': text, 'offset': offset, 'next_offset': offset + len(text) if more else None,
                'truncated': more, 'total_chars': seen, 'encoding': 'utf-8', 'invalid_bytes': invalid_bytes,
                'encoding_note': '非 UTF-8 字节已显示为替代字符' if invalid_bytes else ''}
    return fingerprint, kind, mime, page


class DeliveryStore:
    def __init__(self, home: Path):
        self.home = (Path(home) / 'deliveries').resolve()
        with _LOCK_GUARD:
            self._lock = _LOCKS.setdefault(os.path.normcase(str(self.home)), threading.RLock())

    def _path(self, session_id, run_id):
        path = self.home / _identifier(session_id) / (_identifier(run_id) + '.json')
        if not path.resolve().is_relative_to(self.home) or path.is_symlink():
            raise ValueError('成果记录目录无效')
        return path

    def _load(self, session_id, run_id):
        path = self._path(session_id, run_id)
        if not path.exists():
            return {'version': 1, 'session_id': session_id, 'run_id': run_id, 'artifacts': [], 'evidence': [], 'reviews': []}
        try:
            if path.stat().st_size > MAX_METADATA_BYTES:
                raise ValueError('成果记录过大')
            state = json.loads(path.read_text('utf-8'))
            if (not isinstance(state, dict) or state.get('version') != 1 or state.get('session_id') != session_id
                    or state.get('run_id') != run_id):
                raise ValueError('成果记录归属无效')
            for key, maximum in [('artifacts', MAX_ARTIFACTS), ('evidence', MAX_EVIDENCE), ('reviews', MAX_REVIEWS)]:
                if not isinstance(state.get(key), list) or len(state[key]) > maximum or any(not isinstance(x, dict) for x in state[key]):
                    raise ValueError('成果记录结构无效')
            for item in state['artifacts']:
                if (item.get('session_id') != session_id or item.get('run_id') != run_id
                        or not isinstance(item.get('path'), str) or not isinstance(item.get('_roots'), list)
                        or not item['_roots'] or any(not isinstance(root, str) or not Path(root).is_absolute() for root in item['_roots'])
                        or not isinstance(item.get('registered'), dict)
                        or not re.fullmatch(r'[a-f0-9]{64}', str(item['registered'].get('sha256', '')))):
                    raise ValueError('成果文件记录无效')
                versions = item.get('versions', [])
                if (not isinstance(versions, list) or len(versions) > MAX_VERSIONS
                        or any(not isinstance(version, dict)
                               or not re.fullmatch(r'[a-f0-9]{64}', str(version.get('sha256', ''))) for version in versions)):
                    raise ValueError('成果版本记录无效')
            return state
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError('成果记录无法读取，未按空记录覆盖') from exc

    def _save(self, state):
        path = self._path(state['session_id'], state['run_id'])
        payload = json.dumps(state, ensure_ascii=False, indent=2)
        if len(payload.encode('utf-8')) > MAX_METADATA_BYTES:
            raise ValueError('成果元数据已达到容量上限')
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('w', encoding='utf-8', newline='\n') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()

    @staticmethod
    def _public(item):
        return copy.deepcopy({k: v for k, v in item.items() if not k.startswith('_')})

    def register(self, session_id, run_id, workspace, path, label='', kind='', *, allowed_roots=()):
        _identifier(session_id)
        _identifier(run_id)
        label = _text(label, 200, '成果名称')
        if kind not in ('', 'text', 'image', 'pdf', 'file'):
            raise ValueError('不支持的成果类型')
        workspace = Path(workspace).resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError('工作区不存在')
        if not isinstance(allowed_roots, (list, tuple)) or len(allowed_roots) > 16:
            raise ValueError('允许的工作副本目录无效')
        roots = [workspace, *(Path(root).resolve(strict=True) for root in allowed_roots)]
        if any(not root.is_dir() for root in roots):
            raise ValueError('允许的工作副本目录不存在')
        source = Path(path)
        if not source.is_absolute():
            source = workspace / source
        source = Path(os.path.abspath(source))
        fingerprint, actual_kind, mime, _ = _capture(source, roots)
        if kind and kind != actual_kind:
            raise ValueError(f'文件实际类型为 {actual_kind}，与登记类型不符')
        with self._lock:
            state = self._load(session_id, run_id)
            normalized = os.path.normcase(os.path.normpath(str(source)))
            matches = [item for item in state['artifacts']
                       if os.path.normcase(os.path.normpath(item['path'])) == normalized]
            # Existing older stores may have several IDs for this path. Keep
            # those IDs for historical reviews, and update the newest one only.
            existing = max(matches, key=lambda item: item['created']) if matches else None
            if existing:
                new_label = label or existing['label']
                if (existing['registered'] == fingerprint and existing['label'] == new_label
                        and existing['kind'] == actual_kind and existing['mime'] == mime):
                    return self._public(existing)
                versions = existing.get('versions', [])
                if existing['registered']['sha256'] != fingerprint['sha256']:
                    versions = (versions + [{**existing['registered'],
                                             'recorded_at': existing.get('updated', existing['created'])}])[-MAX_VERSIONS:]
                existing.update(registered=fingerprint, versions=versions, label=new_label,
                                kind=actual_kind, mime=mime, updated=time.time(),
                                _roots=list(dict.fromkeys(map(str, roots))))
                self._save(state)
                return self._public(existing)
            if len(state['artifacts']) >= MAX_ARTIFACTS:
                raise ValueError(f'每个任务最多登记 {MAX_ARTIFACTS} 个文件成果')
            now = time.time()
            item = {'id': f'a-{run_id}-{uuid.uuid4().hex[:16]}', 'session_id': session_id, 'run_id': run_id,
                    'path': str(source), 'name': source.name, 'label': label or source.name,
                    'kind': actual_kind, 'mime': mime, 'registered': fingerprint, 'created': now, 'updated': now, 'versions': [],
                    '_roots': list(dict.fromkeys(map(str, roots)))}
            state['artifacts'].append(item)
            self._save(state)
            return self._public(item)

    def _artifact(self, session_id, artifact_id, run_id=None):
        if not isinstance(artifact_id, str) or not re.fullmatch(r'a-[A-Za-z0-9_-]{1,100}-[a-f0-9]{16}', artifact_id):
            raise ValueError('无效的成果编号')
        owner_run = artifact_id[2:].rsplit('-', 1)[0]
        if run_id is not None and owner_run != run_id:
            raise ValueError('成果不属于当前任务')
        with self._lock:
            state = self._load(session_id, owner_run)
            item = next((x for x in state['artifacts'] if x.get('id') == artifact_id), None)
            if item is None:
                raise ValueError('成果不存在或不属于当前会话')
            return copy.deepcopy(item)

    def _inspect(self, item, *, preview=False, offset=0, limit=12000):
        out = self._public(item)
        try:
            current, kind, mime, page = _capture(item['path'], item['_roots'], preview=preview, offset=offset, limit=limit)
        except (OSError, ValueError, RuntimeError) as exc:
            return {**out, 'current': None, 'status': 'missing' if isinstance(exc, FileNotFoundError) else 'unavailable',
                    'changed': None, 'error': str(exc)[:500], 'preview': None}
        changed = current['sha256'] != item['registered']['sha256']
        return {**out, 'current': current, 'current_kind': kind, 'current_mime': mime,
                'status': 'changed' if changed else 'current', 'changed': changed, 'error': '', 'preview': page}

    def inspect(self, session_id, artifact_id, *, run_id=None):
        return self._inspect(self._artifact(session_id, artifact_id, run_id))

    def read(self, session_id, artifact_id, *, run_id=None, offset=0, limit=12000):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20000:
            raise ValueError('预览分页参数无效；每页最多 20000 字')
        return self._inspect(self._artifact(session_id, artifact_id, run_id), preview=True, offset=offset, limit=limit)

    def list(self, session_id, run_id=None, *, offset=0, limit=50):
        _identifier(session_id)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('成果分页参数无效；每页最多 100 项')
        with self._lock:
            if run_id is None:
                folder = self._path(session_id, 'listing').parent
                runs = [path.stem for path in folder.glob('*.json')]
            else:
                runs = [_identifier(run_id)]
            items = [self._public(item) for run in runs for item in self._load(session_id, run)['artifacts']]
        items.sort(key=lambda item: item['created'], reverse=True)
        end = offset + limit
        return {'items': items[offset:end], 'total': len(items), 'offset': offset,
                'next_offset': end if end < len(items) else None}

    def _snapshots(self, session_id, run_id, artifact_ids, *, require_present=False):
        if (not isinstance(artifact_ids, (list, tuple)) or len(artifact_ids) > MAX_ARTIFACTS
                or any(not isinstance(aid, str) for aid in artifact_ids)):
            raise ValueError('成果编号列表无效')
        snapshots = []
        for aid in dict.fromkeys(artifact_ids):
            item = self.inspect(session_id, aid, run_id=run_id)
            if require_present and item['current'] is None:
                raise ValueError('成果文件不存在或无法读取，不能登记为已验收')
            snapshots.append({'id': aid, 'registered_sha256': item['registered']['sha256'],
                              **(item['current'] or {}), 'status': item['status']})
        return snapshots

    def record_evidence(self, session_id, run_id, *, tool_call_id, command, exit_code, output='', artifact_ids=()):
        """Trusted host call after a real tool result; do not expose as a model tool."""
        tool_call_id = _text(tool_call_id, 200, '工具调用编号')
        command = _text(command, 4000, '执行命令')
        if not tool_call_id or not command or (exit_code is not None and type(exit_code) is not int):
            raise ValueError('执行证据必须包含真实工具调用编号、命令和整数退出码或未知值')
        if not isinstance(output, str):
            raise ValueError('工具输出必须是文字')
        snapshots = self._snapshots(session_id, run_id, artifact_ids)
        excerpt = output if len(output) <= 6000 else output[:3000] + '\n…[输出已截断]…\n' + output[-3000:]
        with self._lock:
            state = self._load(session_id, run_id)
            if len(state['evidence']) >= MAX_EVIDENCE:
                raise ValueError('本任务执行证据数量已达到上限')
            item = {'id': 'e-' + uuid.uuid4().hex, 'session_id': session_id, 'run_id': run_id, 'source': 'tool_result',
                    'tool_call_id': tool_call_id, 'command': command, 'exit_code': exit_code,
                    'status': 'incomplete' if exit_code is None else 'succeeded' if exit_code == 0 else 'failed',
                    'output': excerpt, 'output_truncated': len(output) > 6000, 'artifact_snapshots': snapshots,
                    'created': time.time(), 'scope': 'command_execution_only'}
            state['evidence'].append(item)
            self._save(state)
            return copy.deepcopy(item)

    def review(self, session_id, run_id, *, accepted, note='', artifact_ids=None, expected_versions=None):
        """User control plane only: acceptance is bound to the files actually inspected."""
        if type(accepted) is not bool:
            raise ValueError('验收结果必须是布尔值')
        note = _text(note, 2000, '验收说明')
        if artifact_ids is None:
            artifact_ids = [x['id'] for x in self.list(session_id, run_id, limit=100)['items']]
        if not artifact_ids:
            raise ValueError('请先登记需要验收的文件成果')
        snapshots = self._snapshots(session_id, run_id, artifact_ids, require_present=accepted)
        if expected_versions is not None:
            actual = {item['id']: item.get('sha256') for item in snapshots}
            if (not isinstance(expected_versions, dict) or set(expected_versions) != set(actual)
                    or any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value)
                           or value != actual[key] for key, value in expected_versions.items())):
                raise ValueError('成果已变化，请重新预览后验收')
        with self._lock:
            state = self._load(session_id, run_id)
            if len(state['reviews']) >= MAX_REVIEWS:
                raise ValueError('本任务验收记录数量已达到上限')
            item = {'id': 'r-' + uuid.uuid4().hex, 'session_id': session_id, 'run_id': run_id, 'source': 'user',
                    'accepted': accepted, 'note': note, 'artifact_snapshots': snapshots, 'created': time.time()}
            state['reviews'].append(item)
            self._save(state)
            return copy.deepcopy(item)

    def state(self, session_id, run_id, *, refresh=False):
        with self._lock:
            state = self._load(session_id, run_id)
        artifacts = [self._inspect(item) if refresh else self._public(item) for item in state['artifacts']]
        current = {item['id']: item.get('current') for item in artifacts}
        reviews = copy.deepcopy(state['reviews'])
        for review in reviews:
            review['stale'] = (any(not current.get(item['id']) or current[item['id']]['sha256'] != item.get('sha256')
                                   for item in review['artifact_snapshots']) if refresh else None)
        return {'session_id': session_id, 'run_id': run_id, 'artifacts': artifacts,
                'evidence': copy.deepcopy(state['evidence']), 'reviews': reviews}
