// 内容包 (扩展中心 E1, 见 docs/roadmap/extensions.md 第 2、5 节): 人设预设和 skill 打成一个 .wmext 文件, 导入导出.
// 内容包不带代码, 也不能给她加工具 (人设里的 MCP 服务器、内置工具不带过去, 用哪些工具只看你在预设里勾了什么).
// .wmext 是一个 UTF-8 的 JSON 文件 (都是文字, 用记事本也能看):
//   {"wmext": 1,
//    "manifest": {"id", "name", "version", "type": "content", "author", "license", "description", "contains": {"personas": [名字], "skills": [名字]}},
//    "files": [{"path": "personas/<id>.json" | "skills/<名字>/<文件>", "sha256", "text"}]}
// 导入先 inspect (校验格式、路径、SHA-256, 列出全文和可疑的话给用户看), 用户点了才 install. 装过的记在设置的 extensions 段, 卸载按记录删.
// 导出写到设置目录下的 exports\<id>-<版本>.wmext.
#pragma once
#include <string>

#include "json.h"

namespace extpack {

Json inspect(const std::string& text, std::string& error);  // 预览: {manifest, items: [{kind, name, text, warnings}], conflicts, warnings}
Json install(const std::string& text, std::string& error);  // 装好的记录 (设置已保存)
bool uninstall(const std::string& id, std::string& error);
Json export_pack(const Json& req, std::string& error);       // req: {personas: [id], skills: [名字], id, name, version, author, license, description} -> {path}
std::wstring exports_dir();
Json audit_tail(int limit);  // 工具调用的审计日志, 最新的在前
std::string sha256_hex(const std::string& data);

}  // namespace extpack
