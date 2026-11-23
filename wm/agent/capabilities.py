"""Honest inventory of this harness, built from its actual model-facing tools.

This is an inventory, not a second plugin loader or permission system. Cordis/DSH
packages require an adapter and are not presented as installed Python plugins.
"""
from __future__ import annotations

import copy

from . import plugin_host as PH
from . import policy as P
from . import studio as ST
from . import tools as T
from .prompt_context import workspace_rules


def tool_catalog(hub, session=None):
    direct = hub.studio.direct
    sources = {tool['name']: {'id': key, 'label': entry['label']} for key, entry in direct.servers().items()
               for tool in entry['tools']}
    offered = hub._offered(session) if session else None
    rows, seen = [], set()
    for spec in T.openai_tools() + ST.SPECS + direct.specs():
        item = spec['function']
        name = item['name']
        if name in seen:
            continue
        seen.add(name)
        builtin = name in T.NAMES | ST.NAMES
        source = {} if builtin else sources.get(name, {})
        rows.append({'name': name, 'label': ST.LABELS.get(name, T.LABEL.get(name, name)) if builtin else direct.label(name),
                     'description': item.get('description', ''), 'parameters': item.get('parameters', {}),
                     'enabled': offered is None or name in offered, 'source': 'builtin' if builtin else 'extension',
                     'source_id': source.get('id', ''), 'source_label': source.get('label', '内置'),
                     # This compatibility field means offered under read mode, NOT a sandbox guarantee.
                     'readonly': name in (ST.READ_ONLY | P.READ_TOOLS) if builtin else direct.read_only(name)})
    return rows


def inventory(hub, session=None, preset_id=None):
    presets = hub.presets.list()
    if preset_id:
        if preset_id not in {row['id'] for row in presets}:
            raise ValueError('没有这个 Agent 预设')
        if session is None:
            raise ValueError('请先选择会话再预览预设能力')
        session = copy.copy(session)
        session.preset = preset_id
    tools = tool_catalog(hub, session)
    names = {tool['name'] for tool in tools}
    groups = []

    def add(key, title, description, members=(), *, scope='session', available=True, detail='', status=None):
        chosen = [tool for tool in tools if tool['name'] in members]
        enabled = sum(row['enabled'] for row in chosen)
        if status is None:
            status = ('ready' if enabled == len(chosen) else 'partial' if enabled else 'inactive') if chosen else 'ready' if available else 'missing'
        groups.append({'id': key, 'name': title, 'description': description, 'scope': scope, 'status': status,
                       'tools': chosen, 'available_count': enabled, 'total_count': len(chosen), 'detail': detail})

    add('persona', '角色与工作预设', '将绫音的人设与本会话工作指令组合。', detail='人设、记忆和工作方式分开管理；预设不会提高权限。')
    rules = workspace_rules(hub, session) if session else []
    loaded = [row['name'] for row in rules if row['state'] == 'loaded']
    add('agent-instructions', '工作区规则', '自动读取根目录的 AGENTS.md 和 CLAUDE.md。',
        detail=('已读取：' + '、'.join(loaded) if loaded else '当前没有可读取的根目录规则文件。') + ' 每个文件最多载入 24 KB，子目录规则按需读取。')
    clocks = {name for name in names if name in {'clock_now', 'clock_diff', 'clock_plan'}}
    add('time-context', '时间与日期', '按需读取当前时间、计算时差与安排日期。', clocks, available=bool(clocks),
        detail='由已启用的时钟扩展提供；不会每一步改写历史消息或模型缓存前缀。')
    add('tool-fs', '文件读写与搜索', '读取、编辑、搜索工作区文件。', {'read_file', 'write_file', 'edit_file', 'list_dir', 'glob', 'grep'})
    add('tool-pwsh', 'PowerShell 命令', '执行本机命令并记录结果。', {'run_command'}, detail='命令工具与独立终端面板各有运行记录；不是 Bash 或虚拟机。')
    add('tool-input', '鼠标点按', '用 Windows 底层事件点鼠标；现在只会点按，键盘还没有。', {'mouse_click'},
        detail='点按会动真实鼠标，可能碰到任何窗口：只可查看档不让，完全授权之外每次都问。')
    add('tool-web', '网页搜索与读取', '搜索公开网页并提取正文。', {'web_search', 'web_fetch'}, detail='由本会话的智能搜索开关及权限共同决定是否可用。')
    add('tool-skill', '技能与 MCP', '发现技能，按需载入说明和连接工具服务。', {'extensions_list', 'skill_read', 'mcp_tools', 'mcp_call'})
    add('present', '文件交付与验收', '登记实际文件、读取成果并保留版本与验收证据。', {'artifact_register', 'artifact_list', 'artifact_read'})
    add('tool-subagent', '嵌套子任务', '独立上下文并行执行，等待结果或停止任务。', {'delegate_tasks', 'delegate_status', 'delegate_wait', 'delegate_cancel', 'worktree_status', 'worktree_export'})
    add('tool-todo', '计划与待办', '维护任务步骤、状态和验收标准。', {'task_plan'} | {name for name in names if name.startswith('todo_')})
    add('tool-plugin-manager', '创造与扩展管理', '通过聊天创建技能、MCP 工具和独立界面，管理版本。', {'extension_create', 'extension_enable', 'extension_rollback'}, detail='支持 WorldModel 扩展格式；DSH/Cordis 的 npm 插件不能直接装进此运行时。')
    add('context', '上下文与原始记录', '折叠长工具结果，保留原始正文并分页回查。', {'history_search', 'history_read', 'context_inspect', 'context_read'}, detail='当前压缩是工具结果投影；不宣称已实现 DSH 的模型摘要压缩或 SQLite 全文检索。')
    add('life', '生活与成长', '查看和调整真实生活参数，记录依据并恢复。', {'life_settings', 'growth_status', 'growth_evaluate', 'growth_adopt'})
    add('resident', '后台持续任务', '在明确目标下后台推进，记录进度并在需要你时通知。',
        {'resident_status', 'resident_update'}, available=bool({'resident_status', 'resident_update'} & names),
        detail='任务由持续任务面板显式建立；普通聊天不会自动成为常驻任务。关机时本机不会继续运行。')
    covered = {tool['name'] for group in groups for tool in group['tools']}
    for source_id in sorted({tool['source_id'] for tool in tools if tool['source'] == 'extension'}):
        selected = [tool for tool in tools if tool['source_id'] == source_id and tool['name'] not in covered]
        if not selected:
            continue
        title = selected[0]['source_label']
        detail = '工具清单来自当前启用版本的快照；版本变更或停用后不会继续暴露旧工具。'
        if any(tool['name'] == 'schedule_due' for tool in selected):
            detail += ' 此扩展仅在调用 schedule_due 时检查到期，本身不会后台唤醒。'
        if any(tool['name'] == 'goal_set' for tool in selected):
            detail += ' 此扩展保存目标状态；保存目标本身不代表自动续行。'
        add(source_id, title, '已启用的扩展工具，可直接用于当前 Agent。', {tool['name'] for tool in selected}, detail=detail)
        covered |= {tool['name'] for tool in selected}
    remaining = names - covered
    if remaining:
        add('other-tools', '其他已注册工具', '当前运行时提供的其他能力。', remaining)
    add('sessions', '会话存储与恢复', '本地保存会话、材料、反馈和分支。', scope='global', detail='JSON 文件持久化；原始记录与模型上下文视图分开。')
    add('authorization', '工具授权与停止', '工具调用使用同一份权限、黑名单与停止信号。', scope='global', detail='完全授权与用户黑名单均保留；这是工具策略，不是操作系统沙箱。')
    add('models', '模型与用量账本', '复用模型配置，记录输入、输出、缓存与实际调用花费。', scope='global')
    add('platform', '社区、贡献与更新', '本地导入导出、数据贡献预览和可验证更新。', scope='global', detail='公网社区需要部署服务；数据贡献由用户单独选择，默认关闭。')
    add('workflow-ptc', 'JavaScript 工作流 / PTC', '脚本编排多项工具，并共享工作流运行时。', scope='planned', available=False, detail='尚未接入。现有并行子任务与命令执行不等于 PTC。')
    add('cordis', 'DSH 原生插件兼容', '直接运行依赖 Cordis、Typert 或 DSH 插槽的 npm 插件。', scope='planned', available=False, detail='尚未接入兼容层。现有能力采用本机实现或 MCP 接入。')
    add('office-preview', 'Office 完整预览', '将 Office 文档转换为 PDF 并保留页面布局。', scope='planned', available=False, detail='已有 DOCX / XLSX / PDF 文本解析；不等于 Office 渲染与编辑。')
    result = {'groups': groups, 'tools': tools, 'presets': [{'id': row['id'], 'name': row['name']} for row in presets],
              'preset_id': session.preset if session else '', 'session_id': session.id if session else '',
              'preview': bool(preset_id), 'tool_count': len(tools), 'enabled_count': sum(row['enabled'] for row in tools)}
    # 插件宿主状态: 只在宿主开关开着时下发; 关着时这个键完全不存在 (面板据此一个元素都不显示)。
    if PH.switch_on():
        host = PH.status()
        if isinstance(host, dict):
            result['plugins'] = host
    return result
