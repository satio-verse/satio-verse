"""网络插件·这一块要用的 Windows 调用 (2026-11-11 "网络"插件干跑的一块; 契约: 网络动作接口 v0.2 第 0、1、4、10 节): 作业对象、句柄、命名管道、
网卡在不在、清 DNS 缓存、关机信号, 用 ctypes 包了一层. 每个函数的说明里写着用了哪个系统调用.

全部不需要管理员权限 (作业对象、句柄、自己建的命名管道、只读地查网卡, 都是普通权限能做的); 只在 wm/net/ 里用, 项目别处不引用; 只用标准库
(ctypes 也算). 模块导进来不碰系统: 要用的 DLL 在第一次调用时才加载, 而且每个库对象自己设参数类型, 不去改 ctypes.windll 那份全进程共用的
(同 wm/privilege.py). 不是 Windows 时导入照样成功, 一调用就抛 OSError.

- 作业对象: create_job (CreateJobObjectW + SetInformationJobObject, 带"最后一个句柄关掉就结束里面所有进程", 不带两个允许脱离作业的标志)、assign_process、
  terminate_job、job_active_processes、job_limit_flags (读作业对象的限制标志). 看护方没了, 它手里的作业对象句柄跟着关掉, 核心就跟着没: 契约第 0 节
  "看护方没了, 核心跟着没"靠的就是这两项设置, 看护方启动时用 job_limit_flags 核对 (契约第 10 节〔补 2〕).
- 句柄: set_inheritable、duplicate_handle (可以只留部分权利, 比如作业对象只留结束和查询)、close_handle、handle_valid、
  spawn_inheriting (起子进程时只让列出的几个句柄被继承, 用 subprocess 的 STARTUPINFO(lpAttributeList={"handle_list": [...]}))、
  process_exit_code、open_process、process_alive.
- 进程: process_creation_time (GetProcessTimes: 进程号会被重用, 创建时间不会, 两样都对才认得出是同一个进程)、terminate_process (TerminateProcess, 打开时用
  PROCESS_KILL_ACCESS)、wait_process (WaitForSingleObject, 带期限); pipe_server_pid / PipeClient.server_pid (GetNamedPipeServerProcessId: 管道对面是哪个进程).
- 按种类认句柄 (is_job_object、is_process_handle、process_in_job、is_pipe_handle): 句柄只是个数字, 数字碰巧有效不等于继承到的就是约好的那个东西;
  看护方启动时按种类和关系核对交接来的三个句柄 (QueryInformationJobObject、GetExitCodeProcess、IsProcessInJob、GetFileType).
- 命名管道: 服务端 pipe_server_create (只许当前用户和 SYSTEM, 只建一个实例, 不接受远程) 和 pipe_server_connect / pipe_server_disconnect /
  pipe_peek / pipe_read / pipe_write / pipe_flush, 客户端 pipe_connect / PipeClient / pipe_request (带超时).
- adapter_names / adapter_exists: 只读地查有没有叫这个名字的网卡 (GetAdaptersAddresses).
- flush_dns: 清系统 DNS 缓存 (dnsapi 的 DnsFlushResolverCache). **写出来了, 但干跑这一步里任何时候都不许真的去调它**: 测试把底层的
  _dns_flush_resolver_cache 换成假的.
- set_console_ctrl_handler: 关机、注销、控制台被关时通知我们 (SetConsoleCtrlHandler).
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import time
from ctypes import wintypes

# ---- 常量 -----------------------------------------------------------------------------------------------------------
JOB_OBJECT_ASSIGN_PROCESS = 0x0001
JOB_OBJECT_QUERY = 0x0004
JOB_OBJECT_TERMINATE = 0x0008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800                                    # 允许里面的进程脱离作业 (CREATE_BREAKAWAY_FROM_JOB): 契约要求不带
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x1000                             # 里面的子进程自动不进作业: 契约要求不带
JOB_LIMITED_ACCESS = JOB_OBJECT_TERMINATE | JOB_OBJECT_QUERY            # 交给看护方的作业对象句柄: 只带结束和查询两项权利
_JOB_BASIC_ACCOUNTING = 1                                                 # JobObjectBasicAccountingInformation
_JOB_EXTENDED_LIMIT = 9                                                   # JobObjectExtendedLimitInformation

SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_LIMITED_ACCESS = SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION  # 交给看护方的核心进程句柄: 能等它、能查退出码, 别的不行
PROCESS_KILL_ACCESS = PROCESS_TERMINATE | PROCESS_LIMITED_ACCESS          # 结束一个进程要的权利: 能结束、能查创建时间和退出码、能等它没了 (open_process 的 access)
HANDLE_FLAG_INHERIT = 0x00000001
DUPLICATE_SAME_ACCESS = 0x00000002

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
PIPE_ACCESS_DUPLEX = 0x00000003
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000                                # 这个名字上已经有实例了就失败: 别人抢不了这个名字
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008                                   # 不接受远程连接
PIPE_OPEN_MODE = PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE
PIPE_MODE = PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS
PIPE_MAX_INSTANCES = 1                                                    # 只建一个实例
PIPE_BUFFER = 65536

ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
ERROR_BROKEN_PIPE = 109
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_BUFFER_OVERFLOW = 111
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_PIPE_CONNECTED = 535
ERROR_PIPE_BUSY = 231
_CLOSED_PIPE_ERRORS = (ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED)
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
FILE_TYPE_PIPE = 3                                                        # GetFileType: 管道 (命名管道、匿名管道; 套接字也报这个)

TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
SDDL_REVISION_1 = 1
DACL_SECURITY_INFORMATION = 0x00000004

GAA_FLAGS = 0x1 | 0x2 | 0x4 | 0x8 | 0x100      # 不要单播/任播/组播/DNS 地址, 但要所有接口 (包括没配 IP 的)
AF_UNSPEC = 0

CTRL_C_EVENT, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT = 0, 1, 2, 5, 6

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PipeClosed(OSError):
    """管道对面已经断开 (ERROR_BROKEN_PIPE、ERROR_NO_DATA、ERROR_PIPE_NOT_CONNECTED)."""


def _error(api: str, code: int | None = None) -> OSError:
    code = ctypes.get_last_error() if code is None else code
    return ctypes.WinError(code, f"{api} 失败（{ctypes.FormatError(code).strip()}）")


# ---- 结构 -----------------------------------------------------------------------------------------------------------

class _IoCounters(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong), ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong), ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong), ("OtherTransferCount", ctypes.c_ulonglong)]


class _JobBasicLimit(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _JobExtendedLimit(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JobBasicLimit), ("IoInfo", _IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _JobBasicAccounting(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64), ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64), ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD), ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


class _FileTime(ctypes.Structure):
    """FILETIME: 1601 年 1 月 1 日以来的 100 纳秒数, 拆成低 32 位和高 32 位."""
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wintypes.BOOL)]


class _AdapterAddresses(ctypes.Structure):
    """IP_ADAPTER_ADDRESSES 的前半截 (只用到 Next 和 FriendlyName; 后面的字段不声明, 靠 Next 指针走链表)."""


_AdapterAddresses._fields_ = [
    ("Length", wintypes.ULONG), ("IfIndex", wintypes.DWORD), ("Next", ctypes.POINTER(_AdapterAddresses)),
    ("AdapterName", ctypes.c_char_p), ("FirstUnicastAddress", ctypes.c_void_p), ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p), ("FirstDnsServerAddress", ctypes.c_void_p), ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p), ("FriendlyName", ctypes.c_wchar_p)]


# ---- 库 (第一次用到才加载) ----------------------------------------------------------------------------------------------

class _Libs:
    """kernel32、advapi32、iphlpapi 的函数, 参数类型和返回类型都设好了 (自己的库对象, 不碰 ctypes.windll)."""


_LIBS: _Libs | None = None
_LIBS_LOCK = threading.Lock()


def _declare(lib: object, name: str, restype: object, *argtypes: object) -> object:
    func = getattr(lib, name)
    func.restype = restype
    func.argtypes = list(argtypes)
    return func


def _load() -> _Libs:
    if sys.platform != "win32":
        raise OSError("wm.net.win32 只能在 Windows 上用")
    h, p, dw, bl, ws = ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR
    pdw = ctypes.POINTER(wintypes.DWORD)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    ip = ctypes.WinDLL("iphlpapi", use_last_error=True)
    libs = _Libs()
    libs.handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    for name, restype, *argtypes in (
            ("CloseHandle", bl, h), ("GetCurrentProcess", h), ("OpenProcess", h, dw, bl, dw), ("WaitForSingleObject", dw, h, dw),
            ("GetExitCodeProcess", bl, h, pdw), ("SetHandleInformation", bl, h, dw, dw), ("GetHandleInformation", bl, h, pdw),
            ("DuplicateHandle", bl, h, h, h, ctypes.POINTER(ctypes.c_void_p), dw, bl, dw),
            ("CreateJobObjectW", h, p, ws), ("SetInformationJobObject", bl, h, ctypes.c_int, p, dw),
            ("AssignProcessToJobObject", bl, h, h), ("TerminateJobObject", bl, h, wintypes.UINT),
            ("QueryInformationJobObject", bl, h, ctypes.c_int, p, dw, pdw),
            ("CreateNamedPipeW", h, ws, dw, dw, dw, dw, dw, dw, p), ("ConnectNamedPipe", bl, h, p), ("DisconnectNamedPipe", bl, h),
            ("PeekNamedPipe", bl, h, p, dw, pdw, pdw, pdw), ("ReadFile", bl, h, p, dw, pdw, p), ("WriteFile", bl, h, p, dw, pdw, p),
            ("FlushFileBuffers", bl, h), ("CreateFileW", h, ws, dw, dw, p, dw, dw, h), ("WaitNamedPipeW", bl, ws, dw),
            ("GetSystemWindowsDirectoryW", wintypes.UINT, wintypes.LPWSTR, wintypes.UINT), ("LocalFree", h, h),
            ("IsProcessInJob", bl, h, h, ctypes.POINTER(wintypes.BOOL)), ("GetFileType", dw, h),
            ("GetProcessTimes", bl, h, *(ctypes.POINTER(_FileTime),) * 4), ("TerminateProcess", bl, h, wintypes.UINT),
            ("GetNamedPipeServerProcessId", bl, h, ctypes.POINTER(wintypes.ULONG))):
        setattr(libs, name, _declare(k32, name, restype, *argtypes))
    libs.SetConsoleCtrlHandler = _declare(k32, "SetConsoleCtrlHandler", bl, libs.handler_type, bl)
    for name, restype, *argtypes in (
            ("OpenProcessToken", bl, h, dw, ctypes.POINTER(ctypes.c_void_p)), ("GetTokenInformation", bl, h, ctypes.c_int, p, dw, pdw),
            ("ConvertSidToStringSidW", bl, p, ctypes.POINTER(ctypes.c_void_p)),
            ("ConvertStringSecurityDescriptorToSecurityDescriptorW", bl, ws, dw, ctypes.POINTER(ctypes.c_void_p), p),
            ("ConvertSecurityDescriptorToStringSecurityDescriptorW", bl, p, dw, dw, ctypes.POINTER(ctypes.c_void_p), p),
            ("GetKernelObjectSecurity", bl, h, dw, p, dw, pdw)):
        setattr(libs, name, _declare(adv, name, restype, *argtypes))
    libs.GetAdaptersAddresses = _declare(ip, "GetAdaptersAddresses", wintypes.ULONG, wintypes.ULONG, wintypes.ULONG, p, p,
                                         ctypes.POINTER(wintypes.ULONG))
    return libs


def _libs() -> _Libs:
    global _LIBS
    if _LIBS is None:
        with _LIBS_LOCK:
            if _LIBS is None:
                _LIBS = _load()
    return _LIBS


# ---- 句柄 -----------------------------------------------------------------------------------------------------------

def close_handle(handle: int) -> None:
    """关掉一个句柄 (CloseHandle). 已经无效 / 是 0 就算了, 不抛异常: 收尾时反复关也安全."""
    if handle and handle != INVALID_HANDLE_VALUE:
        _libs().CloseHandle(handle)


def handle_valid(handle: int) -> bool:
    """这个数现在是不是本进程里一个有效的句柄 (GetHandleInformation). 只说"这个数有没有对应的句柄", 不说它是什么: 数字碰巧有效不等于继承到的就是约好的
    那个东西, 所以看护方启动时不用它核对交接来的句柄, 按种类认 (is_job_object、is_process_handle、process_in_job、is_pipe_handle)."""
    if not handle or handle == INVALID_HANDLE_VALUE:
        return False
    flags = wintypes.DWORD()
    return bool(_libs().GetHandleInformation(handle, ctypes.byref(flags)))


# ---- 按种类认句柄 ------------------------------------------------------------------------------------------------------
# 句柄只是个数字. "这个数在我这里有效" (handle_valid) 不等于"我继承到的就是约好的那个东西": 子进程自己也有别的有效句柄, 数字碰巧对上了也说得通.
# 要认的是种类: 作业对象、进程、管道; 还要认关系: 这个进程在不在这个作业里. 下面四个都不需要管理员权限, 用交接时那种只留部分权利的复制件
# (作业对象: 结束和查询; 进程: SYNCHRONIZE 和 PROCESS_QUERY_LIMITED_INFORMATION) 就够.

def is_job_object(handle: int) -> bool:
    """这个数在本进程里是不是一个作业对象句柄 (并且带查询权): QueryInformationJobObject 信息类 1 (48 字节的缓冲区) 调得通, 只有作业对象句柄能."""
    if not handle or handle == INVALID_HANDLE_VALUE:
        return False
    info = _JobBasicAccounting()
    return bool(_libs().QueryInformationJobObject(handle, _JOB_BASIC_ACCOUNTING, ctypes.byref(info), ctypes.sizeof(info), None))


def is_process_handle(handle: int) -> bool:
    """这个数是不是一个进程句柄 (并且带查询权): GetExitCodeProcess 调得通, 别的种类 (作业、线程、事件、文件...) 都不行."""
    if not handle or handle == INVALID_HANDLE_VALUE:
        return False
    code = wintypes.DWORD()
    return bool(_libs().GetExitCodeProcess(handle, ctypes.byref(code)))


def process_in_job(process: int, job: int) -> bool:
    """这个进程在不在这个作业里 (IsProcessInJob; 进程句柄要有 PROCESS_QUERY_(LIMITED_)INFORMATION, 作业句柄要有 JOB_OBJECT_QUERY).
    作业套作业时, 进程在这个作业的下层作业里也算在. 调不通 (句柄种类不对、没有权利) 就是 False."""
    result = wintypes.BOOL()
    if not _libs().IsProcessInJob(process, job, ctypes.byref(result)):
        return False
    return bool(result.value)


def is_pipe_handle(handle: int) -> bool:
    """这个数是不是一个管道句柄 (GetFileType 等于 FILE_TYPE_PIPE=3; 命名管道和匿名管道都是, Python 的 subprocess.PIPE 是匿名管道; 套接字也报 3,
    所以这只是核对种类, 不是分辨哪一根). 注意: 别的线程正堵在同一个 (同步) 句柄的读写上时, GetFileType 会跟着排队等; 启动时核对, 那时还没人用它."""
    if not handle or handle == INVALID_HANDLE_VALUE:
        return False
    return _libs().GetFileType(handle) == FILE_TYPE_PIPE


def is_inheritable(handle: int) -> bool:
    """这个句柄现在能不能被子进程继承 (GetHandleInformation 的 HANDLE_FLAG_INHERIT); 句柄无效也是 False."""
    flags = wintypes.DWORD()
    return bool(_libs().GetHandleInformation(handle, ctypes.byref(flags))) and bool(flags.value & HANDLE_FLAG_INHERIT)


def set_inheritable(handle: int, inheritable: bool = True) -> None:
    """设置句柄能不能被子进程继承 (SetHandleInformation 的 HANDLE_FLAG_INHERIT)."""
    if not _libs().SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT if inheritable else 0):
        raise _error("SetHandleInformation")


def duplicate_handle(handle: int, *, access: int | None = None, inheritable: bool = False) -> int:
    """在本进程里复制一个句柄 (DuplicateHandle, 源和目标都是本进程). access 给了就只留这些权利 (比如 JOB_LIMITED_ACCESS: 只带结束和
    查询), 不给就和原来一样 (DUPLICATE_SAME_ACCESS). inheritable 决定新句柄能不能被子进程继承. 返回新句柄 (调用方负责关)."""
    libs = _libs()
    me = libs.GetCurrentProcess()
    out = ctypes.c_void_p()
    options = DUPLICATE_SAME_ACCESS if access is None else 0
    if not libs.DuplicateHandle(me, handle, me, ctypes.byref(out), access or 0, bool(inheritable), options):
        raise _error("DuplicateHandle")
    return out.value


_SPAWN_LOCK = threading.Lock()


def spawn_inheriting(args: list[str], handles: list[int], **kwargs: object) -> subprocess.Popen:
    """起一个子进程, 只让 handles 里列的句柄被它继承 (别的句柄, 包括别处刚好设成可继承的, 都不会漏给它).

    用 subprocess.Popen 的 STARTUPINFO(lpAttributeList={"handle_list": handles}) (底层是 CreateProcess 的
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST); 列出的句柄这里先设成可继承. 起的时候加锁: 同一个进程里同时起两个这样的子进程不会互相串.
    kwargs 原样交给 Popen (creationflags、cwd、env、stdin/stdout/stderr 等); 标准输入输出如果重定向了, Python 会自己把它们也列进去."""
    if not handles:
        raise ValueError("handles 不能是空的")
    with _SPAWN_LOCK:
        for handle in handles:
            set_inheritable(handle, True)
        startup = subprocess.STARTUPINFO(lpAttributeList={"handle_list": list(handles)})
        return subprocess.Popen(args, startupinfo=startup, **kwargs)  # type: ignore[call-overload]


def open_process(pid: int, access: int = PROCESS_LIMITED_ACCESS, inheritable: bool = False) -> int:
    """按进程号打开一个进程句柄 (OpenProcess). 失败抛 OSError (进程不存在是 winerror 87, 没权限是 5)."""
    handle = _libs().OpenProcess(access, bool(inheritable), pid)
    if not handle:
        raise _error("OpenProcess")
    return handle


def process_exit_code(handle: int) -> int | None:
    """进程有没有退出: 没有返回 None, 退了返回退出码 (先 WaitForSingleObject(句柄, 0) 判断, 再 GetExitCodeProcess;
    不靠 STILL_ACTIVE=259, 因为进程自己可以用 259 退出). 句柄要有 SYNCHRONIZE 和 PROCESS_QUERY_LIMITED_INFORMATION."""
    libs = _libs()
    waited = libs.WaitForSingleObject(handle, 0)
    if waited == WAIT_TIMEOUT:
        return None
    if waited != WAIT_OBJECT_0:
        raise _error("WaitForSingleObject")
    code = wintypes.DWORD()
    if not libs.GetExitCodeProcess(handle, ctypes.byref(code)):
        raise _error("GetExitCodeProcess")
    return code.value


def process_alive(pid: int) -> bool:
    """这个进程号的进程还活着吗 (OpenProcess + WaitForSingleObject). 打不开也分不清的 (没权限) 当它活着."""
    handle = _libs().OpenProcess(PROCESS_LIMITED_ACCESS, False, pid)
    if not handle:
        return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
    try:
        return _libs().WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        _libs().CloseHandle(handle)


def process_creation_time(handle: int) -> int:
    """进程的创建时间 (GetProcessTimes 的第一项; 句柄要有 PROCESS_QUERY_(LIMITED_)INFORMATION): FILETIME, 1601 年 1 月 1 日以来的 100 纳秒数, 一个整数.
    进程号会被重用, 创建时间不会重: (进程号, 创建时间) 两样都对得上, 才认得出"就是我们当时起的那个进程". 失败抛 OSError."""
    created, exited, kernel, user = _FileTime(), _FileTime(), _FileTime(), _FileTime()
    if not _libs().GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
        raise _error("GetProcessTimes")
    return (created.dwHighDateTime << 32) | created.dwLowDateTime


def terminate_process(handle: int, exit_code: int = 1) -> bool:
    """结束一个进程 (TerminateProcess; 句柄要有 PROCESS_TERMINATE, 打开时用 PROCESS_KILL_ACCESS). 成功 True; 没这项权利、句柄无效、进程已经退了 False.
    只是发出结束的请求, 进程真没了要用 wait_process 等."""
    return bool(_libs().TerminateProcess(handle, exit_code))


def wait_process(handle: int, timeout_s: float) -> bool:
    """等进程退出, 最多 timeout_s 秒 (WaitForSingleObject; 句柄要有 SYNCHRONIZE): 退了返回 True, 到时间还没退返回 False."""
    millis = max(0, min(int(timeout_s * 1000), 0xFFFFFFFE))
    waited = _libs().WaitForSingleObject(handle, millis)
    if waited == WAIT_OBJECT_0:
        return True
    if waited == WAIT_TIMEOUT:
        return False
    raise _error("WaitForSingleObject")


def windows_directory() -> str:
    """系统的 Windows 目录 (GetSystemWindowsDirectoryW), 比如 C:\\Windows. 不读环境变量: 给不继承调用方环境的场合用."""
    buf = ctypes.create_unicode_buffer(520)
    size = _libs().GetSystemWindowsDirectoryW(buf, len(buf))
    if not size or size >= len(buf):
        raise _error("GetSystemWindowsDirectoryW")
    return buf.value


# ---- 作业对象 -------------------------------------------------------------------------------------------------------

def create_job(kill_on_close: bool = True, *, breakaway_ok: bool = False, silent_breakaway_ok: bool = False) -> int:
    """建一个作业对象 (CreateJobObjectW), 默认带 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (SetInformationJobObject, 信息类 9):
    最后一个句柄关掉 (包括持有它的进程崩了) 时, 里面所有进程一起被结束; 不带那两个允许脱离作业的标志 (JOB_OBJECT_LIMIT_BREAKAWAY_OK、
    JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK): 里面的进程脱离不了作业. 这是契约 4.1 第 8 步要的两项设置, 看护方启动时按它核对 (job_limit_flags).
    kill_on_close=False 和两个 breakaway 参数只给测试用 (造"设置不合规矩"的作业). 返回句柄 (不可继承; 要交给别的进程时用 duplicate_handle)."""
    libs = _libs()
    job = libs.CreateJobObjectW(None, None)
    if not job:
        raise _error("CreateJobObjectW")
    flags = ((JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE if kill_on_close else 0) | (JOB_OBJECT_LIMIT_BREAKAWAY_OK if breakaway_ok else 0)
             | (JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK if silent_breakaway_ok else 0))
    if flags:
        info = _JobExtendedLimit()
        info.BasicLimitInformation.LimitFlags = flags
        if not libs.SetInformationJobObject(job, _JOB_EXTENDED_LIMIT, ctypes.byref(info), ctypes.sizeof(info)):
            code = ctypes.get_last_error()
            libs.CloseHandle(job)
            raise _error("SetInformationJobObject", code)
    return job


def assign_process(job: int, process: int) -> None:
    """把进程放进作业对象 (AssignProcessToJobObject). 进程句柄要有 PROCESS_SET_QUOTA 和 PROCESS_TERMINATE (subprocess.Popen 给的够用)."""
    if not _libs().AssignProcessToJobObject(job, process):
        raise _error("AssignProcessToJobObject")


def terminate_job(job: int, exit_code: int = 1) -> bool:
    """结束作业对象里的所有进程 (TerminateJobObject; 句柄要有 JOB_OBJECT_TERMINATE). 成功返回 True; 句柄没这个权利或已无效返回 False."""
    return bool(_libs().TerminateJobObject(job, exit_code))


def job_limit_flags(job: int) -> int | None:
    """作业对象现在的限制标志 (QueryInformationJobObject 信息类 9 的 BasicLimitInformation.LimitFlags; 句柄要有 JOB_OBJECT_QUERY). 读不出来 (这个数不是
    作业对象、没有查询权) 是 None. 看护方启动时用它核对作业对象的两项设置: 带 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (句柄全关掉时结束里面所有进程)、
    不带 JOB_OBJECT_LIMIT_BREAKAWAY_OK 和 JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK (里面的进程不许脱离作业)."""
    if not job or job == INVALID_HANDLE_VALUE:
        return None
    info = _JobExtendedLimit()
    if not _libs().QueryInformationJobObject(job, _JOB_EXTENDED_LIMIT, ctypes.byref(info), ctypes.sizeof(info), None):
        return None
    return int(info.BasicLimitInformation.LimitFlags)


def job_active_processes(job: int) -> int:
    """作业对象里现在还有几个活着的进程 (QueryInformationJobObject, 信息类 1 的 ActiveProcesses; 句柄要有 JOB_OBJECT_QUERY)."""
    info = _JobBasicAccounting()
    if not _libs().QueryInformationJobObject(job, _JOB_BASIC_ACCOUNTING, ctypes.byref(info), ctypes.sizeof(info), None):
        raise _error("QueryInformationJobObject")
    return info.ActiveProcesses


# ---- 当前用户和管道的访问控制 ------------------------------------------------------------------------------------------------

def _wide_string(libs: _Libs, pointer: int | None) -> str:
    """LocalAlloc 出来的宽字符串 -> str, 并释放."""
    try:
        return ctypes.wstring_at(pointer) if pointer else ""
    finally:
        if pointer:
            libs.LocalFree(pointer)


def current_user_sid() -> str:
    """当前进程令牌的用户 SID 字符串, 比如 S-1-5-21-... (OpenProcessToken + GetTokenInformation(TokenUser) + ConvertSidToStringSidW)."""
    libs = _libs()
    token = ctypes.c_void_p()
    if not libs.OpenProcessToken(libs.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise _error("OpenProcessToken")
    try:
        size = wintypes.DWORD()
        libs.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER or not size.value:
            raise _error("GetTokenInformation")
        buf = ctypes.create_string_buffer(size.value)
        if not libs.GetTokenInformation(token, _TOKEN_USER, buf, size, ctypes.byref(size)):
            raise _error("GetTokenInformation")
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value   # TOKEN_USER 的第一个字段就是 SID 指针
        text = ctypes.c_void_p()
        if not libs.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise _error("ConvertSidToStringSidW")
        return _wide_string(libs, text.value)
    finally:
        libs.CloseHandle(token)


def pipe_sddl(user_sid: str | None = None) -> str:
    """会话通道的访问控制 (SDDL): 受保护的 DACL (不继承别处的权限), 只有两条: 当前用户读写, SYSTEM 全部. 别人 (包括同一台电脑上的别的用户、
    Everyone、匿名) 连不上."""
    return f"D:P(A;;GRGW;;;{user_sid or current_user_sid()})(A;;GA;;;SY)"


def pipe_dacl_sddl(handle: int) -> str:
    """一个内核对象 (比如管道服务端句柄) 现在的 DACL, 写成 SDDL 字符串 (GetKernelObjectSecurity +
    ConvertSecurityDescriptorToStringSecurityDescriptorW). 只读; 测试拿它核对管道的访问控制.
    注意: 别的线程正堵在同一个 (同步) 句柄的调用上 (比如 ConnectNamedPipe) 时, 这里也会跟着排队等, 一直等下去; 要在那之前读."""
    libs = _libs()
    need = wintypes.DWORD()
    libs.GetKernelObjectSecurity(handle, DACL_SECURITY_INFORMATION, None, 0, ctypes.byref(need))
    if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER or not need.value:
        raise _error("GetKernelObjectSecurity")
    buf = ctypes.create_string_buffer(need.value)
    if not libs.GetKernelObjectSecurity(handle, DACL_SECURITY_INFORMATION, buf, need.value, ctypes.byref(need)):
        raise _error("GetKernelObjectSecurity")
    text = ctypes.c_void_p()
    if not libs.ConvertSecurityDescriptorToStringSecurityDescriptorW(buf, SDDL_REVISION_1, DACL_SECURITY_INFORMATION,
                                                                     ctypes.byref(text), None):
        raise _error("ConvertSecurityDescriptorToStringSecurityDescriptorW")
    return _wide_string(libs, text.value)


# ---- 命名管道: 服务端 -----------------------------------------------------------------------------------------------------

def _create_named_pipe(name: str, open_mode: int, pipe_mode: int, max_instances: int, out_size: int, in_size: int, timeout_ms: int,
                       attributes: _SecurityAttributes) -> int:
    """CreateNamedPipeW 本身 (单独拎出来, 测试用它核对传下去的标志). 失败抛 OSError."""
    handle = _libs().CreateNamedPipeW(name, open_mode, pipe_mode, max_instances, out_size, in_size, timeout_ms, ctypes.byref(attributes))
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise _error("CreateNamedPipeW")
    return handle


def pipe_server_create(name: str, *, sddl: str | None = None) -> int:
    """建会话通道的服务端 (CreateNamedPipeW + ConvertStringSecurityDescriptorToSecurityDescriptorW):

    - 双向字节流, 阻塞模式; 只建一个实例 (PIPE_MAX_INSTANCES = 1, 加 FILE_FLAG_FIRST_PIPE_INSTANCE: 这个名字上已经有实例就失败, 别人抢不了名字);
    - PIPE_REJECT_REMOTE_CLIENTS: 不接受远程连接;
    - 访问控制是 pipe_sddl() (默认: 只许当前用户和 SYSTEM).
    名字已经被占了抛 OSError (winerror 5). 返回服务端句柄 (用完 close_handle)."""
    libs = _libs()
    descriptor = ctypes.c_void_p()
    if not libs.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl or pipe_sddl(), SDDL_REVISION_1, ctypes.byref(descriptor), None):
        raise _error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor.value, False)
        return _create_named_pipe(name, PIPE_OPEN_MODE, PIPE_MODE, PIPE_MAX_INSTANCES, PIPE_BUFFER, PIPE_BUFFER, 0, attributes)
    finally:
        libs.LocalFree(descriptor.value)


def pipe_server_connect(handle: int) -> None:
    """等一个客户端连上来 (ConnectNamedPipe, 阻塞). 客户端在我们调用之前就连上了 (ERROR_PIPE_CONNECTED) 也算连上. 别的失败抛 OSError."""
    if _libs().ConnectNamedPipe(handle, None):
        return
    code = ctypes.get_last_error()
    if code == ERROR_PIPE_CONNECTED:
        return
    raise _error("ConnectNamedPipe", code)


def pipe_server_disconnect(handle: int) -> None:
    """断开当前这个客户端, 让同一个实例能接下一个 (DisconnectNamedPipe). 不抛异常."""
    _libs().DisconnectNamedPipe(handle)


def pipe_peek(handle: int) -> int:
    """管道里现在有几个字节可读 (PeekNamedPipe, 不取走). 对面已经断开又没有剩余数据: 抛 PipeClosed. 靠它做带超时的读, 不会卡死."""
    avail = wintypes.DWORD()
    if not _libs().PeekNamedPipe(handle, None, 0, None, ctypes.byref(avail), None):
        code = ctypes.get_last_error()
        if code in _CLOSED_PIPE_ERRORS:
            raise PipeClosed(f"PeekNamedPipe: 管道已断开（{code}）")
        raise _error("PeekNamedPipe", code)
    return avail.value


def pipe_read(handle: int, size: int) -> bytes:
    """读最多 size 个字节 (ReadFile; 调用方先用 pipe_peek 确认有数据, 不然会阻塞). 对面断开: 抛 PipeClosed."""
    buf = ctypes.create_string_buffer(size)
    got = wintypes.DWORD()
    if not _libs().ReadFile(handle, buf, size, ctypes.byref(got), None):
        code = ctypes.get_last_error()
        if code in _CLOSED_PIPE_ERRORS:
            raise PipeClosed(f"ReadFile: 管道已断开（{code}）")
        raise _error("ReadFile", code)
    return buf.raw[:got.value]


def pipe_write(handle: int, data: bytes) -> None:
    """把 data 全写进去 (WriteFile, 写不完接着写). 对面断开: 抛 PipeClosed."""
    libs = _libs()
    view = bytes(data)
    while view:
        written = wintypes.DWORD()
        if not libs.WriteFile(handle, view, len(view), ctypes.byref(written), None):
            code = ctypes.get_last_error()
            if code in _CLOSED_PIPE_ERRORS:
                raise PipeClosed(f"WriteFile: 管道已断开（{code}）")
            raise _error("WriteFile", code)
        view = view[written.value:]


def pipe_flush(handle: int) -> None:
    """等对面把写进去的数据都读走 (FlushFileBuffers; 对面不读也不断就会一直等, 所以只在最后一条应答之后用, 并且放在带超时的线程里).
    对面已经断开不算错."""
    if not _libs().FlushFileBuffers(handle):
        code = ctypes.get_last_error()
        if code not in _CLOSED_PIPE_ERRORS:
            raise _error("FlushFileBuffers", code)


# ---- 命名管道: 客户端 -----------------------------------------------------------------------------------------------------

def pipe_connect(name: str, timeout_s: float = 10.0) -> int:
    """连上一个命名管道 (CreateFileW 打开; 管道还没建出来 / 正忙就等, 忙的时候用 WaitNamedPipeW), 最多等 timeout_s 秒, 超了抛 TimeoutError.
    返回客户端句柄 (用完 close_handle)."""
    libs = _libs()
    deadline = time.monotonic() + timeout_s
    while True:
        handle = libs.CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
        if handle and handle != INVALID_HANDLE_VALUE:
            return handle
        code = ctypes.get_last_error()
        if code not in (ERROR_FILE_NOT_FOUND, ERROR_PIPE_BUSY):
            raise _error("CreateFileW", code)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{timeout_s:g} 秒内没连上管道 {name}")
        if code == ERROR_PIPE_BUSY:
            libs.WaitNamedPipeW(name, max(1, int(min(remaining, 0.5) * 1000)))
        else:
            time.sleep(min(0.02, remaining))


def pipe_server_pid(handle: int) -> int:
    """命名管道客户端这一端的句柄问: 对面服务端 (建这根管道的那个进程) 的进程号 (GetNamedPipeServerProcessId). 给网络插件的门面用: 看护方不答了就结束它.
    失败抛 OSError (比如句柄不是管道的客户端那一端)."""
    pid = wintypes.ULONG()
    if not _libs().GetNamedPipeServerProcessId(handle, ctypes.byref(pid)):
        raise _error("GetNamedPipeServerProcessId")
    return pid.value


def pipe_exists(name: str) -> bool:
    """这个名字的命名管道现在有没有 (WaitNamedPipeW 只等 1 毫秒: 不存在是 ERROR_FILE_NOT_FOUND; 存在但正忙是超时; 存在而空闲就成功).
    不连上、不占用实例; 比列出 \\\\.\\pipe\\ 里所有的管道快得多 (那个在装了很多软件的电脑上要大半秒)."""
    if _libs().WaitNamedPipeW(name, 1):
        return True
    return ctypes.get_last_error() != ERROR_FILE_NOT_FOUND


class PipeClient:
    """会话通道的客户端: 连上以后一问一答 (一行一个 JSON). 读应答带超时 (用 PeekNamedPipe 轮询, 不会卡死)."""

    def __init__(self, name: str, timeout_s: float = 10.0) -> None:
        self.name = name
        self.handle: int | None = pipe_connect(name, timeout_s)
        self._buf = bytearray()

    def send(self, data: bytes) -> None:
        if self.handle is None:
            raise PipeClosed("客户端已经关了")
        pipe_write(self.handle, data)

    def read_line(self, timeout_s: float = 10.0) -> bytes:
        """读一行 (不含换行). 超时抛 TimeoutError; 对面断开了又没读到整行抛 PipeClosed."""
        if self.handle is None:
            raise PipeClosed("客户端已经关了")
        deadline = time.monotonic() + timeout_s
        pause = 0.001
        while True:
            cut = self._buf.find(b"\n")
            if cut >= 0:
                line = bytes(self._buf[:cut])
                del self._buf[:cut + 1]
                return line
            avail = pipe_peek(self.handle)
            if avail:
                self._buf += pipe_read(self.handle, min(avail, PIPE_BUFFER))
                pause = 0.001
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{timeout_s:g} 秒内没等到应答")
            time.sleep(pause)
            pause = min(pause * 2, 0.02)

    def request(self, data: bytes, timeout_s: float = 10.0) -> bytes:
        self.send(data)
        return self.read_line(timeout_s)

    def server_pid(self) -> int:
        """对面服务端的进程号 (pipe_server_pid): 连上的这根管道是哪个进程建的."""
        if self.handle is None:
            raise PipeClosed("客户端已经关了")
        return pipe_server_pid(self.handle)

    def close(self) -> None:
        if self.handle is not None:
            close_handle(self.handle)
            self.handle = None

    def __enter__(self) -> PipeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def pipe_request(name: str, data: bytes, timeout_s: float = 10.0) -> bytes:
    """连上、发一行、收一行、断开 (整个过程最多约 timeout_s 秒来连、再 timeout_s 秒来等应答)."""
    with PipeClient(name, timeout_s) as client:
        return client.request(data, timeout_s)


# ---- 网卡和 DNS 缓存 --------------------------------------------------------------------------------------------------------

def adapter_names() -> list[str]:
    """这台电脑上所有网络接口的名字 (GetAdaptersAddresses, 只读; 带 GAA_FLAG_INCLUDE_ALL_INTERFACES, 没配 IP 的也在)."""
    libs = _libs()
    size = wintypes.ULONG(16384)
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value)
        code = libs.GetAdaptersAddresses(AF_UNSPEC, GAA_FLAGS, None, buf, ctypes.byref(size))
        if code == ERROR_BUFFER_OVERFLOW:
            continue
        if code != 0:
            raise _error("GetAdaptersAddresses", code)
        names: list[str] = []
        node = ctypes.cast(buf, ctypes.POINTER(_AdapterAddresses))
        while node:
            names.append(node.contents.FriendlyName or "")
            node = node.contents.Next
        return names
    raise OSError("GetAdaptersAddresses: 缓冲区一直不够")


def adapter_exists(alias: str) -> bool:
    """有没有叫 alias 的网卡 (按 adapter_names 比, 不分大小写). 只读, 不需要管理员权限; 查不了 (系统调用失败) 抛 OSError."""
    wanted = alias.casefold()
    return any(name.casefold() == wanted for name in adapter_names())


def _dns_flush_resolver_cache() -> bool:
    """清系统 DNS 缓存的那一个系统调用: dnsapi.dll 的 DnsFlushResolverCache (ipconfig /flushdns 用的就是它). **干跑这一步里不许调用**;
    测试把它换成假的 (见 tests/test_net_guardian.py)."""
    dns = ctypes.WinDLL("dnsapi", use_last_error=True)
    dns.DnsFlushResolverCache.restype = wintypes.BOOL
    dns.DnsFlushResolverCache.argtypes = []
    return bool(dns.DnsFlushResolverCache())


def flush_dns() -> bool:
    """清系统 DNS 缓存 (DnsFlushResolverCache). 成功返回 True; 系统不让 (比如没权限)、DLL 加载不了都返回 False, 不抛异常.
    普通权限能不能清, 契约第 12 节还没验证."""
    try:
        return bool(_dns_flush_resolver_cache())
    except (OSError, AttributeError):
        return False


# ---- 关机、注销 -----------------------------------------------------------------------------------------------------------

_KEEP_ALIVE: list[object] = []     # 回调对象要一直活着, 不然被回收了系统再调就崩


def set_console_ctrl_handler(callback: object) -> object:
    """登记控制台事件的回调 (SetConsoleCtrlHandler): callback(事件号) -> bool, 返回 True 表示这个事件我处理了. 事件号见 CTRL_* 常量:
    关机 (CTRL_SHUTDOWN_EVENT)、注销 (CTRL_LOGOFF_EVENT)、控制台窗口被关 (CTRL_CLOSE_EVENT)、Ctrl+C、Ctrl+Break. 回调在系统另起的线程里被调用.
    返回登记好的回调对象 (调用方不用管; 模块自己也留了一份引用, 不会被回收)."""
    libs = _libs()
    handler = libs.handler_type(lambda event: bool(callback(int(event))))
    if not libs.SetConsoleCtrlHandler(handler, True):
        raise _error("SetConsoleCtrlHandler")
    _KEEP_ALIVE.append(handler)
    return handler
