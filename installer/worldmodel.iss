; WorldModel 桌宠的安装包 (docs/roadmap/own-harness.md 9.1, 只下载一个桌宠的第 C 步; 非商业发布).
; 编译: runtime\innosetup\ISCC.exe installer\worldmodel.iss  (Inno Setup 7.1.0, 便携装在项目里)
;   出来的是 build\installer\WorldModel-Setup-<版本>.exe. 要先有: runtime\python (tools\setup_python.py)、
;   desktop\build\dev\bin\wmpet.exe (desktop\scripts\build.ps1)、downloads\installer\ 里的 WebView2 引导程序和 VC++ 运行库.
; 装在哪: 默认只给当前用户装 (%LOCALAPPDATA%\Programs\WorldModel, 不要管理员); 目录结构和仓库一样, 程序里的相对路径照常.
; 组件: 核心 (固定) + N 卡加速 (CUDA, 约 1.1 GB; 检测到 NVIDIA 驱动 (System32\nvcuda.dll) 就默认勾上, 没有就不装、本机模型用 CPU).
; 不放进来的: 用户自己的设置和记忆、爱弥斯音色、MIOMIO / Wendy、Live2D、HAST-SEC、GPT-SoVITS 等可选组件 (以后在设置里另下).

#define AppName "WorldModel 桌宠"
#define AppVersion "0.3.0"
#define Root ".."
; 桌宠的启动参数 (快捷方式、开机启动、装完打开都一样): 连本机 8790, 后台起本地服务的看守, 用自带的 Python

[Setup]
AppId={{8C4F2B1E-7A55-4E5B-9B2E-4D3C1A9E6F10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=WorldModel
DefaultDirName={localappdata}\Programs\WorldModel
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#Root}\build\installer
OutputBaseFilename=WorldModel-Setup-{#AppVersion}
SetupIconFile={#Root}\desktop\host\res\app.ico
UninstallDisplayIcon={app}\desktop\build\bin\wmpet.exe
UninstallDisplayName={#AppName}
; 3 GB 多的东西: 不整体压 (省内存, 也让没勾的组件不用解压), 模型文件本来就压不动
Compression=lzma2/normal
SolidCompression=no
WizardStyle=modern
CloseApplications=yes
InfoAfterFile={#Root}\installer\THIRD_PARTY_NOTICES.txt

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Types]
Name: "full"; Description: "推荐 (检测到 N 卡时带上显卡加速)"
Name: "custom"; Description: "自己选"; Flags: iscustom

[Components]
Name: "core"; Description: "桌宠和她的大脑: 服务端、自带 Python、本机小模型、语音识别和合成、默认角色、小屋 (约 2.2 GB)"; Types: full custom; Flags: fixed
Name: "cuda"; Description: "N 卡加速 (CUDA, 约 1.1 GB): 本机模型用显卡跑, 快很多. 没有 N 卡用不上, 会用 CPU"; Types: full custom

[Tasks]
Name: "desktopicon"; Description: "在桌面放一个快捷方式"
Name: "autostart"; Description: "开机自动启动桌宠"; Flags: unchecked

[Files]
; ---- 桌宠宿主和页面 ----
Source: "{#Root}\desktop\build\dev\bin\wmpet.exe"; DestDir: "{app}\desktop\build\bin"; Components: core; Flags: ignoreversion
Source: "{#Root}\desktop\build\dev\bin\WebView2Loader.dll"; DestDir: "{app}\desktop\build\bin"; Components: core; Flags: ignoreversion
Source: "{#Root}\desktop\web\*"; DestDir: "{app}\desktop\web"; Excludes: "*.bak*,\brand\avatar-source.png,\brand\logo-source.webp"; Components: core; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Root}\desktop\skills\*"; DestDir: "{app}\desktop\skills"; Excludes: "*.bak*"; Components: core; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Root}\desktop\third_party\rooms\*"; DestDir: "{app}\desktop\third_party\rooms"; Excludes: "\petal-oak\export.log,\petal-oak\export_game_summary.json,\casa-luce-v8\integration-manifest.json,\casa-luce-v8\validation.json"; Components: core; Flags: ignoreversion recursesubdirs createallsubdirs
; ---- 服务端 ----
Source: "{#Root}\wm\*"; DestDir: "{app}\wm"; Excludes: "__pycache__,*.pyc,*.bak*,\net\*"; Components: core; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Root}\web\*"; DestDir: "{app}\web"; Excludes: "__pycache__,*.bak*"; Components: core; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Root}\config\default.json"; DestDir: "{app}\config"; Components: core; Flags: ignoreversion
; 可选组件的清单 (官方仓库、锁定的提交、每个文件的哈希): 设置里"版本与组件"页点了才从官方下
Source: "{#Root}\config\components.json"; DestDir: "{app}\config"; Components: core; Flags: ignoreversion
Source: "{#Root}\tools\serve.py"; DestDir: "{app}\tools"; Components: core; Flags: ignoreversion
Source: "{#Root}\tools\start_llm.py"; DestDir: "{app}\tools"; Components: core; Flags: ignoreversion
; 产品版的默认配置 (本机模型 + 语音识别 + 轻量语音合成; 有 N 卡加速用显卡): 已经有了就不覆盖 (你改过的留着)
Source: "{#Root}\installer\local.cuda.json"; DestDir: "{app}\config"; DestName: "local.json"; Components: cuda; Flags: onlyifdoesntexist
Source: "{#Root}\installer\local.cpu.json"; DestDir: "{app}\config"; DestName: "local.json"; Components: not cuda; Flags: onlyifdoesntexist
Source: "{#Root}\installer\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Components: core; Flags: ignoreversion
; 各家的许可原文 (installer\licenses\, 来源网址、大小、SHA-256、取的日期见里面的 SOURCES.txt); NVIDIA 的 EULA 跟着"N 卡加速"组件走
Source: "{#Root}\installer\licenses\*"; DestDir: "{app}\licenses"; Excludes: "NVIDIA-CUDA-EULA.txt"; Components: core; Flags: ignoreversion
Source: "{#Root}\installer\licenses\NVIDIA-CUDA-EULA.txt"; DestDir: "{app}\licenses"; Components: cuda; Flags: ignoreversion
; ---- 解压 7z 的小工具 (GPT-SoVITS 官方整合包是 7z): 7-Zip 26.04 的 7zr.exe, 0.6 MB ----
Source: "{#Root}\runtime\7zip\7zr.exe"; DestDir: "{app}\runtime\7zip"; Components: core; Flags: ignoreversion
; ---- 自带 Python (tools\setup_python.py 装的) ----
Source: "{#Root}\runtime\python\*"; DestDir: "{app}\runtime\python"; Excludes: "__pycache__,*.pyc,\Lib\site-packages\adodbapi\*"; Components: core; Flags: ignoreversion recursesubdirs createallsubdirs
; ---- 本机模型 llama.cpp: 只要 llama-server 和它用的库 ----
Source: "{#Root}\runtime\llama\llama-server.exe"; DestDir: "{app}\runtime\llama"; Components: core; Flags: ignoreversion
Source: "{#Root}\runtime\llama\llama-server-impl.dll"; DestDir: "{app}\runtime\llama"; Components: core; Flags: ignoreversion
Source: "{#Root}\runtime\llama\llama-common.dll"; DestDir: "{app}\runtime\llama"; Components: core; Flags: ignoreversion
Source: "{#Root}\runtime\llama\llama.dll"; DestDir: "{app}\runtime\llama"; Components: core; Flags: ignoreversion
Source: "{#Root}\runtime\llama\mtmd.dll"; DestDir: "{app}\runtime\llama"; Components: core; Flags: ignoreversion
Source: "{#Root}\runtime\llama\ggml.dll"; DestDir: "{app}\runtime\llama"; Components: core; Flags: ignoreversion
Source: "{#Root}\runtime\llama\ggml-base.dll"; DestDir: "{app}\runtime\llama"; Components: core; Flags: ignoreversion
Source: "{#Root}\runtime\llama\ggml-cpu-*.dll"; DestDir: "{app}\runtime\llama"; Components: core; Flags: ignoreversion
Source: "{#Root}\runtime\llama\libomp.dll"; DestDir: "{app}\runtime\llama"; Components: core; Flags: ignoreversion
Source: "{#Root}\runtime\llama\LICENSE-LLVM-OpenMP"; DestDir: "{app}\runtime\llama"; Components: core; Flags: ignoreversion
Source: "{#Root}\runtime\llama\ggml-cuda.dll"; DestDir: "{app}\runtime\llama"; Components: cuda; Flags: ignoreversion
Source: "{#Root}\runtime\llama\cudart64_12.dll"; DestDir: "{app}\runtime\llama"; Components: cuda; Flags: ignoreversion
Source: "{#Root}\runtime\llama\cublas64_12.dll"; DestDir: "{app}\runtime\llama"; Components: cuda; Flags: ignoreversion
Source: "{#Root}\runtime\llama\cublasLt64_12.dll"; DestDir: "{app}\runtime\llama"; Components: cuda; Flags: ignoreversion
; ---- 模型和动作 ----
Source: "{#Root}\models\llm\Qwen3.5-0.8B-Q8_0.gguf"; DestDir: "{app}\models\llm"; Components: core; Flags: ignoreversion nocompression
Source: "{#Root}\models\asr\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09\*"; DestDir: "{app}\models\asr\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"; Components: core; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Root}\models\asr\silero_vad.onnx"; DestDir: "{app}\models\asr"; Components: core; Flags: ignoreversion
Source: "{#Root}\models\tts\matcha-icefall-zh-baker\*"; DestDir: "{app}\models\tts\matcha-icefall-zh-baker"; Components: core; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Root}\models\tts\vocos-22khz-univ.onnx"; DestDir: "{app}\models\tts"; Components: core; Flags: ignoreversion
Source: "{#Root}\models\vrm\silvercat.vrm"; DestDir: "{app}\models\vrm"; Components: core; Flags: ignoreversion
Source: "{#Root}\models\motion\*"; DestDir: "{app}\models\motion"; Components: core; Flags: ignoreversion recursesubdirs createallsubdirs
; ---- 装的时候用一下就删: 运行库 ----
Source: "{#Root}\downloads\installer\vcredist\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedVCRedist
Source: "{#Root}\downloads\installer\webview2\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedWebView2

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\desktop\build\bin\wmpet.exe"; Parameters: "--server http://127.0.0.1:8790 --services --python ""{app}\runtime\python\pythonw.exe"""; WorkingDir: "{app}\desktop\build\bin"
Name: "{group}\第三方组件和许可"; Filename: "{app}\THIRD_PARTY_NOTICES.txt"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\desktop\build\bin\wmpet.exe"; Parameters: "--server http://127.0.0.1:8790 --services --python ""{app}\runtime\python\pythonw.exe"""; WorkingDir: "{app}\desktop\build\bin"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "WorldModelPet"; ValueData: """{app}\desktop\build\bin\wmpet.exe"" --server http://127.0.0.1:8790 --services --python ""{app}\runtime\python\pythonw.exe"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "正在安装 Visual C++ 运行库 (本机模型要用; 可能会弹出管理员确认)…"; Check: NeedVCRedist; Flags: waituntilterminated
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "正在安装 WebView2 运行时…"; Check: NeedWebView2; Flags: waituntilterminated
Filename: "{app}\desktop\build\bin\wmpet.exe"; Parameters: "--server http://127.0.0.1:8790 --services --python ""{app}\runtime\python\pythonw.exe"""; WorkingDir: "{app}\desktop\build\bin"; Description: "现在打开桌宠"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; 先请本地服务的看守把它开的服务都停掉 (桌宠本身由 CloseApplications 关); --root: 只停这一份装的, 同一台电脑上别的不碰
Filename: "{app}\runtime\python\python.exe"; Parameters: "-m wm.services stop --root ""{app}""";WorkingDir: "{app}"; Flags: runhidden waituntilterminated; RunOnceId: "StopServices"
; 再删掉设置里"版本与组件"装的可选组件: 只删从那里装的文件 (GPT-SoVITS 目录里你训练出的音色、自己放的东西不碰)
Filename: "{app}\runtime\python\python.exe"; Parameters: "-m wm.components remove-all"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveComponents"

[UninstallDelete]
; 运行时生成的东西 (运行日志、Python 的缓存); 你的设置和记忆在 %APPDATA%\WorldModel 和 %USERPROFILE%\.worldmodel, 不删
; logs 里只删运行日志 (服务端、本地服务的输出). 别的是她攒下来的 (2026-10-27 用户: "uninstall should only remove plain logs
; (serve, services), keep cognition, billing and other growth data, and say where they are kept"): 账本和额度 billing、
; 社交经验 social、逐轮记录 *.jsonl 都留着, 卸载完说一声留在哪 (CurUninstallStepChanged).
; 小窝的经历 (2026-10-27 晚起) 和动作成长的数据已经在用户数据区 %USERPROFILE%\.worldmodel\experience、\motion-growth
; (wm/cognition/store.py、wm/motion_growth.py), 本来就不在这里删; logs\cognition 里只剩搬走前的旧副本 (*.migrated).
; 别的以后做 D (更新和备份) 时也挪到用户数据目录.
; 下载队列下好的东西在 {app}\download-queue (2026-11-16 用户定: 下载位置是桌宠文件夹的子文件夹): 是用户在卡片上点头让她下的, 可能有几个 GB,
; 所以这里故意不删它, 也不能把它放进 {app}\downloads 里面 (下面那条整个删). 卸载完只在"卸载后留下的"里告诉用户它在哪.
; 0.3.2 待办: 下载服务和它的小看守 (python -m wm.downloads) 是另起的进程, 卸载时没人去停; 有下载在进行时卸载, {app}\runtime\python
; 里的文件会被占住 (装在 {localappdata}、CloseApplications 在, 最坏是有文件删不掉, 不是故障). 要停它们得给 wm.downloads 加 stop 子命令.
Type: filesandordirs; Name: "{app}\logs\serve"
Type: filesandordirs; Name: "{app}\logs\services"
Type: filesandordirs; Name: "{app}\wm"
Type: filesandordirs; Name: "{app}\tools\__pycache__"
Type: filesandordirs; Name: "{app}\runtime\python"
Type: filesandordirs; Name: "{app}\desktop\build\webview2-data"
; 可选组件没下完的部分
Type: filesandordirs; Name: "{app}\downloads"

[Code]
// 有 NVIDIA 显卡驱动 (CUDA 要的 nvcuda.dll 在 System32) 才默认勾上"N 卡加速"
function HasNvidia: Boolean;
begin
  Result := FileExists(ExpandConstant('{sys}\nvcuda.dll'));
end;

procedure InitializeWizard;
begin
  if HasNvidia then
    WizardSelectComponents('cuda')
  else
    WizardSelectComponents('!cuda');
end;

// VC++ 2015-2022 x64 运行库装过就不再装
function NeedVCRedist: Boolean;
var
  v: Cardinal;
begin
  Result := not (RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', v) and (v = 1));
end;

// WebView2 运行时装过 (给所有用户或当前用户) 就不再装; Win11 自带
function WebView2Version(root: Integer; key: String): String;
begin
  if not RegQueryStringValue(root, key, 'pv', Result) then
    Result := '';
end;

function NeedWebView2: Boolean;
var
  v: String;
begin
  v := WebView2Version(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}');
  if (v = '') or (v = '0.0.0.0') then
    v := WebView2Version(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}');
  Result := (v = '') or (v = '0.0.0.0');
end;

// 卸载完: 她攒下来的东西没删 (见 [UninstallDelete]): logs 里放一个说明, 弹一句告诉你都留在哪 (静默卸载不弹).
// 小窝的经历、动作成长的数据在用户数据区 (%USERPROFILE%\.worldmodel), 卸载本来就不碰, 也写上位置
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  dir, home, dl, dlnote: String;
  lines: TArrayOfString;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  dir := ExpandConstant('{app}\logs');
  home := ExpandConstant('{%USERPROFILE}') + '\.worldmodel';
  dl := ExpandConstant('{app}\download-queue');
  if not DirExists(dir) and not DirExists(home + '\experience') and not DirExists(home + '\motion-growth') and not DirExists(dl) then
    exit;
  dlnote := '';
  if DirExists(dl) then
    dlnote := '· 她下载的东西: ' + dl + #13#10;
  if DirExists(dir) then
  begin
    SetArrayLength(lines, 14);
    lines[0] := '这个文件夹是卸载 WorldModel 时特意留下的: 里面是她攒下来的东西, 卸载不删.';
    lines[1] := '';
    lines[2] := '  billing\     账本 (每天花了多少) 和按渠道的额度 (budget.json)';
    lines[3] := '  social\      社交经验 (她做了什么、对方什么反应)';
    lines[4] := '  *.jsonl      对话、小窝、外部渠道的逐轮记录';
    lines[5] := '  cognition\   小窝认知搬走前的旧副本 (*.migrated), 现在用的在下面的 experience\';
    lines[6] := '';
    lines[7] := '另外几样不在这个文件夹, 卸载也不碰:';
    lines[8] := '  ' + home + '\experience\     小窝的经历 (看到了什么、东西在哪、端东西的经验)';
    lines[9] := '  ' + home + '\motion-growth\  动作成长 (她练出来的动作参数)';
    lines[10] := '  ' + dl + '\     她下载的东西 (下载队列, 你点头才下的; 在设置里改过下载位置的话, 那个位置也不动)';
    lines[11] := '';
    lines[12] := '重新装会接着用. 不要了就把这个文件夹和上面几个文件夹删掉.';
    lines[13] := '(服务端、本地服务的运行日志 serve\、services\ 已经删了.)';
    SaveStringsToUTF8File(dir + '\卸载后留下的.txt', lines, False);
  end;
  if not UninstallSilent then
    MsgBox('卸载完了. 她攒下来的东西没有删:' + #13#10 +
      '· 账本和额度、社交经验、逐轮记录: ' + dir + #13#10 +
      '· 小窝的经历: ' + home + '\experience' + #13#10 +
      '· 动作成长: ' + home + '\motion-growth' + #13#10 + dlnote + #13#10 +
      '重新装会接着用; 不要了就把这几个文件夹删掉. ' + dir + ' 里的"卸载后留下的.txt"写着每样是什么.', mbInformation, MB_OK);
end;
