"""Workbench direct tools: expose selected Creator-extension tools as native tools.

Extensions normally only reach the model through mcp_tools / mcp_call. That costs an extra
round trip and the model has to remember tool names. This module lets a short, reviewed list
of extension tools (workbench-clock/jobs/goal/todo/schedule/memo) appear in the normal tool
table, dispatched straight to the extension's MCP server.

Discovery would need an MCP handshake, which is async, while StudioRuntime.tools() is sync.
So the tool list is snapshotted by tools/workbench-plugins/sync_direct_tools.py into
extensions/direct-tools.json. The snapshot is only trusted while its revision matches the
extension's active revision; a stale or missing snapshot simply exposes nothing.

This is not a sandbox: approved tools still run the extension's own process.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

CACHE = 'direct-tools.json'


class DirectTools:
    def __init__(self, home, *, reserved=()):
        self.home = Path(home)
        self.root = self.home / 'extensions'
        self.cache_path = self.root / CACHE
        self.registry_path = self.root / 'registry.json'
        self.reserved = frozenset(reserved)

    def _cache(self):
        try:
            data = json.loads(self.cache_path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError, UnicodeError):
            return {}
        servers = data.get('servers') if isinstance(data, dict) else None
        return servers if isinstance(servers, dict) else {}

    def _active(self):
        try:
            state = json.loads(self.registry_path.read_text(encoding='utf-8-sig'))
            packages = state.get('packages') if isinstance(state, dict) else None
            return {k: v.get('active_revision') for k, v in (packages or {}).items()
                    if isinstance(v, dict) and v.get('enabled')}
        except (OSError, ValueError, UnicodeError, AttributeError):
            return {}

    def servers(self):
        """Only servers whose snapshot matches the currently enabled revision."""
        active = self._active()
        out = {}
        for server_id, entry in self._cache().items():
            if not isinstance(server_id, str) or not server_id.startswith('creator:') or not isinstance(entry, dict):
                continue
            package = server_id.split(':', 1)[-1]
            revision = entry.get('revision')
            if not isinstance(revision, str) or not revision or active.get(package) != revision:
                continue
            declared = entry.get('tools')
            if not isinstance(declared, list):
                continue
            tools = [t for t in declared
                     if isinstance(t, dict) and isinstance(t.get('name'), str)
                     and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', t['name']) and t['name'] not in self.reserved
                     and isinstance(t.get('inputSchema'), dict) and isinstance(t.get('read_only'), bool)]
            if tools:
                out[server_id] = {'revision': entry.get('revision'), 'label': entry.get('label') or package,
                                  'tools': tools}
        # Never dispatch a name according to registry order. Both schemas and
        # execution must identify one unambiguous extension, and builtins win.
        counts = Counter(t['name'] for entry in out.values() for t in entry['tools'])
        for entry in out.values():
            entry['tools'] = [t for t in entry['tools'] if counts[t['name']] == 1]
        return {key: entry for key, entry in out.items() if entry['tools']}

    def names(self, read_only=False):
        return {tool['name'] for entry in self.servers().values() for tool in entry['tools']
                if not read_only or tool.get('read_only')}

    def _tool(self, name):
        for server_id, entry in self.servers().items():
            for tool in entry['tools']:
                if tool['name'] == name:
                    return server_id, entry, tool
        return None, None, None

    def server_for(self, name):
        return self._tool(name)[0]

    def read_only(self, name):
        tool = self._tool(name)[2]
        return bool(tool and tool.get('read_only'))

    def label(self, name):
        server_id, entry, tool = self._tool(name)
        if not tool:
            return name
        return str(tool.get('label') or tool['name'])[:40]

    def specs(self, names=None, read_only=False):
        """OpenAI tool specs, filtered by an optional name set; first declaration wins."""
        out, seen = [], set()
        for entry in self.servers().values():
            for tool in entry['tools']:
                name = tool['name']
                if name in seen or (names is not None and name not in names):
                    continue
                if read_only and not tool.get('read_only'):
                    continue
                seen.add(name)
                parameters = tool.get('inputSchema') if isinstance(tool.get('inputSchema'), dict) else {}
                out.append({'type': 'function', 'function': {
                    'name': name,
                    'description': str(tool.get('description') or entry.get('label') or name)[:1500],
                    'parameters': parameters or {'type': 'object', 'properties': {}}}})
        return out
