"""Bounded local document extraction; no rendering, macros, formulas or network IO.

Office XML is read directly from ZIP entries without extracting files. PDF uses
optional pypdf. Parsing runs in a disposable memory/time-limited process.
"""
from __future__ import annotations

import contextlib
from html.parser import HTMLParser
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET

MAX_BYTES = 32 * 1024 * 1024
MAX_STREAM_BYTES = 128 * 1024 * 1024  # QQ 大文件先分块下到临时文件再解析; 总大小仍要有上限
MAX_EXPANDED = 128 * 1024 * 1024
MAX_XML = 32 * 1024 * 1024
MAX_TEXT = 300_000
MAX_SECTIONS = 5000
MAX_PAGES = 200
MAX_SHEETS = 32
MAX_CELLS = 50_000
MAX_ROWS = 10_000
MAX_ARCHIVE_FILES = 2000
MAX_ARCHIVE_DEPTH = 2
TIMEOUT = 30
MEMORY_BYTES = 768 * 1024 * 1024  # 200 页 PDF 和压缩包里的文档一起解析时, 512 MB 的余量不够
CACHE_VERSION = 5
TEXT_SUFFIXES = {'.txt', '.md', '.markdown', '.csv', '.tsv', '.json', '.jsonl', '.yaml', '.yml', '.toml', '.xml',
                 '.html', '.htm', '.ini', '.cfg', '.conf', '.properties', '.tex', '.rst', '.log', '.sql',
                 '.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.css', '.scss', '.less',
                 '.cpp', '.cc', '.cxx', '.hpp', '.hxx', '.h', '.c', '.rs', '.go', '.java', '.kt', '.cs', '.php',
                 '.rb', '.lua', '.swift', '.vue', '.svelte', '.m', '.mm', '.r', '.jl', '.pl', '.pm', '.sh', '.bash',
                 '.zsh', '.fish', '.asm', '.s', '.v', '.sv', '.svh', '.vh', '.vhd', '.vhdl', '.dart', '.ex', '.exs',
                 '.erl', '.hrl', '.clj', '.cljs', '.cljc', '.scala', '.groovy', '.gradle', '.cmake', '.mk', '.ninja',
                 '.bazel', '.bzl', '.proto', '.graphql', '.gql', '.ics', '.vcf', '.xaml', '.axaml', '.qml', '.zig',
                 '.nim', '.cr', '.d', '.pas', '.fs', '.fsx', '.vb', '.sln', '.csproj', '.vcxproj', '.props', '.targets',
                 '.editorconfig', '.gitattributes', '.gitignore', '.dockerignore',
                 '.bat', '.cmd', '.ps1', '.srt', '.vtt'}
# 老式 Word / Excel / PowerPoint 和 WPS 的二进制格式: 都是 OLE 复合文档, 只读里面的文字, 不执行宏.
LEGACY_SUFFIXES = {'.doc', '.xls', '.ppt', '.wps', '.et', '.dps'}
DOCUMENT_SUFFIXES = {'.docx', '.xlsx', '.pptx', '.pdf', '.odt', '.ods', '.odp', '.rtf', '.epub',
                      '.ofd', '.eml', '.mhtml', '.mht', '.fb2', '.xmind'} | LEGACY_SUFFIXES
# 压缩包: ZIP / TAR 和常见的单文件压缩都用标准库读; 里面的文件再按各自的后缀解析, 不落到磁盘.
ARCHIVE_SUFFIXES = {'.zip', '.tar', '.tgz', '.tbz', '.tbz2', '.txz', '.tar.gz', '.tar.bz2', '.tar.xz',
                    '.gz', '.bz2', '.xz'}
READABLE_SUFFIXES = TEXT_SUFFIXES | DOCUMENT_SUFFIXES | ARCHIVE_SUFFIXES
IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
# 认得出、但本机没有安全解析器的格式: 说清楚缺什么, 不假装读过. 宏文档按原来的规矩不读.
UNREADABLE_SUFFIXES = {
    '.rar': 'RAR 压缩包只认出了格式；本机没有能解 RAR 的组件，内容还没解压，也没有读取。',
    '.7z': '7z 压缩包只认出了格式；本机没有能解 7z 的组件，内容还没解压，也没有读取。',
    '.docm': '这是启用了宏的 Word 文档；宏没有执行，本轮也不读含宏文档，内容还没读取。',
    '.xlsm': '这是启用了宏的 Excel 工作簿；宏没有执行，本轮也不读含宏文档，内容还没读取。',
    '.pptm': '这是启用了宏的 PowerPoint 演示文稿；宏没有执行，本轮也不读含宏文档，内容还没读取。',
}
FORMAT_NAMES = {'.txt': '文字文件', '.docx': 'Word 文档', '.xlsx': 'Excel 工作簿', '.pptx': 'PowerPoint 演示文稿',
                '.pdf': 'PDF 文档', '.odt': '文本文档', '.ods': '电子表格', '.odp': '演示文稿', '.rtf': 'RTF 文档',
                '.epub': 'EPUB 电子书', '.ofd': 'OFD 文档', '.eml': '邮件', '.mhtml': '网页邮件', '.mht': '网页邮件',
                '.fb2': 'FB2 电子书', '.xmind': 'XMIND 思维导图', '.doc': '老式 Word 文档', '.xls': '老式 Excel 表格',
                '.ppt': '老式 PowerPoint 演示文稿', '.wps': 'WPS 文档', '.et': 'WPS 表格', '.dps': 'WPS 演示文稿',
                '.zip': 'ZIP 压缩包', '.tar': 'TAR 压缩包', '.gz': 'GZ 压缩包', '.bz2': 'BZ2 压缩包',
                '.xz': 'XZ 压缩包', '.rar': 'RAR 压缩包', '.7z': '7z 压缩包'}


def _format_name(suffix):
    return FORMAT_NAMES.get(suffix, f'{suffix.lstrip(".") or "未知"} 格式')


class MaterialError(ValueError):
    pass


def _safe_member(name):
    """压缩包里的一项能不能读: 只接受普通的相对路径, 不碰越界、盘符、链接和超长名字."""
    path = PurePosixPath(name)
    return (bool(name) and len(name) <= 240 and '\\' not in name and ':' not in name
            and all(ord(c) >= 32 for c in name)
            and not path.is_absolute() and '..' not in path.parts)


def _plain_text(data):
    """没有后缀的文件 / 压缩包成员: 只有看着像文字才按文本读, 免得把二进制读成乱码."""
    sample = data[:65536]
    if b'\x00' in sample:
        return False
    text = sample.decode('utf-8', errors='ignore')
    if len(text) < 2:
        return False
    good = sum(c.isprintable() or c in '\n\r\t' for c in text)
    return good >= len(text) * 0.8


def looks_text(data):
    """给渠道认没有后缀的文件用: 看着像文字才按文本读, 不猜二进制."""
    return _plain_text(data)


class ArchiveBudget:
    """一个压缩包 (含嵌套) 一共能展开多少、读几项; 不是每层各算一份."""

    def __init__(self):
        self.bytes = MAX_EXPANDED
        self.files = MAX_ARCHIVE_FILES

    def take(self, size):
        if self.files <= 0:
            raise MaterialError('压缩包文件数超过本地解析上限')
        if size < 0 or size > self.bytes:
            raise MaterialError('压缩包展开大小超过本地解析上限')
        self.files -= 1
        self.bytes -= size


def sniff(data, suffix=''):
    """先认后缀; 后缀不认识时按文件头认几种常见容器和明显的普通文字, 不硬猜二进制."""
    suffix = (suffix or '').lower()
    if suffix in READABLE_SUFFIXES or suffix in UNREADABLE_SUFFIXES:
        return suffix
    if data.startswith(b'\xff\xfe') or data.startswith(b'\xfe\xff'):
        return '.txt'
    if data[:4] in (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08'):
        try:
            return _zip_kind(data)
        except MaterialError:
            return '.zip'
    if data[:8] == _OLE_MAGIC:
        return '.ole'
    if data[:5] == b'%PDF-':
        return '.pdf'
    if data[:5].lower() == b'{\\rtf':
        return '.rtf'
    if data[:2] == b'\x1f\x8b':
        return '.gz'
    if data[:3] == b'BZh':
        return '.bz2'
    if data[:6] == b'\xfd7zXZ\x00':
        return '.xz'
    if data[:6] == b'7z\xbc\xaf\x27\x1c':
        return '.7z'
    if data[:7] == b'Rar!\x1a\x07\x00' or data[:8] == b'Rar!\x1a\x07\x01\x00':
        return '.rar'
    if len(data) >= 262 and data[257:262] == b'ustar':
        return '.tar'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return '.png'
    if data[:3] == b'\xff\xd8\xff':
        return '.jpg'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return '.gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return '.webp'
    if _plain_text(data):  # 没有后缀的普通文字: 只认没有空字符、可打印字符够多的, 不硬猜二进制
        return '.txt'
    return ''


def _tag(node):
    return node.tag.rsplit('}', 1)[-1]


class Output:
    def __init__(self, format):
        self.result = {'format': format, 'parse_status': 'ready', 'text': '', 'sections': [], 'warnings': [], 'truncated': False}
        self.chunks = []
        self.chars = 0

    def warn(self, message):
        if message not in self.result['warnings'] and len(self.result['warnings']) < 30:
            self.result['warnings'].append(message)

    def add(self, kind, label, text, **source):
        if len(self.result['sections']) >= MAX_SECTIONS or self.chars >= MAX_TEXT:
            self.result['truncated'] = True
            return False
        chunk = f'[{label}]\n{text}\n\n'
        room = MAX_TEXT - self.chars
        if len(chunk) > room:
            chunk = chunk[:room]
            self.result['truncated'] = True
        self.result['sections'].append({'kind': kind, 'label': label, 'start': self.chars,
                                        'end': self.chars + len(chunk), **source})
        self.chunks.append(chunk)
        self.chars += len(chunk)
        return not self.result['truncated']

    def finish(self):
        self.result['text'] = ''.join(self.chunks)
        self.result['chars'] = len(self.result['text'])
        if self.result['truncated']:
            self.result['parse_status'] = 'partial'
            self.warn('内容超过本地提取上限，仅返回已标明来源的部分内容；未提取部分尚未读取。')
        return self.result


class OfficeZip:
    def __init__(self, data):
        self.z = zipfile.ZipFile(io.BytesIO(data))
        infos = self.z.infolist()
        if len(infos) > 2000:
            raise MaterialError('压缩包文件数超过 2000 项')
        seen, expanded = set(), 0
        for info in infos:
            name = info.filename
            path = PurePosixPath(name)
            if (not name or len(name) > 240 or '\\' in name or ':' in name or '\x00' in name
                    or path.is_absolute() or '..' in path.parts or name.casefold() in seen
                    or stat.S_ISLNK(info.external_attr >> 16)):
                raise MaterialError('压缩包包含不安全、重复或越界的文件路径')
            seen.add(name.casefold())
            if info.flag_bits & 1:
                raise MaterialError('不支持加密的 Office 文件')
            expanded += info.file_size
            if expanded > MAX_EXPANDED or info.file_size > MAX_XML:
                raise MaterialError('压缩包展开大小超过本地解析上限')
            if info.file_size > 1024 * 1024 and info.file_size > max(1, info.compress_size) * 200:
                raise MaterialError('压缩比例过高，拒绝展开可疑文件')
            if 'vbaproject' in name.casefold() or name.casefold().endswith(('.exe', '.dll', '.vbs')):
                raise MaterialError('含宏或可执行内容的 Office 文件不在本轮解析范围内')
        self.names = set(self.z.namelist())

    def xml(self, name):
        if name not in self.names:
            raise MaterialError(f'缺少必要的文档结构：{name}')
        data = self.z.read(name)
        # UTF-16 is unnecessary for OOXML generated by supported writers; rejecting
        # NUL bytes also prevents entity declarations hidden in another encoding.
        if b'\x00' in data or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
            raise MaterialError('文档 XML 包含不支持的编码或实体声明')
        node = ET.fromstring(data)
        if sum(1 for _ in node.iter()) > 200_000:
            raise MaterialError('文档 XML 节点数超过解析上限')
        return node

    def external_count(self):
        count = 0
        for name in self.names:
            if name.endswith('.rels'):
                count += sum(n.get('TargetMode', '').lower() == 'external' for n in self.xml(name))
        return count


_OLE_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
_OLE_END = 0xFFFFFFFE


def _u16(data, offset):
    return int.from_bytes(data[offset:offset + 2], 'little')


def _u32(data, offset):
    return int.from_bytes(data[offset:offset + 4], 'little')


class OleFile:
    """只读老式 Office / WPS 用的 OLE 复合文档; 不调用系统组件, 不执行宏.

    按 [MS-CFB] 读 FAT / MiniFAT / 目录, 只取需要的流; 扇区链有环、越界、大小离谱都当坏文件.
    """

    def __init__(self, data):
        if len(data) < 512 or data[:8] != _OLE_MAGIC:
            raise MaterialError('不是有效的 OLE 复合文档')
        self.data = data
        self.sector = 1 << _u16(data, 30)
        self.mini = 1 << _u16(data, 32)
        if self.sector not in (512, 4096) or self.mini != 64:
            raise MaterialError('OLE 复合文档的扇区大小不认识')
        if len(data) < self.sector:
            raise MaterialError('OLE 复合文档缺少文件头扇区')
        self.fat = self._read_fat()
        mini = self._read_chain_bytes(_u32(data, 60), _u32(data, 64) * self.sector) \
            if _u32(data, 64) else b''
        self.mini_fat = [_u32(mini, offset) for offset in range(0, len(mini), 4)]
        self.entries = self._read_directory()
        root = self.entries[0] if self.entries and self.entries[0][1] == 5 else next(
            (e for e in self.entries if e[1] == 5), None)
        self.ministream = self._read_chain_bytes(root[2], root[3]) if root and root[3] else b''

    def _sector(self, number):
        start = (number + 1) * self.sector
        if number >= 0xFFFFFFFA or start + self.sector > len(self.data):
            raise MaterialError('OLE 复合文档的扇区越界')
        return self.data[start:start + self.sector]

    def _read_fat(self):
        sectors, seen = [], set()
        for index in range(109):
            number = _u32(self.data, 76 + index * 4)
            if number < 0xFFFFFFFA:
                sectors.append(number)
        next_difat = _u32(self.data, 68)
        while next_difat < 0xFFFFFFFA:
            if next_difat in seen:
                raise MaterialError('OLE 复合文档的 DIFAT 链有环')
            seen.add(next_difat)
            raw = self._sector(next_difat)
            for offset in range(0, len(raw) - 4, 4):
                number = _u32(raw, offset)
                if number < 0xFFFFFFFA:
                    sectors.append(number)
            next_difat = _u32(raw, len(raw) - 4)
            if len(seen) > 10000:
                raise MaterialError('OLE 复合文档的 FAT 扇区太多')
        if not sectors:
            raise MaterialError('OLE 复合文档没有 FAT')
        fat = []
        for number in sectors:
            raw = self._sector(number)
            fat.extend(_u32(raw, offset) for offset in range(0, len(raw), 4))
        return fat

    def _chain(self, start, table, limit):
        chain, seen = [], set()
        while start != _OLE_END:
            if start in seen or start >= len(table) or len(chain) > limit:
                raise MaterialError('OLE 复合文档的扇区链有环或越界')
            seen.add(start)
            chain.append(start)
            start = table[start]
        return chain

    def _read_chain_bytes(self, start, size):
        if size <= 0:
            return b''
        if size > MAX_EXPANDED:
            raise MaterialError('OLE 复合文档里的流超过解析上限')
        if size < _u32(self.data, 56) and hasattr(self, 'mini_fat') and hasattr(self, 'ministream'):
            table, unit, source = self.mini_fat, self.mini, self.ministream
            offset = lambda number: number * unit
        else:
            table, unit, source = self.fat, self.sector, self.data
            offset = lambda number: (number + 1) * unit
        out = bytearray()
        for number in self._chain(start, table, len(self.data) // unit + 1):
            begin = offset(number)
            if begin + unit > len(source):
                raise MaterialError('OLE 复合文档的流越界')
            out.extend(source[begin:begin + unit])
            if len(out) >= size:
                break
        if len(out) < size:
            raise MaterialError('OLE 复合文档的流不完整')
        return bytes(out[:size])

    def _read_directory(self):
        if not self.fat:
            raise MaterialError('OLE 复合文档没有目录')
        raw = b''.join(self._sector(number) for number in self._chain(
            _u32(self.data, 48), self.fat, len(self.data) // self.sector + 1))
        entries = []
        for offset in range(0, len(raw) - 127, 128):
            kind = raw[offset + 66]
            if kind not in (1, 2, 5):
                continue
            name_length = _u16(raw, offset + 64)
            if name_length > 64 or name_length % 2:
                raise MaterialError('OLE 复合文档的目录项名称无效')
            name = raw[offset:offset + name_length].decode('utf-16-le', errors='replace').rstrip('\x00')
            entries.append((name, kind, _u32(raw, offset + 116), _u32(raw, offset + 120)))
        return entries

    def read(self, name):
        entry = next((e for e in self.entries if e[0] == name), None) or next(
            (e for e in self.entries if e[0].casefold() == name.casefold()), None)
        if entry is None or entry[1] != 2:
            raise MaterialError(f'缺少必要的 OLE 流：{name}')
        return self._read_chain_bytes(entry[2], entry[3])

    def largest(self):
        """认不出标准流名时 (有些 WPS 版本自己起名) 取最大的那个文字流兜底."""
        streams = [e for e in self.entries if e[1] == 2 and 0 < e[3] <= MAX_EXPANDED]
        if not streams:
            raise MaterialError('OLE 复合文档里没有可读的流')
        return max(streams, key=lambda e: e[3])


def _zip_kind(data):
    """ZIP 不一定是压缩包: 先看里面是不是 OOXML / ODF / EPUB, 是就按文档读."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            names = {name.casefold(): name for name in package.namelist()}
            if 'word/document.xml' in names:
                return '.docx'
            if 'xl/workbook.xml' in names:
                return '.xlsx'
            if 'ppt/presentation.xml' in names:
                return '.pptx'
            if 'meta-inf/container.xml' in names:
                return '.epub'
            if 'ofd.xml' in names:
                return '.ofd'
            if 'content.xml' in names:
                with package.open(names['content.xml']) as stream:
                    head = stream.read(65536)
                if b'xmap-content' in head:  # 老版 XMind 也用 content.xml, 但不是 ODF
                    return '.xmind'
                if 'mimetype' in names:
                    try:
                        kind = package.read(names['mimetype'])[:100].decode('ascii', errors='ignore').strip()
                    except (OSError, ValueError, zipfile.BadZipFile):
                        kind = ''
                    if kind.endswith('spreadsheet'):
                        return '.ods'
                    if kind.endswith('presentation'):
                        return '.odp'
                return '.odt'
            if 'content.json' in names and ('metadata.json' in names or 'manifest.json' in names):
                return '.xmind'
    except (OSError, ValueError, KeyError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
        if data[:2] == b'PK':
            return '.zip'
        raise MaterialError('压缩包损坏或不是有效的 ZIP') from exc
    return '.zip'


def _ole_kind(data):
    """后缀没写对时, 从 OLE 的流名认老式 Word / Excel / PowerPoint."""
    try:
        names = {entry[0].casefold() for entry in OleFile(data).entries}
    except MaterialError:
        return '.doc'
    if 'worddocument' in names:
        return '.doc'
    if 'workbook' in names or 'book' in names:
        return '.xls'
    if 'powerpoint document' in names or 'pp40' in names:
        return '.ppt'
    return '.doc'


def _entry_suffix(name):
    suffix = PurePosixPath(name).suffix.lower()
    return suffix if re.fullmatch(r'\.[a-z0-9]{1,12}', suffix) else ''


def _archive_listing(out, names):
    if not names:
        return
    shown = names[:200]
    rows = [f'{name} ({size} 字节)' for name, size in shown]
    if len(names) > len(shown):
        rows.append(f'…还有 {len(names) - len(shown)} 项')
    out.add('archive', f'压缩包目录（{len(names)} 个文件）', '\n'.join(rows))


def _archive_entry(out, name, data, declared, budget, depth):
    """压缩包里的一项按它自己的后缀读; 读不了的写明原因, 不拖累别的项."""
    suffix = _entry_suffix(name)
    if not suffix and _plain_text(data):
        suffix = '.txt'
    if not suffix:
        out.warn(f'压缩包里的 {name} 不知道是什么格式，没有读取。')
        return
    if suffix in IMAGE_SUFFIXES:
        out.warn(f'压缩包里的 {name} 是图片，本次只读文字，没有提取图片里的内容。')
        return
    partial = declared > MAX_BYTES and suffix in TEXT_SUFFIXES
    try:
        result = _parse(data, suffix, limit=MAX_STREAM_BYTES, partial=partial,
                        budget=budget, depth=depth + 1)
    except MaterialError as exc:
        out.warn(f'压缩包里的 {name} 没读成：{exc}')
        return
    if result.get('parse_status') not in {'ready', 'partial'}:
        out.warn(f"压缩包里的 {name} 没读成：{result.get('error') or result.get('parse_status')}")
        return
    out.add('archive', f'压缩包内 {name}', result.get('text', ''))
    for warning in result.get('warnings', []):
        out.warn(f'[{name}] {warning}')


def _zip_archive(data, suffix, budget, depth):
    try:
        package = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise MaterialError('压缩包损坏或不是有效的 ZIP；内容尚未读取') from exc
    with package:
        infos = package.infolist()
        if len(infos) > MAX_ARCHIVE_FILES:
            raise MaterialError('压缩包文件数超过 2000 项')
        seen, listing = set(), []
        for info in infos:
            name = info.filename
            if not _safe_member(name):
                raise MaterialError('压缩包包含不安全、重复或越界的文件路径')
            if name.casefold() in seen:
                raise MaterialError('压缩包包含不安全、重复或越界的文件路径')
            seen.add(name.casefold())
            if info.flag_bits & 1:
                raise MaterialError('不支持加密的压缩包')
            if not info.is_dir():
                listing.append((name, info.file_size))
        out = Output('zip')
        _archive_listing(out, listing)
        for info in infos:
            if info.is_dir():
                continue
            name = info.filename
            if stat.S_ISLNK(info.external_attr >> 16):
                budget.take(info.file_size)
                out.warn(f'压缩包里的 {name} 是链接，没有读取。')
                continue
            low = name.casefold()
            if 'vbaproject' in low or low.endswith(('.exe', '.dll', '.msi', '.scr', '.com', '.lnk')):
                budget.take(info.file_size)
                out.warn(f'压缩包里的 {name} 是可执行或宏内容，没有读取。')
                continue
            budget.take(info.file_size)
            if info.file_size > MAX_STREAM_BYTES:
                out.warn(f'压缩包里的 {name} 太大，没有读取。')
                continue
            try:
                with package.open(info) as stream:
                    raw = stream.read(MAX_STREAM_BYTES)
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
                raise MaterialError(f'压缩包里的 {name} 读不出来：{exc}') from exc
            _archive_entry(out, name, raw, info.file_size, budget, depth)
        out.warn('压缩包只读里面的文字和文档；可执行文件、宏、加密项和图片没有执行或提取，路径越界的内容不读取。')
        return out.finish()


def _tar_archive(data, suffix, budget, depth):
    import tarfile

    try:
        package = tarfile.open(fileobj=io.BytesIO(data), mode='r:*')
    except (OSError, ValueError, tarfile.TarError) as exc:
        raise MaterialError('压缩包损坏或不是有效的 TAR；内容尚未读取') from exc
    with package:
        members = package.getmembers()
        if len(members) > MAX_ARCHIVE_FILES:
            raise MaterialError('压缩包文件数超过 2000 项')
        seen, listing = set(), []
        for member in members:
            if not _safe_member(member.name):
                raise MaterialError('压缩包包含不安全、重复或越界的文件路径')
            if member.name.casefold() in seen:
                raise MaterialError('压缩包包含不安全、重复或越界的文件路径')
            seen.add(member.name.casefold())
            if member.isfile():
                listing.append((member.name, member.size))
        out = Output('tar')
        _archive_listing(out, listing)
        for member in members:
            if member.isdir():
                continue
            if not member.isfile():
                out.warn(f'压缩包里的 {member.name} 是链接或设备，没有读取。')
                continue
            name = member.name
            low = name.casefold()
            if 'vbaproject' in low or low.endswith(('.exe', '.dll', '.msi', '.scr', '.com', '.lnk')):
                budget.take(member.size)
                out.warn(f'压缩包里的 {name} 是可执行或宏内容，没有读取。')
                continue
            budget.take(member.size)
            if member.size > MAX_STREAM_BYTES:
                out.warn(f'压缩包里的 {name} 太大，没有读取。')
                continue
            stream = package.extractfile(member)
            if stream is None:
                out.warn(f'压缩包里的 {name} 没有内容，没有读取。')
                continue
            with stream:
                raw = stream.read(MAX_STREAM_BYTES)
            _archive_entry(out, name, raw, member.size, budget, depth)
        out.warn('压缩包只读里面的文字和文档；可执行文件、宏、加密项和图片没有执行或提取，路径越界的内容不读取。')
        return out.finish()


def _decompress(data, suffix):
    import bz2
    import gzip
    import lzma

    try:
        source = io.BytesIO(data)
        if suffix == '.gz':
            with gzip.GzipFile(fileobj=source) as stream:
                raw = stream.read(MAX_EXPANDED + 1)
        elif suffix == '.bz2':
            with bz2.BZ2File(source) as stream:
                raw = stream.read(MAX_EXPANDED + 1)
        elif suffix == '.xz':
            with lzma.LZMAFile(source) as stream:
                raw = stream.read(MAX_EXPANDED + 1)
        else:
            raise MaterialError('不认识的压缩格式')
    except (OSError, EOFError, ValueError, lzma.LZMAError) as exc:
        raise MaterialError('压缩文件损坏或不是支持的压缩格式；内容尚未读取') from exc
    if len(raw) > MAX_EXPANDED:
        raise MaterialError('压缩包展开大小超过本地解析上限')
    return raw


def _compressed(data, suffix, budget, depth):
    raw = _decompress(data, suffix)
    budget.take(len(raw))
    if len(raw) >= 262 and raw[257:262] == b'ustar':
        return _tar_archive(raw, '.tar', budget, depth + 1)
    kind = sniff(raw, '')
    if not kind:
        if not _plain_text(raw):
            raise MaterialError('压缩后的内容不是可读的文字或文档')
        kind = '.txt'
    return _parse(raw, kind, limit=MAX_STREAM_BYTES, budget=budget, depth=depth + 1)


def _archive(data, suffix, budget=None, depth=0):
    if depth > MAX_ARCHIVE_DEPTH:
        raise MaterialError('压缩包嵌套层数超过本地解析上限')
    budget = budget or ArchiveBudget()
    if suffix == '.zip':
        kind = _zip_kind(data)
        if kind != '.zip':
            return _parse(data, kind, budget=budget, depth=depth)
        return _zip_archive(data, suffix, budget, depth)
    if suffix in {'.tar', '.tgz', '.tbz', '.tbz2', '.txz', '.tar.gz', '.tar.bz2', '.tar.xz'}:
        return _tar_archive(data, suffix, budget, depth)
    return _compressed(data, suffix, budget, depth)


def _clean_binary_text(text):
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\x0b', '\n').replace('\x0c', '\n')
    text = text.replace('\x07', '\t')
    return ''.join(c for c in text if c in '\n\t' or ord(c) >= 32)


def _looks_text(text):
    text = _clean_binary_text(text).strip()
    if len(text) < 2:
        return ''
    good = sum(c.isprintable() or c in '\n\t' for c in text)
    return text if good >= len(text) * 0.8 else ''


def _binary_text(data):
    """最后一道兜底: 从二进制里捞 UTF-16LE 和 ASCII 的文字段 (老式 WPS 的排版各有各的写法)."""
    found = []
    for start in (0, 1):
        buf = []
        for offset in range(start, len(data) - 1, 2):
            code = data[offset] | (data[offset + 1] << 8)
            if code in (9, 10, 13) or 0x20 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD:
                buf.append(chr(code))
            else:
                if len(buf) >= 4:
                    found.append(''.join(buf))
                buf = []
        if len(buf) >= 4:
            found.append(''.join(buf))
    for match in re.finditer(rb'[\x20-\x7e\r\n\t]{4,}', data):
        found.append(match.group().decode('ascii', errors='replace'))
    out = []
    for text in found:
        text = _looks_text(text)
        if text and (not out or text not in out[-1]):
            out.append(text)
    return '\n'.join(out)


def _doc_pieces(word, table, flags):
    """Word 97-2003 的 piece table: 文字可能在 WordDocument 流里, 也可能在 0Table / 1Table 里分片."""
    if len(word) < 0x01AA:
        return ''
    fc_clx, lcb_clx = _u32(word, 0x01A2), _u32(word, 0x01A6)
    if not table or not lcb_clx or fc_clx + lcb_clx > len(table):
        return ''
    clx, pos = table[fc_clx:fc_clx + lcb_clx], 0
    while pos < len(clx) and clx[pos] == 1:  # Prc: 跳过格式信息
        if pos + 3 > len(clx):
            return ''
        pos += 3 + _u16(clx, pos + 1)
    if pos + 5 > len(clx) or clx[pos] != 2:  # Pcdt
        return ''
    length = _u32(clx, pos + 1)
    plc = clx[pos + 5:pos + 5 + length]
    if len(plc) < 16 or (len(plc) - 4) % 12:
        return ''
    count = (len(plc) - 4) // 12
    if count > 100_000:
        return ''
    cps = [_u32(plc, index * 4) for index in range(count + 1)]
    base = (count + 1) * 4
    pieces = []
    for index in range(count):
        chars = cps[index + 1] - cps[index]
        if chars <= 0:
            continue
        pcd = plc[base + index * 8:base + index * 8 + 8]
        fc = _u32(pcd, 2)
        compressed = bool(fc & 0x40000000)
        offset = (fc & 0x3FFFFFFF) // 2 if compressed else fc & 0x3FFFFFFF
        size = chars if compressed else chars * 2
        raw = word[offset:offset + size]
        if len(raw) < size:
            continue
        if compressed:
            try:
                text = raw.decode('gb18030')
            except UnicodeDecodeError:
                text = raw.decode('cp1252', errors='replace')
        else:
            text = raw.decode('utf-16-le', errors='replace')
        pieces.append(_clean_binary_text(text))
    return ''.join(pieces)


def _doc(data, suffix):
    """老式 Word / WPS 文字 (.doc / .wps): 先读 piece table, 认不出时从流里捞文字."""
    ole = OleFile(data)
    has_word = any(e[0].casefold() == 'worddocument' for e in ole.entries)
    word = ole.read('WordDocument') if has_word else b''
    flags = _u16(word, 10) if word else 0
    table_name = '1Table' if flags & 0x0200 else '0Table'
    table = b''
    try:
        table = ole.read(table_name)
    except MaterialError:
        pass
    text = _doc_pieces(word, table, flags) if word else ''
    if not text.strip():
        candidates = [word] if word else []
        extra_name = ole.largest()[0]
        if extra_name.casefold() != 'worddocument':
            try:
                candidates.append(ole.read(extra_name))
            except MaterialError:
                pass
        text = '\n'.join(_binary_text(raw) for raw in candidates)
    out = Output(suffix.lstrip('.') or 'doc')
    for number, line in enumerate(_clean_binary_text(text).splitlines(), 1):
        if line.strip() and not out.add('paragraph', f'段落 {number}', line.strip()):
            break
    out.warn('老式 Word / WPS 只提取文字；图片、批注、页眉页脚、表格排版和嵌入对象未提取。')
    return out.finish()


def _xl_string(data, pos):
    """BIFF8 的 XLUnicodeString; 读不全返回 (None, pos)."""
    if pos + 3 > len(data):
        return None, pos
    chars, flags = _u16(data, pos), data[pos + 2]
    pos += 3
    rich = _u16(data, pos) if flags & 0x08 else 0
    pos += 2 if flags & 0x08 else 0
    extra = _u32(data, pos) if flags & 0x04 else 0
    pos += 4 if flags & 0x04 else 0
    size = chars * 2 if flags & 0x01 else chars
    if chars > 1_000_000 or pos + size > len(data):
        return None, pos
    raw = data[pos:pos + size]
    if flags & 0x01:
        text = raw.decode('utf-16-le', errors='replace')
    else:
        try:
            text = raw.decode('gb18030')
        except UnicodeDecodeError:
            text = raw.decode('cp1252', errors='replace')
    pos += size + rich * 4 + extra
    return _clean_binary_text(text), pos


def _xls_strings(data):
    """BIFF8 的 SST / LABEL / RSTRING / STRING 记录; 老式 WPS 表格也用同一套记录."""
    strings, pos = [], 0
    while pos + 4 <= len(data):
        kind, length = _u16(data, pos), _u16(data, pos + 2)
        end = pos + 4 + length
        if end > len(data):
            break
        payload = data[pos + 4:end]
        if kind == 0x00FC and len(payload) >= 8:  # SST; 长表会被拆到后面的 CONTINUE 里
            parts, cursor = [payload], end
            while cursor + 4 <= len(data) and _u16(data, cursor) == 0x003C:
                more = _u16(data, cursor + 2)
                if cursor + 4 + more > len(data):
                    break
                parts.append(data[cursor + 4:cursor + 4 + more])
                cursor += 4 + more
            sst = b''.join(parts)
            count, offset = _u32(sst, 4), 8
            for _ in range(min(count, 100_000)):
                text, offset = _xl_string(sst, offset)
                if text is None:
                    break
                if text.strip():
                    strings.append(text)
            end = cursor
        elif kind in (0x0204, 0x00D6) and len(payload) > 6:  # LABEL / RSTRING
            text, _ = _xl_string(payload, 6)
            if text and text.strip():
                strings.append(text)
        elif kind == 0x0207:  # STRING (公式结果)
            text, _ = _xl_string(payload, 0)
            if text and text.strip():
                strings.append(text)
        pos = end
    return strings


def _xls(data, suffix):
    """老式 Excel / WPS 表格 (.xls / .et): 读 Workbook / Book 流里的单元格文字."""
    ole = OleFile(data)
    stream = b''
    for name in ('Workbook', 'Book'):
        try:
            stream = ole.read(name)
            break
        except MaterialError:
            continue
    if not stream:
        stream = ole.read(ole.largest()[0])
    strings = _xls_strings(stream)
    if not strings:
        strings = [line for line in _binary_text(stream).splitlines() if line.strip()]
    out = Output(suffix.lstrip('.') or 'xls')
    for number, text in enumerate(strings, 1):
        if not out.add('cell', f'单元格 {number}', text):
            break
    out.warn('老式 Excel / WPS 表格只提取单元格文字；公式、样式、图表和嵌入对象未提取，也不重新计算。')
    return out.finish()


def _ppt_strings(data):
    """PowerPoint 97-2003 的记录树: 找 TextCharsAtom / TextBytesAtom; 记录不是严格的也按 8 字节头试着走."""
    strings, seen = [], set()

    def walk(start, end, depth):
        pos, records = start, 0
        while pos + 8 <= end and records < 100_000:
            kind, length = _u16(data, pos + 2), _u32(data, pos + 4)
            payload, nxt = pos + 8, pos + 8 + length
            if kind == 0 or nxt > end:
                return
            if kind == 0x0FA0:  # TextCharsAtom
                if length % 2 == 0:
                    text = _looks_text(data[payload:nxt].decode('utf-16-le', errors='replace'))
                    if text and text not in seen:
                        seen.add(text)
                        strings.append(text)
            elif kind == 0x0FA8:  # TextBytesAtom
                raw = data[payload:nxt]
                try:
                    decoded = raw.decode('gb18030') if any(byte >= 128 for byte in raw) else raw.decode('cp1252')
                except UnicodeDecodeError:
                    decoded = raw.decode('cp1252', errors='replace')
                text = _looks_text(decoded)
                if text and text not in seen:
                    seen.add(text)
                    strings.append(text)
            elif depth < 8 and length >= 8 and payload + 8 <= nxt:  # 容器: 试着读里面的子记录
                child_kind, child_length = _u16(data, payload + 2), _u32(data, payload + 4)
                if 0 < child_kind < 0x2000 and child_length <= nxt - payload - 8:
                    walk(payload, nxt, depth + 1)
            pos, records = nxt, records + 1

    walk(0, len(data), 0)
    if not strings:  # 记录头对不上时, 只按 atom 头扫一遍
        for marker, wide in ((b'\xa0\x0f', True), (b'\xa8\x0f', False)):
            for match in re.finditer(re.escape(marker), data):
                start = match.start()
                if start + 8 > len(data):
                    continue
                length = _u32(data, start + 4)
                if length <= 0 or start + 8 + length > len(data):
                    continue
                raw = data[start + 8:start + 8 + length]
                if wide:
                    decoded = raw.decode('utf-16-le', errors='replace')
                else:
                    try:
                        decoded = raw.decode('gb18030') if any(byte >= 128 for byte in raw) else raw.decode('cp1252')
                    except UnicodeDecodeError:
                        decoded = raw.decode('cp1252', errors='replace')
                text = _looks_text(decoded)
                if text and text not in seen:
                    seen.add(text)
                    strings.append(text)
    return strings


def _ppt(data, suffix):
    """老式 PowerPoint / WPS 演示 (.ppt / .dps): 读 PowerPoint Document 流里的文字."""
    ole = OleFile(data)
    stream = b''
    for name in ('PowerPoint Document', 'PP40'):
        try:
            stream = ole.read(name)
            break
        except MaterialError:
            continue
    if not stream:
        stream = ole.read(ole.largest()[0])
    strings = _ppt_strings(stream)
    if not strings:
        strings = [line for line in _binary_text(stream).splitlines() if line.strip()]
    out = Output(suffix.lstrip('.') or 'ppt')
    for number, text in enumerate(strings, 1):
        if not out.add('slide', f'幻灯片 {number}', text):
            break
    out.warn('老式 PowerPoint / WPS 演示只提取文字；图片、图表、备注、动画和嵌入对象未提取。')
    return out.finish()


def _word_text(node):
    parts = []
    for child in node.iter():
        tag = _tag(child)
        if tag == 't':
            parts.append(child.text or '')
        elif tag == 'tab':
            parts.append('\t')
        elif tag in {'br', 'cr'}:
            parts.append('\n')
    return ''.join(parts)


def _docx(data):
    package, out = OfficeZip(data), Output('docx')
    if package.external_count():
        out.warn('文档含外部链接或关联资源；仅提取文件内已有文字，未访问链接。')
    doc = package.xml('word/document.xml')
    body = next((n for n in doc if _tag(n) == 'body'), None)
    if body is None:
        raise MaterialError('DOCX 缺少正文')
    paragraph, table = 0, 0
    for block in body:
        tag = _tag(block)
        if tag == 'p':
            paragraph += 1
            if not out.add('paragraph', f'段落 {paragraph}', _word_text(block)):
                break
        elif tag == 'tbl':
            table += 1
            rows, row_chars = [], 0
            for row_number, row in enumerate((n for n in block if _tag(n) == 'tr'), 1):
                cells = [' / '.join(_word_text(p) for p in cell if _tag(p) == 'p')
                         for cell in row if _tag(cell) == 'tc']
                rows.append(f'第 {row_number} 行 | ' + ' | '.join(cells))
                row_chars += len(rows[-1])
                if len(rows) >= MAX_ROWS or row_chars >= MAX_TEXT:
                    out.result['truncated'] = True
                    break
            if not out.add('table', f'表格 {table}', '\n'.join(rows)):
                break
    out.warn('提取正文段落和表格；不推断 Word 排版页码，图片、批注、页眉页脚和嵌入对象未提取。')
    return out.finish()


def _xlsx(data):
    package, out = OfficeZip(data), Output('xlsx')
    if package.external_count():
        out.warn('工作簿含外部链接；未访问外部数据或更新公式。')
    strings = []
    if 'xl/sharedStrings.xml' in package.names:
        root = package.xml('xl/sharedStrings.xml')
        strings = [''.join(n.text or '' for n in si.iter() if _tag(n) == 't') for si in root]
    workbook = package.xml('xl/workbook.xml')
    rels = {r.get('Id'): r for r in package.xml('xl/_rels/workbook.xml.rels')}
    sheets = [n for n in workbook.iter() if _tag(n) == 'sheet']
    out.result['sheets'] = []
    out.result['sheet_count'] = len(sheets)
    cells_used = 0
    for index, sheet in enumerate(sheets[:MAX_SHEETS], 1):
        name = sheet.get('name', f'Sheet{index}')[:200]
        relation_id = next((v for k, v in sheet.attrib.items() if k.endswith('}id')), '')
        relation = rels.get(relation_id)
        if relation is None or relation.get('TargetMode', '').lower() == 'external':
            raise MaterialError('工作表关联无效或指向外部资源')
        target = relation.get('Target', '')
        path = PurePosixPath(target.lstrip('/')) if target.startswith('/') else PurePosixPath('xl') / target
        if '..' in path.parts or '\\' in target or ':' in target:
            raise MaterialError('工作表关联路径越界')
        root = package.xml(str(path))
        lines, row_count, cell_count, line_chars = [], 0, 0, 0
        for row in (n for n in root.iter() if _tag(n) == 'row'):
            row_count += 1
            if row_count > MAX_ROWS:
                out.result['truncated'] = True
                break
            for cell in (n for n in row if _tag(n) == 'c'):
                if cells_used >= MAX_CELLS:
                    out.result['truncated'] = True
                    break
                cells_used += 1
                cell_count += 1
                location = cell.get('r', f'row{row_count}:cell{cell_count}')[:40]
                value = next((n.text or '' for n in cell if _tag(n) == 'v'), '')
                formula = next((n.text or '' for n in cell if _tag(n) == 'f'), None)
                kind = cell.get('t', '')
                if kind == 's':
                    try:
                        pos = int(value)
                        if pos < 0:
                            raise IndexError()
                        value = strings[pos]
                    except (ValueError, IndexError) as exc:
                        raise MaterialError('工作表共享文本索引无效') from exc
                elif kind == 'inlineStr':
                    value = ''.join(n.text or '' for n in cell.iter() if _tag(n) == 't')
                elif kind == 'b':
                    value = 'TRUE' if value == '1' else 'FALSE'
                if formula is not None:
                    value = f'={formula} [未计算公式；缓存值：{value or "无"}]'
                lines.append(f'{location}: {value}')
                line_chars += len(lines[-1])
                if line_chars >= MAX_TEXT:
                    out.result['truncated'] = True
                    break
            if out.result['truncated']:
                break
        out.result['sheets'].append({'index': index, 'name': name, 'rows': min(row_count, MAX_ROWS),
                                    'cells': cell_count, 'state': sheet.get('state', 'visible')})
        if not out.add('sheet', f'工作表 {name}', '\n'.join(lines), sheet=name, sheet_index=index):
            break
    if len(sheets) > MAX_SHEETS:
        out.result['truncated'] = True
    out.warn('单元格按地址列出；公式只显示原文和已有缓存值，不重新计算；日期序列和样式不渲染。')
    return out.finish()


def _pptx_slides(package):
    """Slides in presentation order; fall back to slideN.xml when the order is unreadable."""
    order = []
    if 'ppt/presentation.xml' in package.names and 'ppt/_rels/presentation.xml.rels' in package.names:
        rels = {r.get('Id'): r for r in package.xml('ppt/_rels/presentation.xml.rels')}
        for node in package.xml('ppt/presentation.xml').iter():
            if _tag(node) != 'sldId':
                continue
            relation_id = next((v for k, v in node.attrib.items() if k.endswith('}id')), '')
            relation = rels.get(relation_id)
            if relation is None or relation.get('TargetMode', '').lower() == 'external':
                continue
            target = relation.get('Target', '')
            path = PurePosixPath(target.lstrip('/')) if target.startswith('/') else PurePosixPath('ppt') / target
            if '..' in path.parts or '\\' in target or ':' in target:
                continue
            if str(path) in package.names:
                order.append(str(path))
    if not order:
        def key(name):
            number = re.search(r'(\d+)\.xml$', name)
            return (int(number.group(1)) if number else 0, name)
        order = sorted((n for n in package.names if re.fullmatch(r'ppt/slides/slide\d+\.xml', n)), key=key)
    return order


def _pptx(data):
    package, out = OfficeZip(data), Output('pptx')
    if package.external_count():
        out.warn('演示文稿含外部链接或关联资源；仅提取文件内已有文字，未访问链接。')
    slides = _pptx_slides(package)
    if not slides:
        raise MaterialError('PPTX 缺少幻灯片')
    for index, name in enumerate(slides, 1):
        root = package.xml(name)
        lines = []
        for paragraph in (n for n in root.iter() if _tag(n) == 'p'):
            line = ''.join(n.text or '' for n in paragraph.iter() if _tag(n) == 't').strip()
            if line:
                lines.append(line)
        if not out.add('slide', f'幻灯片 {index}', '\n'.join(lines)):
            break
    out.warn('提取幻灯片里的文字和表格文字；图片、图表、备注和动画未提取。')
    return out.finish()


def _odf_text(node):
    pieces = [node.text or '']
    for child in node:
        tag = _tag(child)
        if tag == 's':
            pieces.append(' ')
        elif tag == 'tab':
            pieces.append('\t')
        elif tag in {'line-break', 'br'}:
            pieces.append('\n')
        else:
            pieces.append(_odf_text(child))
        pieces.append(child.tail or '')
    return ''.join(pieces)


def _odf_rows(table):
    rows = []
    for row in (n for n in table.iter() if _tag(n) == 'table-row'):
        cells = []
        for cell in row:
            if _tag(cell) not in {'table-cell', 'covered-table-cell'}:
                continue
            parts = [_odf_text(n).strip() for n in cell.iter() if _tag(n) in {'p', 'h'}]
            cells.append(' / '.join(x for x in parts if x) or _odf_text(cell).strip())
        rows.append(' | '.join(cells))
    return rows


def _odf(data, suffix):
    """ODT / ODS / ODP: read content.xml; no styles, macros or embedded objects."""
    package, out = OfficeZip(data), Output(suffix.lstrip('.'))
    root = package.xml('content.xml')
    body = next((n for n in root.iter() if _tag(n) == 'body'), None)
    if body is None:
        raise MaterialError('ODF 缺少正文')
    if suffix == '.ods':
        tables = [n for n in body.iter() if _tag(n) == 'table']
        if not tables:
            raise MaterialError('ODS 缺少工作表')
        for index, table in enumerate(tables, 1):
            name = next((v for k, v in table.attrib.items() if k.endswith('}name')), '') or f'表{index}'
            if not out.add('sheet', f'工作表 {name}', '\n'.join(_odf_rows(table)), sheet=name, sheet_index=index):
                break
        out.warn('单元格按行列出；公式只显示原文，不重新计算；图片、样式和嵌入对象未提取。')
        return out.finish()
    if suffix == '.odp':
        pages = [n for n in body.iter() if _tag(n) == 'page'] or [body]
        for index, page in enumerate(pages, 1):
            lines = [_odf_text(n).strip() for n in page.iter() if _tag(n) in {'p', 'h'}]
            if not out.add('slide', f'幻灯片 {index}', '\n'.join(x for x in lines if x)):
                break
        out.warn('提取幻灯片里的文字；图片、图表、备注和动画未提取。')
        return out.finish()
    paragraph, table = 0, 0

    def walk(node):
        nonlocal paragraph, table
        tag = _tag(node)
        if tag in {'p', 'h'}:
            paragraph += 1
            text = _odf_text(node).strip()
            return out.add('paragraph', f'段落 {paragraph}', text) if text else True
        if tag == 'table':
            table += 1
            return out.add('table', f'表格 {table}', '\n'.join(_odf_rows(node)))
        for child in node:
            if not walk(child):
                return False
        return True

    walk(body)
    out.warn('提取正文段落和表格文字；图片、批注、页眉页脚和嵌入对象未提取。')
    return out.finish()


_RTF_SKIP = frozenset({'fonttbl', 'colortbl', 'stylesheet', 'info', 'pict', 'object', 'header', 'headerl', 'headerr',
                       'headerf', 'footer', 'footerl', 'footerr', 'footerf', 'listtable', 'listoverridetable',
                       'rsidtbl', 'generator', 'xmlnstbl', 'themedata', 'colorschememapping', 'latentstyles',
                       'datastore'})


def _rtf(data):
    """RTF text only: skip font/color/picture groups, decode \\u and \\'hh characters."""
    raw = data.decode('latin-1')
    pieces, stack, skip, index = [], [False], False, 0
    while index < len(raw):
        char = raw[index]
        if char == '{':
            stack.append(skip)
            index += 1
        elif char == '}':
            if len(stack) > 1:
                skip = stack.pop()
            index += 1
        elif char == '\\':
            index += 1
            if index >= len(raw):
                break
            code = raw[index]
            if code in '\\{}':
                if not skip:
                    pieces.append(code)
                index += 1
            elif code == "'":
                value = raw[index + 1:index + 3]
                try:
                    byte = bytes([int(value, 16)])
                except ValueError:
                    index += 1
                    continue
                if not skip:
                    pieces.append(byte.decode('cp1252', errors='replace'))
                index += 3
            elif code == '*':
                skip = True
                index += 1
            elif code.isalpha():
                end = index
                while end < len(raw) and raw[end].isalpha():
                    end += 1
                word = raw[index:end]
                stop = end
                if stop < len(raw) and raw[stop] == '-':
                    stop += 1
                while stop < len(raw) and raw[stop].isdigit():
                    stop += 1
                parameter = raw[end:stop]
                if stop < len(raw) and raw[stop] == ' ':
                    stop += 1
                index = stop
                if skip:
                    continue
                if word == 'u' and parameter:
                    number = int(parameter)
                    pieces.append(chr(number if number >= 0 else number + 65536))
                    if index < len(raw) and raw[index] not in '\\{}':
                        index += 1  # RTF puts one fallback character after \uN
                elif word in {'par', 'line', 'sect', 'page', 'row'}:
                    pieces.append('\n')
                elif word == 'tab':
                    pieces.append('\t')
                elif word in _RTF_SKIP:
                    skip = True
            elif code == '~':
                if not skip:
                    pieces.append('\xa0')
                index += 1
            else:
                index += 1
        else:
            if not skip and char not in '\r\n':
                pieces.append(char)
            index += 1
    text = re.sub(r'\n{3,}', '\n\n', ''.join(pieces)).strip()
    out = Output('rtf')
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip() and not out.add('paragraph', f'段落 {number}', line.strip()):
            break
    out.warn('RTF 只提取文字；图片、表格排版和嵌入对象未提取。')
    return out.finish()


class _HTMLText(HTMLParser):
    """Text from one XHTML chapter; skip script/style and insert breaks for blocks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skipping = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'head'}:
            self.skipping += 1
        elif tag in {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'head'}:
            self.skipping = max(0, self.skipping - 1)
        elif tag in {'p', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def _epub_documents(package):
    documents, package_path = [], ''
    if 'META-INF/container.xml' in package.names:
        container = package.xml('META-INF/container.xml')
        package_path = next((n.get('full-path', '') for n in container.iter() if _tag(n) == 'rootfile'), '')
    if package_path and package_path in package.names:
        root = package.xml(package_path)
        manifest = {n.get('id'): n.get('href', '') for n in root.iter() if _tag(n) == 'item'}
        base = PurePosixPath(package_path).parent
        for item in (n for n in root.iter() if _tag(n) == 'itemref'):
            href = manifest.get(item.get('idref'), '').split('#', 1)[0].split('?', 1)[0]
            if not href:
                continue
            path = PurePosixPath(href.lstrip('/')) if href.startswith('/') else base / href
            if '..' not in path.parts and '\\' not in href and ':' not in href and str(path) in package.names:
                documents.append(str(path))
    if not documents:
        documents = sorted(n for n in package.names
                           if n.lower().endswith(('.xhtml', '.html', '.htm')) and not n.startswith('META-INF/'))
    return documents


def _epub(data):
    package, out = OfficeZip(data), Output('epub')
    documents = _epub_documents(package)
    if not documents:
        raise MaterialError('EPUB 里没有可读的章节')
    for index, name in enumerate(documents, 1):
        raw = package.z.read(name)
        try:
            page = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            page = raw.decode('utf-8', errors='replace')
        parser = _HTMLText()
        parser.feed(page)
        text = re.sub(r'\n{3,}', '\n\n', ''.join(parser.parts)).strip()
        if not out.add('chapter', f'第 {index} 节', text):
            break
    out.warn('EPUB 只提取各章正文文字；脚本、样式、图片和封面未提取。')
    return out.finish()


def _ofd_page_key(name):
    return tuple(int(x) for x in re.findall(r'\d+', name))


def _ofd(data):
    """OFD (国内电子发票 / 公文常用): 从 ZIP 里按 Document.xml 的页序读 TextCode 文字, 不验证签章."""
    package, out = OfficeZip(data), Output('ofd')
    if 'OFD.xml' not in package.names:
        raise MaterialError('OFD 缺少 OFD.xml')
    root = package.xml('OFD.xml')
    doc_roots = [str(n.text).strip() for n in root.iter()
                 if _tag(n) == 'DocRoot' and str(n.text or '').strip()]
    if not doc_roots:
        doc_roots = sorted((n for n in package.names if re.fullmatch(r'Doc_\d+/Document\.xml', n, re.IGNORECASE)),
                           key=_ofd_page_key)
    pages = []
    for doc_root in doc_roots[:20]:
        path = PurePosixPath(doc_root)
        if (path.is_absolute() or '..' in path.parts or '\\' in doc_root or ':' in doc_root
                or doc_root not in package.names):
            continue
        document = package.xml(doc_root)
        base = path.parent
        for page in (n for n in document.iter() if _tag(n) == 'Page'):
            location = str(page.get('BaseLoc') or '').strip()
            if not location:
                continue
            target = PurePosixPath(location.lstrip('/')) if location.startswith('/') else base / location
            name = str(target)
            if ('..' not in target.parts and '\\' not in location and ':' not in location
                    and name in package.names):
                pages.append(name)
    if not pages:
        pages = sorted((n for n in package.names
                        if re.fullmatch(r'Doc_\d+/Pages/Page_\d+/Content\.xml', n, re.IGNORECASE)), key=_ofd_page_key)
    if not pages:
        raise MaterialError('OFD 里没有找到页面')
    found = 0
    for index, name in enumerate(pages, 1):
        page = package.xml(name)
        lines = []
        for obj in (n for n in page.iter() if _tag(n) == 'TextObject'):
            text = ''.join(n.text or '' for n in obj.iter() if _tag(n) == 'TextCode')
            if text.strip():
                lines.append(text.strip())
        if not lines:
            lines = [str(n.text).strip() for n in page.iter()
                     if _tag(n) == 'TextCode' and str(n.text or '').strip()]
        found += len(lines)
        if not out.add('page', f'第 {index} 页', '\n'.join(lines)):
            break
    out.warn('OFD 只提取文字层；图片、印章、签章和版式未提取，也没有验证签章。')
    result = out.finish()
    if not found:
        result.update(parse_status='needs_ocr', text='', chars=0, sections=[],
                      error='OFD 页面里没有可提取的文字，可能是扫描件或只含图片；尚未运行 OCR。')
    return result


def _eml_body_text(part):
    raw = part.get_payload(decode=True)
    if raw is None:
        value = part.get_payload()
        return value if isinstance(value, str) else ''
    charset = part.get_content_charset() or 'utf-8'
    for encoding in (charset, 'utf-8', 'gb18030'):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode('utf-8', errors='replace')


def _eml(data, suffix='.eml'):
    """邮件 / 网页邮件: 只读邮件头和正文文字; 附件、图片、外链都不打开, HTML 脚本不执行."""
    from email import policy
    from email.parser import BytesParser

    try:
        message = BytesParser(policy=policy.default).parsebytes(data)
    except Exception as exc:  # 邮件结构损坏: 交给上层说清楚, 不假装读过
        raise MaterialError('邮件文件损坏或结构无法解析') from exc
    out = Output(suffix.lstrip('.') or 'eml')
    header = []
    for label, key in (('发件人', 'From'), ('收件人', 'To'), ('抄送', 'Cc'), ('主题', 'Subject'), ('日期', 'Date')):
        value = message.get(key)
        if value:
            header.append(f'{label}: {" ".join(str(value).split())[:500]}')
    if header:
        out.add('header', '邮件头', '\n'.join(header))
    plain, html, attachments = [], [], []

    def walk(part, depth=0):
        if depth > 20:
            out.warn('邮件嵌套太深，后面的部分没有读取。')
            return
        if part.is_multipart():
            for child in part.iter_parts():
                walk(child, depth + 1)
            return
        filename = part.get_filename()
        if filename:
            payload = part.get_payload(decode=True) or b''
            name = ''.join(c for c in str(filename) if ord(c) >= 32)[:200]
            attachments.append((name, len(payload)))
            return
        kind = part.get_content_type()
        if kind == 'text/plain':
            text = _eml_body_text(part).strip()
            if text:
                plain.append(text)
        elif kind == 'text/html':
            parser = _HTMLText()
            parser.feed(_eml_body_text(part))
            text = re.sub(r'\n{3,}', '\n\n', ''.join(parser.parts)).strip()
            if text:
                html.append(text)

    walk(message)
    for index, text in enumerate(plain or html, 1):
        if not out.add('body', f'正文 {index}', text):
            break
    if not plain and not html:
        out.warn('邮件里没有可读的正文文字。')
    if attachments:
        shown = '、'.join(f'{name} ({size} 字节)' for name, size in attachments[:50])
        out.warn(f'邮件附件只列名字和大小，没有打开：{shown}')
    out.warn('邮件只读正文文字；附件、图片、远程图片和外链未打开，HTML 里的脚本没有执行。')
    result = out.finish()
    if not plain and not html:
        result.update(parse_status='unsupported', error='邮件里没有可读的正文文字；附件没有打开。')
    return result


def _fb2(data):
    """FB2 电子书是 XML: 只取书名、作者和正文段落, 不读图片和样式."""
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise MaterialError('FB2 含不支持的实体声明')
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise MaterialError('FB2 不是有效的 XML') from exc
    out = Output('fb2')
    description = next((n for n in root.iter() if _tag(n) == 'description'), None)
    if description is not None:
        title = next((str(n.text).strip() for n in description.iter()
                      if _tag(n) == 'book-title' and str(n.text or '').strip()), '')
        authors = []
        for author in (n for n in description.iter() if _tag(n) == 'author'):
            name = ' '.join(str(n.text).strip() for n in author
                            if _tag(n) in {'first-name', 'middle-name', 'last-name'} and str(n.text or '').strip())
            if name:
                authors.append(name)
        header = '\n'.join(x for x in (f'书名: {title}' if title else '',
                                      f'作者: {"、".join(authors)}' if authors else '') if x)
        if header:
            out.add('header', '书籍信息', header)
    body = next((n for n in root if _tag(n) == 'body'), None)
    if body is None:
        raise MaterialError('FB2 缺少正文')
    paragraph = 0

    def walk(node):
        nonlocal paragraph
        tag = _tag(node)
        if tag in {'binary', 'stylesheet', 'script'}:
            return True
        if tag in {'p', 'v', 'subtitle', 'text-author', 'empty-line'}:
            text = ''.join(node.itertext()).strip()
            if text:
                paragraph += 1
                return out.add('paragraph', f'段落 {paragraph}', text)
            return True
        for child in node:
            if not walk(child):
                return False
        return True

    walk(body)
    out.warn('FB2 只提取正文文字；图片、脚注排版、样式和嵌入内容未提取。')
    result = out.finish()
    if not result['text'].strip():
        result.update(parse_status='unsupported', error='FB2 里没有可读的正文文字。')
    return result


def _xmind(data):
    """XMIND 思维导图是 ZIP: 新版读 content.json 的主题和备注, 老版读 content.xml 的 title."""
    package, out = OfficeZip(data), Output('xmind')
    lines = []
    if 'content.json' in package.names:
        raw = package.z.read('content.json')
        if len(raw) > MAX_XML:
            raise MaterialError('XMIND 的 content.json 超过本地解析上限')
        try:
            root = json.loads(raw.decode('utf-8-sig'))
        except (UnicodeDecodeError, ValueError) as exc:
            raise MaterialError('XMIND 的 content.json 读不出来') from exc
        seen = 0

        def walk(node, depth=0):
            nonlocal seen
            if depth > 50 or seen >= 5000:
                return
            if isinstance(node, dict):
                seen += 1
                title = node.get('title')
                if isinstance(title, str) and title.strip():
                    lines.append(title.strip())
                notes = node.get('notes')
                if isinstance(notes, dict):
                    plain = notes.get('plain')
                    if isinstance(plain, dict) and isinstance(plain.get('content'), str):
                        if plain['content'].strip():
                            lines.append(plain['content'].strip())
                children = node.get('children')
                if isinstance(children, dict):
                    for child in children.values():
                        walk(child, depth + 1)
                elif children is not None:
                    walk(children, depth + 1)
                if 'topic' in node:
                    walk(node['topic'], depth + 1)
            elif isinstance(node, list):
                for child in node:
                    walk(child, depth + 1)

        walk(root)
    if not lines and 'content.xml' in package.names:
        root = package.xml('content.xml')
        for node in root.iter():
            if _tag(node) == 'title' and str(node.text or '').strip():
                lines.append(str(node.text).strip())
                if len(lines) >= 5000:
                    break
    if not lines:
        raise MaterialError('XMIND 里没有可读的主题文字')
    out.add('topics', '思维导图主题', '\n'.join(lines))
    out.warn('XMIND 只提取主题和备注文字；图片、附件、标签颜色和画布布局未提取。')
    return out.finish()


EMPTY_PAGE = '此页没有可提取文字，可能是空白页或扫描图片；尚未运行 OCR。'  # 放在没读出字的页里 (资料本不把它当正文)


def _pdf(data):
    try:
        from pypdf import PdfReader
    except ImportError:
        return {'format': 'pdf', 'parse_status': 'unavailable', 'error': '本机未安装 PDF 文字解析组件 pypdf；原文件已保存，尚未读取内容。'}
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        raise MaterialError('PDF 已加密；本轮不读取加密文档')
    out = Output('pdf')
    out.result['pages'] = len(reader.pages)
    empty, extracted = 0, 0
    for index in range(min(len(reader.pages), MAX_PAGES)):
        content = reader.pages[index].extract_text() or ''
        if not content.strip():
            empty += 1
            content = EMPTY_PAGE
        else:
            extracted += 1
        if not out.add('page', f'第 {index + 1} 页', content, page=index + 1):
            break
    if len(reader.pages) > MAX_PAGES:
        out.result['truncated'] = True
    if empty:
        out.warn(f'{empty} 个已检查页面没有可提取文字；图片中的文字尚未读取。')
    out.warn('PDF 只提取已有文字层；分栏和表格阅读顺序可能与视觉排版不同，图片与附件未提取。')
    result = out.finish()
    if empty and not extracted:
        result['parse_status'] = 'needs_ocr'
    return result


def _text(data, suffix, partial=False):
    encoding = 'utf-8-sig'
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        encoding = 'utf-16'
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as exc:
        if partial:
            # 分块读时末尾可能正好截断一个字符: 先按原编码替换, 只有替换点很少才认; 否则再试本地编码
            candidate = data.decode(encoding, errors='replace')
            if candidate.count('\ufffd') <= 2:
                text = candidate
            else:
                try:
                    text = data.decode('gb18030')
                except UnicodeDecodeError:
                    text = data.decode('gb18030', errors='replace')
                encoding = 'gb18030'
        else:
            try:
                text = data.decode('gb18030')
                encoding = 'gb18030'
            except UnicodeDecodeError:
                raise MaterialError('文本编码无法识别；没有用替换字符冒充原文') from exc
    if '\x00' in text:
        raise MaterialError('文件含二进制空字符，不能作为普通文本读取')
    out = Output(suffix.lstrip('.') or 'text')
    out.result['encoding'] = encoding
    # Preserve raw text offsets for existing CSV/JSON/text context_read callers.
    out.result['text'] = text[:MAX_TEXT]
    out.result['chars'] = min(len(text), MAX_TEXT)
    out.result['sections'] = [{'kind': 'text', 'label': '文本', 'start': 0, 'end': min(len(text), MAX_TEXT)}]
    if len(text) > MAX_TEXT:
        out.result.update(truncated=True, parse_status='partial')
        out.warn('文字超过 30 万字符，本次仅提取前 30 万字符；其余尚未读取。')
    if partial:
        out.result.update(truncated=True, parse_status='partial')
        out.warn('文件太大，先分块读了开头；后面的内容尚未读取。')
    return out.result


def _parse(data, suffix, *, limit=MAX_BYTES, partial=False, budget=None, depth=0):
    suffix = (suffix or '').lower()
    if len(data) > limit:
        raise MaterialError(f'单个附件最多 {limit // (1024 * 1024)} MB')
    if suffix in UNREADABLE_SUFFIXES:
        return {'format': suffix.lstrip('.'), 'parse_status': 'unsupported',
                'error': UNREADABLE_SUFFIXES[suffix], 'detected': suffix.lstrip('.')}
    if suffix not in READABLE_SUFFIXES:
        kind = sniff(data, suffix)
        if kind in UNREADABLE_SUFFIXES:
            return {'format': kind.lstrip('.'), 'parse_status': 'unsupported',
                    'error': UNREADABLE_SUFFIXES[kind], 'detected': kind.lstrip('.')}
        if kind:
            if kind == '.ole':
                kind = _ole_kind(data)
            suffix = kind
    if suffix not in READABLE_SUFFIXES and suffix not in IMAGE_SUFFIXES:
        return {'format': suffix.lstrip('.') or 'unknown', 'parse_status': 'unsupported',
                'error': f'文件头认不出是什么格式（后缀 {suffix or "无"}）；原文件已保存，尚未读取内容。'}
    if suffix in TEXT_SUFFIXES:
        return _text(data, suffix, partial)
    if suffix == '.docx':
        return _docx(data)
    if suffix == '.xlsx':
        return _xlsx(data)
    if suffix == '.pptx':
        return _pptx(data)
    if suffix == '.pdf':
        return _pdf(data)
    if suffix in {'.odt', '.ods', '.odp'}:
        return _odf(data, suffix)
    if suffix == '.rtf':
        return _rtf(data)
    if suffix == '.epub':
        return _epub(data)
    if suffix == '.ofd':
        return _ofd(data)
    if suffix in {'.eml', '.mhtml', '.mht'}:
        return _eml(data, suffix)
    if suffix == '.fb2':
        return _fb2(data)
    if suffix == '.xmind':
        return _xmind(data)
    if suffix in ARCHIVE_SUFFIXES:
        return _archive(data, suffix, budget=budget, depth=depth)
    if suffix in LEGACY_SUFFIXES:
        if data[:2] == b'PK':  # 有些 WPS / Office 文件其实已经是 OOXML 压缩包, 只是后缀没改
            if suffix in {'.xls', '.et'}:
                return _xlsx(data)
            if suffix in {'.ppt', '.dps'}:
                return _pptx(data)
            return _docx(data)
        if data[:5].lower() == b'{\\rtf':
            return _rtf(data)
        if data[:8] != _OLE_MAGIC:  # 另存成 HTML / CSV / 纯文字的 .doc / .xls 也能读
            if b'\x00' not in data[:4096]:
                return _text(data, suffix)
            raise MaterialError('老式 Office / WPS 文件头不认识，内容尚未读取')
        if suffix in {'.doc', '.wps'}:
            return _doc(data, suffix)
        if suffix in {'.xls', '.et'}:
            return _xls(data, suffix)
        return _ppt(data, suffix)
    if suffix in IMAGE_SUFFIXES:
        # 图片不走文字解析: 交给看图的模型 (动图采样成多帧拼图, 见 channels/media.to_data_url)
        return {'format': suffix.lstrip('.'), 'parse_status': 'image', 'text': '', 'chars': 0,
                'error': '', 'note': '图片会在这一轮直接交给看图模型；动图会采样成带时间戳的多帧拼图。'}
    return {'format': suffix.lstrip('.') or 'unknown', 'parse_status': 'unsupported',
            'error': f'认出来了是{_format_name(suffix)}，但本机还没有读它的组件；原文件已保存，尚未读取内容。'}


def _parse_file(path, suffix):
    """大文件已经分块下到临时文件: 从磁盘读, 不再受 32 MB 的单次上限, 但仍有总大小和解析资源上限."""
    size = path.stat().st_size
    if size > MAX_STREAM_BYTES:
        raise MaterialError(f'文件超过 {MAX_STREAM_BYTES // (1024 * 1024)} MB，尚未读取')
    return _parse(path.read_bytes(), suffix, limit=MAX_STREAM_BYTES)


def _windows_job(process, memory=None):
    """Bound the one owned parser process before giving it any document bytes."""
    import ctypes
    from ctypes import wintypes as W
    class Basic(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64), ('flags', W.DWORD),
                    ('minimum', ctypes.c_size_t), ('maximum', ctypes.c_size_t), ('active', W.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', W.DWORD), ('scheduling', W.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ('ro', 'wo', 'oo', 'rb', 'wb', 'ob')]
    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                    ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [ctypes.c_void_p, W.LPCWSTR], W.HANDLE
    kernel.SetInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    info = Extended()
    info.basic.flags = 0x100 | 0x2000  # PROCESS_MEMORY | KILL_ON_JOB_CLOSE
    info.process_memory = memory or MEMORY_BYTES
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(handle, W.HANDLE(int(process._handle))):
        if handle:
            kernel.CloseHandle(handle)
        raise MaterialError('无法建立附件解析的资源限制，未解析内容')
    return lambda: kernel.CloseHandle(handle)


def _run_worker(arguments, input_data, suffix):
    environment = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL'}}
    environment.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', PYTHONNOUSERSITE='1')
    process, close_job = None, None
    try:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *arguments],
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if os.name == 'nt':
            close_job = _windows_job(process)
        stdout, _ = process.communicate(input_data, timeout=TIMEOUT)
        if process.returncode or len(stdout) > 4 * 1024 * 1024:
            raise MaterialError('解析进程超出资源限制或异常退出，内容尚未读取')
        result = json.loads(stdout.decode('utf-8'))
        if not isinstance(result, dict) or 'parse_status' not in result:
            raise MaterialError('解析器未返回有效结果')
        return result
    except subprocess.TimeoutExpired:
        return {'format': suffix.lstrip('.'), 'parse_status': 'error',
                'error': f'本地解析超过 {TIMEOUT:g} 秒限制，内容尚未读取'}
    except (OSError, ValueError, TypeError) as exc:
        return {'format': suffix.lstrip('.'), 'parse_status': 'error', 'error': str(exc)[:300]}
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.communicate(timeout=2)
        if close_job:
            close_job()


def extract(data: bytes, suffix: str, partial: bool = False) -> dict:
    """Run the bounded parser and return explicit status; never fake an extraction."""
    suffix = suffix.lower()
    if len(data) > MAX_BYTES:
        return {'format': suffix.lstrip('.'), 'parse_status': 'error',
                'error': f'单个附件最多 {MAX_BYTES // (1024 * 1024)} MB'}
    arguments = ['--worker', suffix, *(['partial'] if partial else [])]
    return _run_worker(arguments, data, suffix)


def extract_path(path, suffix: str) -> dict:
    """大文件先分块下到磁盘再解析: 不把整个文件塞进服务端内存, 解析仍在小进程里跑."""
    suffix = suffix.lower()
    try:
        size = Path(path).stat().st_size
    except OSError as exc:
        return {'format': suffix.lstrip('.'), 'parse_status': 'error', 'error': str(exc)[:300]}
    if size > MAX_STREAM_BYTES:
        return {'format': suffix.lstrip('.'), 'parse_status': 'error',
                'error': f'文件超过 {MAX_STREAM_BYTES // (1024 * 1024)} MB，尚未读取'}
    return _run_worker(['--worker-file', suffix, str(path)], None, suffix)


if __name__ == '__main__' and len(sys.argv) >= 3 and sys.argv[1] in {'--worker', '--worker-file'}:
    logging.disable(logging.CRITICAL)
    try:
        if os.name != 'nt':
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
            resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT, TIMEOUT))
        if sys.argv[1] == '--worker-file':
            result = _parse_file(Path(sys.argv[3]), sys.argv[2])
        else:
            result = _parse(sys.stdin.buffer.read(MAX_BYTES + 1), sys.argv[2],
                            partial=len(sys.argv) > 3 and sys.argv[3] == 'partial')
    except Exception as exc:
        result = {'format': sys.argv[2].lstrip('.'), 'parse_status': 'error',
                  'error': str(exc)[:300] if isinstance(exc, MaterialError)
                  else f'文件格式损坏或解析器出错（{type(exc).__name__}），内容尚未读取'}
    print(json.dumps(result, ensure_ascii=False))
