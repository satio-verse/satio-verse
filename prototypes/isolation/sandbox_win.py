"""把进程关进沙箱的那一层 (2026-11-14 隔离原型, 插件宿主方案第六节第 6 步).

全是 ctypes 调 Windows 接口, 只用标准库, 不导入 wm (原型要能脱离项目的包单独跑). 两种关法:
- 应用容器 (AppContainer): 一个只按名字算出来的 SID, 不给任何能力 (SECURITY_CAPABILITIES 的能力数是 0);
- 受限的令牌: 从自己的令牌削出来 -- 去掉特权, 管理员组只用于拒绝, 完整性级别设成低; 可以再加限制 SID.
外加: 作业对象 (句柄全关掉时结束里面所有进程, 不许脱离)、只继承标准输入输出三个句柄、给目录加权限、收尾要用的查询.

"会改东西"的函数 -- spawn / set_dacl / set_low_label / protect_user_only / make_restricted_token /
appcontainer_register / appcontainer_unregister / open_as_token (诊断: 临时线程换成某个令牌的身份只读打开几个文件, 立刻换回) --
只在用户双击 run_me.cmd 时才会被调到. 对话里和测试里一律不调:
tests/conftest.py 把它们全换成"一调用就失败". 这个文件里没有任何办法绕开"自己是管理员就不干"的自查,
也没有任何把权限降下来再起进程的办法; 它做的只是: 在已经是普通权限的进程里, 把子进程的权限再削小.

纯函数 (SID 转换、环境块、命令行) 和结构体布局不碰系统, 测试里可以直接验.
"""
from __future__ import annotations

import ctypes
import functools
import json
import os
import queue
import re
import subprocess
import threading
import time
import types
from ctypes import wintypes

# ---------------------------------------------------------------- 常量
# 文件访问掩码 (和资源管理器里"读取和执行""修改""完全控制"对应)
FILE_GENERIC_READ = 0x00120089
FILE_GENERIC_WRITE = 0x00120116
FILE_GENERIC_EXECUTE = 0x001200A0
DELETE = 0x00010000
FILE_ALL_ACCESS = 0x001F01FF
ACCESS_READ_EXEC = FILE_GENERIC_READ | FILE_GENERIC_EXECUTE  # 0x1200A9
ACCESS_MODIFY = FILE_GENERIC_READ | FILE_GENERIC_WRITE | FILE_GENERIC_EXECUTE | DELETE  # 0x1301BF
OBJECT_INHERIT_ACE = 0x1
CONTAINER_INHERIT_ACE = 0x2
INHERIT_BOTH = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE

SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x4
LABEL_SECURITY_INFORMATION = 0x10
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SDDL_REVISION_1 = 1
GRANT_ACCESS = 1
TRUSTEE_IS_SID = 0
TRUSTEE_IS_UNKNOWN = 0

# 令牌
TOKEN_ASSIGN_PRIMARY = 0x1
TOKEN_DUPLICATE = 0x2
TOKEN_QUERY = 0x8
TOKEN_ADJUST_DEFAULT = 0x80
DISABLE_MAX_PRIVILEGE = 0x1
SE_GROUP_INTEGRITY = 0x20
TOKEN_USER_CLASS = 1
TOKEN_GROUPS_CLASS = 2
TOKEN_DEFAULT_DACL_CLASS = 6
GENERIC_ALL = 0x10000000
TOKEN_ELEVATION_TYPE_CLASS = 18
TOKEN_ELEVATION_CLASS = 20
TOKEN_INTEGRITY_LEVEL_CLASS = 25
TOKEN_LOGON_SID_CLASS = 28
TOKEN_IS_APPCONTAINER_CLASS = 29

# 进程
CREATE_SUSPENDED = 0x4
DETACHED_PROCESS = 0x8
CREATE_UNICODE_ENVIRONMENT = 0x400
EXTENDED_STARTUPINFO_PRESENT = 0x80000
STARTF_USESHOWWINDOW = 0x1
STARTF_USESTDHANDLES = 0x100
HANDLE_FLAG_INHERIT = 0x1
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
SEM_FAILCRITICALERRORS = 0x1
SEM_NOGPFAULTERRORBOX = 0x2
SEM_NOOPENFILEERRORBOX = 0x8000
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_OBJECT_0 = 0
STILL_ACTIVE = 259
ERROR_INVALID_PARAMETER = 87
ERROR_ACCESS_DENIED = 5

# 作业对象
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

# 常用 SID (字符串形式; 要用时转成字节)
SID_ADMINS = "S-1-5-32-544"
SID_USERS = "S-1-5-32-545"
SID_EVERYONE = "S-1-1-0"
SID_SYSTEM = "S-1-5-18"
SID_RESTRICTED = "S-1-5-12"
SID_ALL_APP_PACKAGES = "S-1-15-2-1"
SID_LOW = "S-1-16-4096"

# 应用容器的名字: 1~64 个字符, 只许字母数字和 - _ .
APPCONTAINER_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
HRESULT_ALREADY_EXISTS = 0x800700B7
HRESULT_NOT_FOUND = 0x80070490


class SandboxError(Exception):
    """某个系统接口失败. api 是接口名, code 是错误码 (Win32 错误码, 或者 HRESULT 的无符号值)."""

    def __init__(self, api: str, code: int, note: str = ""):
        super().__init__(api, code, note)
        self.api = api
        self.code = code
        self.note = note

    def __str__(self) -> str:
        shown = f"{self.code}" if self.code < 0x10000 else f"{self.code} (0x{self.code:08X})"
        return f"{self.api} 失败, 错误码 {shown}" + (f" ({self.note})" if self.note else "")


# ---------------------------------------------------------------- 纯函数 (不碰系统)
def sid_from_string(text: str) -> bytes:
    """"S-1-5-32-544" 这种写法转成 SID 的二进制 (版本 1, 子权限个数, 6 字节机构号, 每个子权限 4 字节小端)."""
    m = re.fullmatch(r"S-1-(\d+)((?:-\d+)+)", text.strip().upper())
    if not m:
        raise ValueError(f"不是 SID 的写法: {text!r}")
    authority = int(m.group(1))
    subs = [int(x) for x in m.group(2).split("-")[1:]]
    if authority >= 1 << 48 or not 1 <= len(subs) <= 15 or any(s >= 1 << 32 for s in subs):
        raise ValueError(f"SID 的数值越界: {text!r}")
    return bytes([1, len(subs)]) + authority.to_bytes(6, "big") + b"".join(s.to_bytes(4, "little") for s in subs)


def sid_to_string(sid: bytes) -> str:
    """sid_from_string 的反过来."""
    if len(sid) < 12 or sid[0] != 1 or len(sid) != 8 + 4 * sid[1]:
        raise ValueError("不是一个完整的 SID")
    authority = int.from_bytes(sid[2:8], "big")
    subs = [int.from_bytes(sid[8 + 4 * i: 12 + 4 * i], "little") for i in range(sid[1])]
    return f"S-1-{authority}" + "".join(f"-{s}" for s in subs)


def build_env_block(env: dict[str, str]) -> str:
    """CreateProcess 要的环境块: "名=值\\0" 一条接一条, 按名字不分大小写排序, 末尾再加一个 \\0."""
    for key, value in env.items():
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError(f"环境变量不合规: {key!r}")
    items = sorted(env.items(), key=lambda kv: kv[0].upper())
    body = "".join(f"{k}={v}\0" for k, v in items)
    return (body or "\0") + "\0"


def build_command_line(exe: str, args: list[str]) -> str:
    """按 C 运行库的规矩拼命令行 (含空格或引号的参数加引号). 探针的命令行就这样拼, 不经过任何 shell."""
    return subprocess.list2cmdline([exe, *args])


def sandbox_token_sid(name: str) -> bytes:
    """受限令牌用的"沙箱 SID": 按名字算出来的一个编出来的本地账号样子的 SID (S-1-5-21-a-b-c-1337, 不对应任何真账号).
    11-16 真机上两种限制 SID 配法 CreateRestrictedToken 都报 87, 它们共有的就是应用容器的 SID (S-1-15-2-...);
    怀疑系统不收它当限制 SID (没证实, 结果里有逐个 SID 单独试的诊断), 所以令牌这条线改用这个."""
    import hashlib
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    parts = [int.from_bytes(digest[i:i + 4], "little") for i in (0, 4, 8)]
    return sid_from_string("S-1-5-21-" + "-".join(str(p) for p in parts) + "-1337")


def valid_appcontainer_name(name: str) -> bool:
    return APPCONTAINER_NAME_RE.fullmatch(name) is not None


# ---------------------------------------------------------------- 结构体 (布局和 Windows 头文件一致, 测试里核大小)
class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wintypes.BOOL)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TOKEN_GROUPS(ctypes.Structure):
    _fields_ = [("GroupCount", wintypes.DWORD), ("Groups", SID_AND_ATTRIBUTES * 1)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


class TOKEN_DEFAULT_DACL(ctypes.Structure):
    _fields_ = [("DefaultDacl", ctypes.c_void_p)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [("AppContainerSid", ctypes.c_void_p), ("Capabilities", ctypes.c_void_p),
                ("CapabilityCount", wintypes.DWORD), ("Reserved", wintypes.DWORD)]


class TRUSTEE_W(ctypes.Structure):
    _fields_ = [("pMultipleTrustee", ctypes.c_void_p), ("MultipleTrusteeOperation", ctypes.c_int),
                ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int), ("ptstrName", ctypes.c_void_p)]


class EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [("grfAccessPermissions", wintypes.DWORD), ("grfAccessMode", ctypes.c_int),
                ("grfInheritance", wintypes.DWORD), ("Trustee", TRUSTEE_W)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong), ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong), ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong), ("OtherTransferCount", ctypes.c_ulonglong)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


# ---------------------------------------------------------------- 系统库 (只载入、设参数类型, 不调任何接口)
@functools.lru_cache(maxsize=1)
def _api() -> types.SimpleNamespace:
    """用新的库对象设参数类型, 不去改 ctypes.windll 那份全进程共用的 (和 wm/privilege.py 同样的做法)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    uenv = ctypes.WinDLL("userenv", use_last_error=True)
    h, d, b, p = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p
    cw, w = wintypes.LPCWSTR, wintypes.LPWSTR
    ph, pd, pp = ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_void_p)
    pi_ptr = ctypes.POINTER(PROCESS_INFORMATION)
    st = ctypes.c_size_t

    def decl(dll, name, restype, *argtypes):
        fn = getattr(dll, name)
        fn.restype = restype
        fn.argtypes = list(argtypes)
        return fn

    a = types.SimpleNamespace()
    # kernel32
    a.GetCurrentProcess = decl(k32, "GetCurrentProcess", h)
    a.CloseHandle = decl(k32, "CloseHandle", b, h)
    a.CreatePipe = decl(k32, "CreatePipe", b, ph, ph, ctypes.POINTER(SECURITY_ATTRIBUTES), d)
    a.SetHandleInformation = decl(k32, "SetHandleInformation", b, h, d, d)
    a.InitializeProcThreadAttributeList = decl(k32, "InitializeProcThreadAttributeList", b, p, d, d,
                                               ctypes.POINTER(st))
    a.UpdateProcThreadAttribute = decl(k32, "UpdateProcThreadAttribute", b, p, d, st, p, st, p, p)
    a.DeleteProcThreadAttributeList = decl(k32, "DeleteProcThreadAttributeList", None, p)
    a.CreateProcessW = decl(k32, "CreateProcessW", b, cw, w, p, p, b, d, p, cw, p, pi_ptr)
    a.ResumeThread = decl(k32, "ResumeThread", d, h)
    a.WaitForSingleObject = decl(k32, "WaitForSingleObject", d, h, d)
    a.GetExitCodeProcess = decl(k32, "GetExitCodeProcess", b, h, pd)
    a.TerminateProcess = decl(k32, "TerminateProcess", b, h, wintypes.UINT)
    a.OpenProcess = decl(k32, "OpenProcess", h, d, b, d)
    a.SetErrorMode = decl(k32, "SetErrorMode", wintypes.UINT, wintypes.UINT)
    a.CreateJobObjectW = decl(k32, "CreateJobObjectW", h, p, cw)
    a.SetInformationJobObject = decl(k32, "SetInformationJobObject", b, h, ctypes.c_int, p, d)
    a.QueryInformationJobObject = decl(k32, "QueryInformationJobObject", b, h, ctypes.c_int, p, d, pd)
    a.AssignProcessToJobObject = decl(k32, "AssignProcessToJobObject", b, h, h)
    a.TerminateJobObject = decl(k32, "TerminateJobObject", b, h, wintypes.UINT)
    a.LocalFree = decl(k32, "LocalFree", p, p)
    a.GetCurrentThread = decl(k32, "GetCurrentThread", h)
    a.GetFileAttributesW = decl(k32, "GetFileAttributesW", d, cw)
    a.CreateFileW = decl(k32, "CreateFileW", h, cw, d, d, p, d, d, h)
    # advapi32
    a.OpenProcessToken = decl(adv, "OpenProcessToken", b, h, d, ph)
    a.GetTokenInformation = decl(adv, "GetTokenInformation", b, h, ctypes.c_int, p, d, pd)
    a.SetTokenInformation = decl(adv, "SetTokenInformation", b, h, ctypes.c_int, p, d)
    a.CreateRestrictedToken = decl(adv, "CreateRestrictedToken", b, h, d, d, p, d, p, d, p, ph)
    a.CreateProcessAsUserW = decl(adv, "CreateProcessAsUserW", b, h, cw, w, p, p, b, d, p, cw, p, pi_ptr)
    a.GetNamedSecurityInfoW = decl(adv, "GetNamedSecurityInfoW", d, cw, ctypes.c_int, d, pp, pp, pp, pp, pp)
    a.SetNamedSecurityInfoW = decl(adv, "SetNamedSecurityInfoW", d, w, ctypes.c_int, d, p, p, p, p)
    a.SetEntriesInAclW = decl(adv, "SetEntriesInAclW", d, wintypes.ULONG, p, p, pp)
    a.ConvertStringSecurityDescriptorToSecurityDescriptorW = decl(
        adv, "ConvertStringSecurityDescriptorToSecurityDescriptorW", b, cw, d, pp, pd)
    a.ConvertSecurityDescriptorToStringSecurityDescriptorW = decl(
        adv, "ConvertSecurityDescriptorToStringSecurityDescriptorW", b, p, d, d, pp, pd)
    a.GetSecurityDescriptorSacl = decl(adv, "GetSecurityDescriptorSacl", b, p, ctypes.POINTER(b), pp,
                                       ctypes.POINTER(b))
    a.FreeSid = decl(adv, "FreeSid", p, p)
    a.ImpersonateLoggedOnUser = decl(adv, "ImpersonateLoggedOnUser", b, h)
    a.RevertToSelf = decl(adv, "RevertToSelf", b)
    a.OpenThreadToken = decl(adv, "OpenThreadToken", b, h, d, b, ph)
    # userenv (应用容器)
    a.CreateAppContainerProfile = decl(uenv, "CreateAppContainerProfile", ctypes.c_long, cw, cw, cw, p, d, pp)
    a.DeriveAppContainerSidFromAppContainerName = decl(uenv, "DeriveAppContainerSidFromAppContainerName",
                                                       ctypes.c_long, cw, pp)
    a.DeleteAppContainerProfile = decl(uenv, "DeleteAppContainerProfile", ctypes.c_long, cw)
    return a


def _fail(api: str) -> SandboxError:
    return SandboxError(api, ctypes.get_last_error())


def _read_sid(ptr: int) -> bytes:
    """把系统里一个 SID 拷成字节 (第 2 个字节是子权限个数, 总长 8 + 4 * 个数)."""
    count = ctypes.string_at(ptr + 1, 1)[0]
    return ctypes.string_at(ptr, 8 + 4 * count)


def _sid_buffer(sid: bytes):
    return (ctypes.c_ubyte * len(sid)).from_buffer_copy(sid)


# ---------------------------------------------------------------- 自己是不是管理员 (和 wm/privilege.py 等价, 不导入它)
def _token_elevated() -> bool | None:
    """当前进程令牌的 TokenIsElevated; 系统调用失败是 None."""
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            elevation, size = wintypes.DWORD(), wintypes.DWORD()
            if not advapi32.GetTokenInformation(token, TOKEN_ELEVATION_CLASS, ctypes.byref(elevation),
                                                ctypes.sizeof(elevation), ctypes.byref(size)):
                return None
            return bool(elevation.value)
        finally:
            kernel32.CloseHandle(token)
    except (OSError, AttributeError, ValueError):
        return None


def _shell_is_admin() -> bool | None:
    """老办法 (shell32.IsUserAnAdmin), 令牌那条路走不通时的后备."""
    try:
        return bool(ctypes.WinDLL("shell32").IsUserAnAdmin())
    except (OSError, AttributeError):
        return None


def is_elevated() -> bool | None:
    """True: 以管理员身份在跑 (令牌已提权); False: 不是; None: 查不出来. 不抛异常.
    调用方 (measure.py) 把"True"和"None"都当成"不干".
    """
    if os.name != "nt":
        return None
    try:
        got = _token_elevated()
        return got if got is not None else _shell_is_admin()
    except Exception:  # noqa: BLE001 - 自查不管出什么意外都不能漏出去, 漏出去等于没查
        return None


# ---------------------------------------------------------------- 令牌
def _open_own_token(access: int) -> int:
    a = _api()
    token = wintypes.HANDLE()
    if not a.OpenProcessToken(a.GetCurrentProcess(), access, ctypes.byref(token)):
        raise _fail("OpenProcessToken")
    return token.value


def _token_buffer(token: int, info_class: int):
    """GetTokenInformation 的两步 (先问长度再取), 返回装着结果的缓冲区. 里面的指针只在这个缓冲区活着时有效."""
    a = _api()
    need = wintypes.DWORD(0)
    a.GetTokenInformation(token, info_class, None, 0, ctypes.byref(need))  # 第一次一定失败, 只为取长度
    if need.value == 0:
        raise _fail("GetTokenInformation")
    buf = ctypes.create_string_buffer(need.value)
    if not a.GetTokenInformation(token, info_class, buf, need.value, ctypes.byref(need)):
        raise _fail("GetTokenInformation")
    return buf


def _token_groups(token: int, info_class: int = TOKEN_GROUPS_CLASS) -> list[tuple[bytes, int]]:
    buf = _token_buffer(token, info_class)
    count = wintypes.DWORD.from_buffer(buf).value
    array = (SID_AND_ATTRIBUTES * count).from_address(ctypes.addressof(buf) + TOKEN_GROUPS.Groups.offset)
    return [(_read_sid(item.Sid), item.Attributes) for item in array]


def current_user_sid() -> bytes:
    """当前用户的 SID (只在内存里用, 不写进任何结果)."""
    token = _open_own_token(TOKEN_QUERY)
    try:
        buf = _token_buffer(token, TOKEN_USER_CLASS)
        return _read_sid(ctypes.c_void_p.from_buffer(buf).value)
    finally:
        _api().CloseHandle(token)


def current_logon_sid() -> bytes | None:
    """这次登录的登录 SID; 取不到是 None."""
    try:
        token = _open_own_token(TOKEN_QUERY)
    except SandboxError:
        return None
    try:
        groups = _token_groups(token, TOKEN_LOGON_SID_CLASS)
        return groups[0][0] if groups else None
    except SandboxError:
        return None
    finally:
        _api().CloseHandle(token)


def own_integrity_rid() -> int | None:
    """自己令牌的完整性级别 (0x1000 低, 0x2000 中, 0x3000 高, 0x4000 系统); 取不到是 None."""
    try:
        token = _open_own_token(TOKEN_QUERY)
    except SandboxError:
        return None
    try:
        buf = _token_buffer(token, TOKEN_INTEGRITY_LEVEL_CLASS)
        sid = _read_sid(ctypes.c_void_p.from_buffer(buf).value)
        return int.from_bytes(sid[-4:], "little")  # 最后一个子权限就是级别
    except SandboxError:
        return None
    finally:
        _api().CloseHandle(token)


def _sid_attr_array(sids: list[bytes], keep: list):
    """SID_AND_ATTRIBUTES 数组 (给 CreateRestrictedToken 用); 缓冲区放进 keep 保活. 空列表给 None."""
    if not sids:
        return None
    array = (SID_AND_ATTRIBUTES * len(sids))()
    for i, sid in enumerate(sids):
        buf = _sid_buffer(sid)
        keep.append(buf)
        array[i].Sid = ctypes.addressof(buf)
        array[i].Attributes = 0
    keep.append(array)
    return array


def make_restricted_token(*, disable: list[bytes] = (), restrict: list[bytes] = (), low_integrity: bool = True,
                          default_dacl_sids: list[bytes] = (), notes: list[str] | None = None,
                          flags: int = DISABLE_MAX_PRIVILEGE) -> int:
    """从自己的令牌削出一个受限的主令牌, 返回句柄 (用完交给 close_handle).
    - 去掉所有特权 (只留"绕过遍历检查"); disable 里的组改成"只用于拒绝" (令牌里没有的组直接跳过);
    - restrict 非空: 加上这些限制 SID (访问检查要连着再过一遍, 第二遍只认这些 SID);
    - low_integrity: 完整性级别设成低;
    - flags: CreateRestrictedToken 的标志位, 默认去掉特权; 诊断配法传 0 (不去特权, 令牌几乎和自己的一样);
    - default_dacl_sids: 把这些 SID 加进令牌的默认 DACL (起来的进程自己建的对象、它自己的进程对象才过得了第二遍检查;
      这一步尽力而为, 失败不算错, 原因放进 notes).
    不提权: 削出来的令牌是自己令牌的子集, 起进程不需要额外特权.
    """
    a = _api()
    base = _open_own_token(TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY | TOKEN_QUERY | TOKEN_ADJUST_DEFAULT)
    new = wintypes.HANDLE()
    try:
        present = {sid for sid, _ in _token_groups(base)}
        to_disable = [s for s in disable if s in present]
        keep: list = []
        dis_arr = _sid_attr_array(to_disable, keep)
        res_arr = _sid_attr_array(list(restrict), keep)
        if not a.CreateRestrictedToken(base, flags, len(to_disable), dis_arr, 0, None,
                                       len(restrict), res_arr, ctypes.byref(new)):
            raise _fail("CreateRestrictedToken")
        try:
            if low_integrity:
                _set_integrity_low(new.value)
            if default_dacl_sids:
                try:
                    _grant_in_token_default_dacl(new.value, list(default_dacl_sids))
                except SandboxError as exc:
                    if notes is not None:
                        notes.append(str(exc))
        except BaseException:
            a.CloseHandle(new.value)
            raise
        return new.value
    finally:
        a.CloseHandle(base)


def _grant_in_token_default_dacl(token: int, sids: list[bytes]) -> None:
    """把 sids 加进令牌的默认 DACL (并进现有的, 授予全部访问)."""
    a = _api()
    buf = _token_buffer(token, TOKEN_DEFAULT_DACL_CLASS)
    old = ctypes.c_void_p.from_buffer(buf).value  # TOKEN_DEFAULT_DACL 的第一个字段就是 ACL 指针 (指在 buf 里, 可能是空)
    keep: list = []
    access = _explicit_access([(sid, GENERIC_ALL, 0) for sid in sids], keep)
    new = ctypes.c_void_p()
    rc = a.SetEntriesInAclW(len(sids), access, old, ctypes.byref(new))
    if rc:
        raise SandboxError("SetEntriesInAclW(TokenDefaultDacl)", rc)
    try:
        holder = TOKEN_DEFAULT_DACL(new.value)
        if not a.SetTokenInformation(token, TOKEN_DEFAULT_DACL_CLASS, ctypes.byref(holder), ctypes.sizeof(holder)):
            raise _fail("SetTokenInformation(TokenDefaultDacl)")
    finally:
        a.LocalFree(new)


def _set_integrity_low(token: int) -> None:
    a = _api()
    sid = _sid_buffer(sid_from_string(SID_LOW))
    label = TOKEN_MANDATORY_LABEL()
    label.Label.Sid = ctypes.addressof(sid)
    label.Label.Attributes = SE_GROUP_INTEGRITY
    # 长度要把 SID 本身算进去 (微软示例就这么传)
    if not a.SetTokenInformation(token, TOKEN_INTEGRITY_LEVEL_CLASS, ctypes.byref(label),
                                 ctypes.sizeof(label) + len(sid)):
        raise _fail("SetTokenInformation(TokenIntegrityLevel)")


def _token_dword(token: int, info_class: int) -> int | None:
    a = _api()
    value, size = wintypes.DWORD(), wintypes.DWORD()
    if not a.GetTokenInformation(token, info_class, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size)):
        return None
    return value.value


def open_as_token(token: int, paths: list[str]) -> dict:
    """诊断用 (只在某个令牌的进程创建了却没回话时调一次): 开一个临时线程, 把线程换成这个令牌的身份, 对每个路径各查一次属性、
    各只读 (读加执行) 打开一次, 只记错误码 (0 = 成了), 然后换回来, 线程退出. 不改任何文件和权限.
    先核对线程令牌真的到了"模拟"级别 (类型是模拟令牌、级别 >= 2), 没到就不去开, 记 error, 免得记出一串假的"打不开".
    返回 {impersonated, level, error, results: {路径: [属性错误码, 打开错误码]}}.
    """
    out: dict = {"impersonated": False, "level": None, "error": None, "results": {}}
    bad_handles = (None, 0, ctypes.c_void_p(-1).value)

    def work() -> None:
        a = _api()
        if not a.ImpersonateLoggedOnUser(token):
            out["error"] = f"ImpersonateLoggedOnUser 失败, 错误码 {ctypes.get_last_error()}"
            return
        try:
            thread_token = wintypes.HANDLE()
            if not a.OpenThreadToken(a.GetCurrentThread(), TOKEN_QUERY, True, ctypes.byref(thread_token)):
                out["error"] = f"OpenThreadToken 失败, 错误码 {ctypes.get_last_error()}"
                return
            try:
                kind, level = _token_dword(thread_token.value, 8), _token_dword(thread_token.value, 9)  # TokenType、TokenImpersonationLevel
            finally:
                a.CloseHandle(thread_token.value)
            out["level"] = level
            if kind != 2 or level is None or level < 2:
                out["error"] = f"线程令牌没到模拟级别 (类型 {kind}, 级别 {level}), 诊断没做成"
                return
            out["impersonated"] = True
            for path in paths:
                attrs = a.GetFileAttributesW(path)
                attr_err = 0 if attrs != 0xFFFFFFFF else ctypes.get_last_error()
                handle = a.CreateFileW(path, ACCESS_READ_EXEC, 7, None, 3, 0x02000000, None)  # 共享全开、OPEN_EXISTING、目录也能开
                if handle in bad_handles:
                    open_err = ctypes.get_last_error()
                else:
                    open_err = 0
                    a.CloseHandle(handle)
                out["results"][path] = [attr_err, open_err]
        finally:
            a.RevertToSelf()

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(15)
    if thread.is_alive():
        out["error"] = "诊断线程 15 秒没做完"
    return out


def close_handle(handle: int | None) -> None:
    if handle:
        _api().CloseHandle(handle)


# ---------------------------------------------------------------- 应用容器
def appcontainer_sid(name: str) -> bytes:
    """只按名字算出应用容器的 SID, 不登记 (不写注册表, 不建文件夹)."""
    if not valid_appcontainer_name(name):
        raise ValueError(f"应用容器的名字不合规: {name!r}")
    a = _api()
    psid = ctypes.c_void_p()
    hr = a.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(psid))
    if hr < 0:
        raise SandboxError("DeriveAppContainerSidFromAppContainerName", hr & 0xFFFFFFFF)
    try:
        return _read_sid(psid.value)
    finally:
        a.FreeSid(psid)


def appcontainer_register(name: str, display: str, description: str) -> tuple[bytes, bool]:
    """登记应用容器 (会在当前用户的注册表里写一项, 在 AppData\\Local\\Packages 下建文件夹).
    返回 (SID, 是不是这次新建的); 名字已经登记过 (上次没收尾) 不算失败, 返回 (SID, False).
    """
    if not valid_appcontainer_name(name):
        raise ValueError(f"应用容器的名字不合规: {name!r}")
    a = _api()
    psid = ctypes.c_void_p()
    hr = a.CreateAppContainerProfile(name, display, description, None, 0, ctypes.byref(psid))
    if hr < 0:
        if hr & 0xFFFFFFFF == HRESULT_ALREADY_EXISTS:
            return appcontainer_sid(name), False
        raise SandboxError("CreateAppContainerProfile", hr & 0xFFFFFFFF)
    try:
        return _read_sid(psid.value), True
    finally:
        a.FreeSid(psid)


def appcontainer_unregister(name: str) -> int:
    """删掉登记的应用容器; 返回 0 (删了, 或者本来就没有) 或 HRESULT 的无符号值 (没删成)."""
    hr = _api().DeleteAppContainerProfile(name) & 0xFFFFFFFF
    return 0 if hr in (0, HRESULT_NOT_FOUND) else hr


def appcontainer_registered(sid: bytes) -> bool | None:
    """只读: 当前用户的注册表里有没有这个应用容器的登记项. True / False / None (查不出来)."""
    try:
        import winreg
        path = (r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppContainer"
                r"\Mappings" + "\\" + sid_to_string(sid))
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ):
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


# ---------------------------------------------------------------- 目录权限
def _explicit_access(entries: list[tuple[bytes, int, int]], keep: list):
    """(SID, 访问掩码, 继承标志) 的列表 -> EXPLICIT_ACCESS_W 数组 (都是"授予"); SID 缓冲区放进 keep 保活."""
    access = (EXPLICIT_ACCESS_W * len(entries))()
    for i, (sid, mask, inherit) in enumerate(entries):
        buf = _sid_buffer(sid)
        keep.append(buf)
        access[i].grfAccessPermissions = mask
        access[i].grfAccessMode = GRANT_ACCESS
        access[i].grfInheritance = inherit
        access[i].Trustee.TrusteeForm = TRUSTEE_IS_SID
        access[i].Trustee.TrusteeType = TRUSTEE_IS_UNKNOWN
        access[i].Trustee.ptstrName = ctypes.addressof(buf)
    keep.append(access)
    return access


def set_dacl(path: str, entries: list[tuple[bytes, int, int]], *, merge: bool = True,
             protected: bool = False) -> None:
    """给目录加访问控制项. entries 是 (SID, 访问掩码, 继承标志) 的列表.
    merge=True: 并进现有的 DACL (别的项原样保留); merge=False: 只留 entries 里这些.
    protected=True: 不再继承上一级的权限. 加了可继承的项以后, 系统会把它推给目录里已有的内容.
    """
    a = _api()
    old = ctypes.c_void_p()
    sd = ctypes.c_void_p()
    if merge:
        rc = a.GetNamedSecurityInfoW(path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None,
                                     ctypes.byref(old), None, ctypes.byref(sd))
        if rc:
            raise SandboxError("GetNamedSecurityInfoW", rc)
    try:
        keep: list = []
        access = _explicit_access(entries, keep)
        new = ctypes.c_void_p()
        rc = a.SetEntriesInAclW(len(entries), access, old if merge else None, ctypes.byref(new))
        if rc:
            raise SandboxError("SetEntriesInAclW", rc)
        try:
            flags = DACL_SECURITY_INFORMATION | (PROTECTED_DACL_SECURITY_INFORMATION if protected else 0)
            rc = a.SetNamedSecurityInfoW(path, SE_FILE_OBJECT, flags, None, None, new, None)
            if rc:
                raise SandboxError("SetNamedSecurityInfoW(DACL)", rc)
        finally:
            a.LocalFree(new)
    finally:
        if sd.value:
            a.LocalFree(sd)


def protect_user_only(path: str, user_sid: bytes) -> None:
    """让目录的权限和家目录下的默认一样: 只有本人、SYSTEM、Administrators, 不继承上一级 (假保管箱用)."""
    set_dacl(path, [(user_sid, FILE_ALL_ACCESS, INHERIT_BOTH),
                    (sid_from_string(SID_SYSTEM), FILE_ALL_ACCESS, INHERIT_BOTH),
                    (sid_from_string(SID_ADMINS), FILE_ALL_ACCESS, INHERIT_BOTH)],
             merge=False, protected=True)


def set_low_label(path: str) -> None:
    """给目录打上"低"完整性标签 (带继承): 低完整性的进程才写得进去. 应用容器和低完整性的受限令牌都是低完整性,
    没有这个标签它们连自己的文件夹都写不进. 只打在沙箱自己的文件夹上, 别的目录不动."""
    a = _api()
    sd = ctypes.c_void_p()
    if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW("S:(ML;OICI;NW;;;LW)", SDDL_REVISION_1,
                                                                  ctypes.byref(sd), None):
        raise _fail("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        sacl = ctypes.c_void_p()
        if not a.GetSecurityDescriptorSacl(sd, ctypes.byref(present), ctypes.byref(sacl), ctypes.byref(defaulted)):
            raise _fail("GetSecurityDescriptorSacl")
        rc = a.SetNamedSecurityInfoW(path, SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION, None, None, None, sacl)
        if rc:
            raise SandboxError("SetNamedSecurityInfoW(LABEL)", rc)
    finally:
        a.LocalFree(sd)


def dacl_sddl(path: str) -> str:
    """只读: 目录的 DACL 写成 SDDL 字符串 (诊断用; 调用方负责抹掉里面本人的 SID)."""
    a = _api()
    sd = ctypes.c_void_p()
    rc = a.GetNamedSecurityInfoW(path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None, None, None,
                                 ctypes.byref(sd))
    if rc:
        raise SandboxError("GetNamedSecurityInfoW", rc)
    try:
        text = ctypes.c_void_p()
        if not a.ConvertSecurityDescriptorToStringSecurityDescriptorW(sd, SDDL_REVISION_1,
                                                                      DACL_SECURITY_INFORMATION,
                                                                      ctypes.byref(text), None):
            raise _fail("ConvertSecurityDescriptorToStringSecurityDescriptorW")
        try:
            return ctypes.wstring_at(text.value)
        finally:
            a.LocalFree(text)
    finally:
        a.LocalFree(sd)


# ---------------------------------------------------------------- 作业对象
class Job:
    """作业对象: 句柄全关掉时结束里面所有进程; 不设"允许脱离", 里面的进程脱离不了."""

    def __init__(self) -> None:
        a = _api()
        handle = a.CreateJobObjectW(None, None)
        if not handle:
            raise _fail("CreateJobObjectW")
        self.handle: int | None = handle
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not a.SetInformationJobObject(handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
                                         ctypes.sizeof(info)):
            err = _fail("SetInformationJobObject")
            a.CloseHandle(handle)
            self.handle = None
            raise err

    def assign(self, process_handle: int) -> None:
        if not _api().AssignProcessToJobObject(self.handle, process_handle):
            raise _fail("AssignProcessToJobObject")

    def terminate(self, code: int = 1) -> None:
        if self.handle:
            _api().TerminateJobObject(self.handle, code)

    def active(self) -> int | None:
        """作业里还活着几个进程 (包括子孙); 查不出来是 None."""
        if not self.handle:
            return None
        info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not _api().QueryInformationJobObject(self.handle, JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                                                ctypes.byref(info), ctypes.sizeof(info), None):
            return None
        return info.ActiveProcesses

    def close(self) -> None:
        if self.handle:
            _api().CloseHandle(self.handle)
            self.handle = None


# ---------------------------------------------------------------- 起进程
def _make_pipe(parent_writes: bool) -> tuple[int, int]:
    """匿名管道. 返回 (给子进程的那端, 自己留的那端): 给子进程的可继承, 自己留的不可继承."""
    a = _api()
    attrs = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
    read_end, write_end = wintypes.HANDLE(), wintypes.HANDLE()
    if not a.CreatePipe(ctypes.byref(read_end), ctypes.byref(write_end), ctypes.byref(attrs), 0):
        raise _fail("CreatePipe")
    child_end, parent_end = (read_end.value, write_end.value) if parent_writes else (write_end.value, read_end.value)
    if not a.SetHandleInformation(parent_end, HANDLE_FLAG_INHERIT, 0):
        err = _fail("SetHandleInformation")
        a.CloseHandle(child_end)
        a.CloseHandle(parent_end)
        raise err
    return child_end, parent_end


def _wrap_handle(handle: int, writable: bool):
    """把自己留的那端包成文件对象 (关文件对象就关句柄)."""
    import msvcrt
    fd = msvcrt.open_osfhandle(handle, 0)  # 和标准库 subprocess 包管道句柄时传的一样 (open_osfhandle 只认 O_APPEND、O_RDONLY、O_TEXT)
    return os.fdopen(fd, "wb" if writable else "rb", buffering=0 if writable else -1)


class _AttrList:
    """PROC_THREAD_ATTRIBUTE_LIST: 起进程时带的扩展属性 (只继承哪几个句柄、应用容器的身份)."""

    def __init__(self, count: int) -> None:
        a = _api()
        size = ctypes.c_size_t(0)
        a.InitializeProcThreadAttributeList(None, count, 0, ctypes.byref(size))  # 第一次一定失败, 只为取长度
        if size.value == 0:
            raise _fail("InitializeProcThreadAttributeList")
        self.buf = ctypes.create_string_buffer(size.value)
        if not a.InitializeProcThreadAttributeList(self.buf, count, 0, ctypes.byref(size)):
            raise _fail("InitializeProcThreadAttributeList")
        self._keep: list = []

    def keep(self, obj) -> None:
        """让某个缓冲区活到 CreateProcess 返回之后 (属性里存的是指针, 不是拷贝)."""
        self._keep.append(obj)

    def add(self, attribute: int, value) -> None:
        self.keep(value)
        if not _api().UpdateProcThreadAttribute(self.buf, 0, attribute, ctypes.addressof(value),
                                                ctypes.sizeof(value), None, None):
            raise _fail("UpdateProcThreadAttribute")

    def delete(self) -> None:
        _api().DeleteProcThreadAttributeList(self.buf)


class Child:
    """一个已经起来的子进程: 三条管道 (标准输入、输出、错误)、作业对象、进程句柄.
    探针说话的方式: 一行一个 JSON. send/recv/ask 都只收发纯 ASCII (json.dumps 默认转义), 和代码页无关.
    """

    def __init__(self, process: int, pid: int, job: Job, fin, fout, ferr, create_ms: float) -> None:
        self.pid = pid
        self.create_ms = create_ms
        self._proc: int | None = process
        self._job = job
        self._fin, self._fout, self._ferr = fin, fout, ferr
        self._lines: queue.Queue = queue.Queue()
        self._err = bytearray()
        self._seq = 0
        self._summary: dict | None = None
        self._t_out = threading.Thread(target=self._pump_out, daemon=True)
        self._t_err = threading.Thread(target=self._pump_err, daemon=True)
        self._t_out.start()
        self._t_err.start()

    def _pump_out(self) -> None:
        try:
            for line in iter(self._fout.readline, b""):
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(None)

    def _pump_err(self) -> None:
        try:
            while True:
                chunk = self._ferr.read1(1024)
                if not chunk:
                    break
                if len(self._err) < 4096:
                    self._err.extend(chunk[: 4096 - len(self._err)])
        except (OSError, ValueError):
            pass

    def send(self, obj: dict) -> int:
        self._seq += 1
        data = (json.dumps(dict(obj, id=self._seq)) + "\n").encode("ascii")
        self._fin.write(data)  # 管道没缓冲; 子进程没了会抛 OSError
        return self._seq

    def recv(self, timeout: float) -> dict:
        try:
            line = self._lines.get(timeout=max(0.0, timeout))
        except queue.Empty:
            raise TimeoutError("等探针回话超时") from None
        if line is None:
            raise EOFError("探针的输出断了")
        try:
            got = json.loads(line.decode("ascii", "replace"))
        except ValueError:
            return {"ok": False, "err": "bad_line"}
        return got if isinstance(got, dict) else {"ok": False, "err": "bad_line"}

    def ask(self, obj: dict, timeout: float) -> dict:
        """发一条指令, 等同一个编号的回话; 超时抛 TimeoutError, 对方没了抛 EOFError / OSError."""
        seq = self.send(obj)
        deadline = time.monotonic() + timeout
        while True:
            reply = self.recv(deadline - time.monotonic())
            if reply.get("id") == seq or "id" not in reply:
                return reply

    def exited(self) -> bool:
        return self._proc is None or _api().WaitForSingleObject(self._proc, 0) == WAIT_OBJECT_0

    def wait_exit(self, ms: int) -> bool:
        return self._proc is None or _api().WaitForSingleObject(self._proc, ms) == WAIT_OBJECT_0

    def exit_code(self) -> int | None:
        if self._proc is None:
            return None
        code = wintypes.DWORD()
        if not _api().GetExitCodeProcess(self._proc, ctypes.byref(code)) or code.value == STILL_ACTIVE:
            return None
        return code.value

    def kill(self) -> None:
        self._job.terminate(1)

    def stderr_text(self) -> str:
        return bytes(self._err).decode("utf-8", "replace")

    def close(self) -> dict:
        """结束作业里所有进程, 等它们退出, 放掉句柄. 返回 {exited, active_in_job, exit_code}. 可以重复调."""
        if self._summary is not None:
            return self._summary
        self._job.terminate(1)
        exited = self.wait_exit(5000)
        active = self._job.active()
        for _ in range(20):  # 刚结束的作业, 计数要等一下才归零
            if active in (0, None):
                break
            time.sleep(0.1)
            active = self._job.active()
        code = self.exit_code()
        try:
            self._fin.close()
        except (OSError, ValueError):
            pass
        self._t_out.join(2)
        self._t_err.join(2)
        # 读线程还卡在读 (有进程还握着管道的另一端) 就不去关文件对象, 免得 close 和 readline 抢同一把锁
        if not self._t_out.is_alive():
            self._fout.close()
        if not self._t_err.is_alive():
            self._ferr.close()
        close_handle(self._proc)
        self._proc = None
        self._job.close()
        self._summary = {"exited": exited, "active_in_job": active, "exit_code": code}
        return self._summary


def spawn(exe: str, args: list[str], cwd: str, env: dict[str, str] | None, *, token: int | None = None,
          appcontainer_sid_bytes: bytes | None = None) -> Child:
    """在关法里起一个子进程 (先挂起 -> 放进作业对象 -> 再恢复), 只继承标准输入输出三个句柄.
    - 什么都不给: 普通起 (对照用);
    - token: 用 make_restricted_token 削出来的令牌起 (CreateProcessAsUserW);
    - appcontainer_sid_bytes: 关进这个应用容器, 不给任何能力.
    起之前把自己的"错误模式"设成不弹错误框 (子进程会继承), 起完恢复.
    """
    if token is not None and appcontainer_sid_bytes is not None:
        raise ValueError("令牌和应用容器不能同时用")
    a = _api()
    cmd_buf = ctypes.create_unicode_buffer(build_command_line(exe, args))
    # env 是 None 只给诊断配法用 (让系统给子进程继承本进程的环境, 查应用容器起进程报 203 是不是缺环境变量); 平时一律自己造
    env_raw = build_env_block(env).encode("utf-16-le") if env is not None else b""
    env_buf = (ctypes.c_char * len(env_raw)).from_buffer_copy(env_raw) if env is not None else None
    open_handles: list[int] = []  # 还没交出去的句柄, 出错时统统关掉
    attrs: _AttrList | None = None
    old_mode = a.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX)
    try:
        in_c, in_p = _make_pipe(parent_writes=True)
        open_handles += [in_c, in_p]
        out_c, out_p = _make_pipe(parent_writes=False)
        open_handles += [out_c, out_p]
        err_c, err_p = _make_pipe(parent_writes=False)
        open_handles += [err_c, err_p]

        attrs = _AttrList(2 if appcontainer_sid_bytes else 1)
        inherit_list = (wintypes.HANDLE * 3)(in_c, out_c, err_c)
        attrs.add(PROC_THREAD_ATTRIBUTE_HANDLE_LIST, inherit_list)
        if appcontainer_sid_bytes:
            sid_buf = _sid_buffer(appcontainer_sid_bytes)
            caps = SECURITY_CAPABILITIES()
            caps.AppContainerSid = ctypes.addressof(sid_buf)
            caps.Capabilities = None
            caps.CapabilityCount = 0  # 不给任何能力
            attrs.keep(sid_buf)
            attrs.add(PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, caps)

        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
        startup.StartupInfo.wShowWindow = 0  # SW_HIDE
        startup.StartupInfo.hStdInput = in_c
        startup.StartupInfo.hStdOutput = out_c
        startup.StartupInfo.hStdError = err_c
        startup.lpAttributeList = ctypes.addressof(attrs.buf)
        flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | DETACHED_PROCESS
        if env is not None:
            flags |= CREATE_UNICODE_ENVIRONMENT
        info = PROCESS_INFORMATION()

        t0 = time.perf_counter()
        if token is not None:
            ok = a.CreateProcessAsUserW(token, exe, cmd_buf, None, None, True, flags, env_buf, cwd,
                                        ctypes.byref(startup), ctypes.byref(info))
            api_name = "CreateProcessAsUserW"
        else:
            ok = a.CreateProcessW(exe, cmd_buf, None, None, True, flags, env_buf, cwd,
                                  ctypes.byref(startup), ctypes.byref(info))
            api_name = "CreateProcessW"
        create_ms = (time.perf_counter() - t0) * 1000
        error = ctypes.get_last_error()
        if not ok:
            raise SandboxError(api_name, error)

        process, thread = info.hProcess, info.hThread
        job = None
        try:
            job = Job()
            job.assign(process)
            if a.ResumeThread(thread) == 0xFFFFFFFF:
                raise _fail("ResumeThread")
        except BaseException:
            a.TerminateProcess(process, 1)
            a.CloseHandle(process)
            if job is not None:
                job.close()
            raise
        finally:
            a.CloseHandle(thread)

        # 成功了: 给子进程的三端自己这边关掉 (不然读不到"对方关了"), 自己留的三端交给文件对象
        for handle in (in_c, out_c, err_c):
            a.CloseHandle(handle)
            open_handles.remove(handle)
        try:
            fin = _wrap_handle(in_p, writable=True)
            open_handles.remove(in_p)
            fout = _wrap_handle(out_p, writable=False)
            open_handles.remove(out_p)
            ferr = _wrap_handle(err_p, writable=False)
            open_handles.remove(err_p)
        except BaseException:
            job.terminate(1)  # 进程已经在跑了: 包不成文件对象就别留着它
            job.close()
            a.CloseHandle(process)
            raise
        return Child(process, info.dwProcessId, job, fin, fout, ferr, create_ms)
    finally:
        a.SetErrorMode(old_mode)
        if attrs is not None:
            attrs.delete()
        for handle in open_handles:
            a.CloseHandle(handle)


def process_exists(pid: int) -> bool | None:
    """True: 还在; False: 没有这个编号的进程了; None: 看不出来 (打不开, 比如权限不够).
    注意编号会被系统重用, 只作"刚才起的那个进程已经没了"的旁证."""
    a = _api()
    handle = a.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False if ctypes.get_last_error() == ERROR_INVALID_PARAMETER else None
    try:
        return a.WaitForSingleObject(handle, 0) != WAIT_OBJECT_0
    finally:
        a.CloseHandle(handle)
