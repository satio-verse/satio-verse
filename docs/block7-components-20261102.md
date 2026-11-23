# 第 7 块（文档引用 / Office）要下的组件清单（2026-11-02，已装）

第 7 块要做的事（原定路线）：

1. 原生网页里选一段引用，"回到原处"时跳到 `网址#:~:text=那段字`（浏览器内核自带的滚到文字）。
2. PDF 引用记页码。
3. Office：她生成 Word 报告 → 你在工作台里选一段引用、让她改写 → 导出 PDF / PPT。

第 1、2 件和 Office 里的大部分**不用下载**：本机已经装了 Word / PowerPoint / Excel 和下面这些 Python 包。真正缺的只有做 PPT 的库，另有一个可选的 Word 预览库。

## 本机已经有的（不用下）

| 名字 | 版本 | 许可 | 第 7 块里用来做什么 |
|---|---|---|---|
| Microsoft Office 16（Word / PowerPoint / Excel） | 本机装好的，COM 已注册 | 你自己的授权 | Word / PPT 转 PDF：经 COM 让本机 Word、PowerPoint 另存为 PDF，版式和你用 Word 打开时一样 |
| python-docx | 1.2.0 | MIT | 生成 Word 报告；按段落改写后写回 |
| openpyxl | 3.1.5 | MIT | 读写 Excel |
| pywin32 | 311 | PSF | 调本机 Word / PowerPoint 的 COM |
| pypdf / pypdfium2 | 6.10.2 / 5.12.1 | BSD-3 / BSD-3 + Apache-2.0 | 读 PDF 文字、按页定位、出页面缩略图 |
| lxml / Pillow | 6.1.0 / 10.4.0 | BSD-3 / HPND | python-pptx、python-docx 的依赖 |
| 工作台现有的文件读取（`wm/agent/material_readers.py`） | — | 本项目 | 已经能读 docx / xlsx / pptx 的文字、按段落和表格分块，不靠第三方库 |

## 要下的（都是 PyPI 上的纯 Python 包，装进本机 Python 3.10）

| 名字 | 版本 | 来源 | 大小（wheel） | 许可 | 用来做什么 | 要不要 |
|---|---|---|---|---|---|---|
| python-pptx | 1.0.2（2024-08） | PyPI `python-pptx`（scanny/python-pptx） | 462 KB | MIT | 她生成 PPT（一页一页写标题、要点、图） | **要**（不下就只能出 Word / PDF，不能出 PPT） |
| XlsxWriter | 3.3.1（2026-10-28） | PyPI `XlsxWriter`（jmcnamara/XlsxWriter） | 176 KB | BSD-2-Clause | python-pptx 的依赖（PPT 里的图表数据） | 跟着 python-pptx 一起 |
| mammoth | 1.13.0（2026-09） | PyPI `mammoth`（mwilliamson/python-mammoth） | 54 KB | BSD-2-Clause | Word 文档在工作台面板里预览成网页（标题、列表、表格、图都保留），方便一段一段选中引用 | 可选（不下就用现有读取器按段落画个简单版，列表、图会差一些） |
| cobble | 0.1.4 | PyPI `cobble` | 4 KB | BSD-2-Clause | mammoth 的依赖 | 跟着 mammoth |

合计：要的约 640 KB；加上可选的约 700 KB。都不碰显卡、不改系统设置。

## 不建议现在下

| 名字 | 版本 / 大小 | 许可 | 为什么不下 |
|---|---|---|---|
| LibreOffice | 26.8.2，Windows 安装包 358.5 MB（官方 download.documentfoundation.org） | MPL-2.0 | 只在没装 Office 的电脑上才需要用它转 PDF。你的电脑有 Office；以后联合发布给别人用时，可以做成设置里"组件"页的可选下载 |
| docx2pdf | 0.1.8，7 KB | MIT | 只是包了一层 Word COM，本机的 pywin32 直接调就行，多一个依赖没意义 |

## 你点头以后怎么装

用本机的 Python 装，只装上面列的版本：

```bash
python -m pip install python-pptx==1.0.2 XlsxWriter==3.3.1
```

要可选的 Word 预览再加：

```bash
python -m pip install mammoth==1.13.0 cobble==0.1.4
```

装进的是服务端用的那份 Python（`C:\Users\ASUS\AppData\Local\Programs\Python\Python310`）。只装包不影响正在跑的服务；第 7 块的新功能（她生成、改写 Word，导出 PDF / PPT 的工具）要改后端，照约定先和主会话说，上线要重启服务端（8790），重启前先问你、确认她没在忙。

## 装好了

2026-11-02 用户回"装必需和可选"，装好了：python-pptx 1.0.2、XlsxWriter 3.3.1、mammoth 1.13.0、cobble 0.1.4（`python -m pip install` 固定版本，装前装后 `pip freeze` 对比只多了这四个，别的包没动）。试跑过：python-docx 写一份 Word → mammoth 转成网页（标题、列表、表格都在）；python-pptx 写出 PPT；本机 Word 经 COM（pywin32 `DispatchEx("Word.Application")`，`SaveAs2` FileFormat=17）把 docx 另存成 PDF。LibreOffice、docx2pdf 没下。第 7 块的功能还没开始做（要改后端，先和主会话说）。

## 功能做好了

2026-11-02 第 7 块做完（见 `docs/workbench-polish-block7-20261102.html`）：她的 Office 工具、Word 预览、按段引用、导出 PDF、网页引用回到原处。Office 部分要等 8790 重启才上线。

第 7 块上线：2026-11-02 04:06 用户回"现在重启"后上线：重启前查过她空着（QQ 没有在跑的学习任务、工作台没有在跑的会话），`python tools/serve.py restart`（先试跑新代码 4 秒，再请看守重启），启动编号 69c403fe → 39976d76，`/api/status` 通、QQ 在线；核对过：她的工具里有 docx_create / docx_read / docx_edit / pptx_create / office_to_pdf，`docx_preview`、`raw` 两个接口在（只读请求），那张"她想改进自己"的申请（QQ #A1，存在 asks.json）重启后还在。
