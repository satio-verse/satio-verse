# 桌宠宿主 wmpet (M0). 用 desktop/scripts/build.ps1 构建 (写死 CPEditor 自带的 MinGW-w64 路径).
cmake_minimum_required(VERSION 3.20)
project(wmpet CXX RC)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# WebView2 SDK 1.0.4314.35 (NuGet 包原样解开, 不改), 只用头文件和 x64 的 WebView2Loader.dll
set(WV2 ${CMAKE_CURRENT_SOURCE_DIR}/../third_party/webview2/pkg/build/native)
# 开发版: 自检用这一份 (日志、WebView2 数据都在 build/dev 下). build.ps1 编完再把它发布到 build/bin (用户实际用的那份).
set(OUT ${CMAKE_CURRENT_SOURCE_DIR}/../build/dev/bin)

add_executable(wmpet WIN32 src/main.cpp src/webview_host.cpp src/selftest.cpp src/util.cpp src/ledge.cpp src/ledge_test.cpp
  src/json.cpp src/http_server.cpp src/api.cpp src/mcp.cpp src/settings.cpp src/mcp_client.cpp src/panel.cpp src/tools.cpp src/activity.cpp src/guard.cpp src/builtins.cpp src/extpack.cpp src/workbench.cpp src/services.cpp
  src/autonomy.cpp src/browser_pane.cpp res/wmpet.rc)  # 图标 (res/app.ico)
target_include_directories(wmpet PRIVATE ${WV2}/include src)
target_compile_definitions(wmpet PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX _WIN32_WINNT=0x0A00 WINVER=0x0A00)
target_compile_options(wmpet PRIVATE -Wall -Wno-unknown-pragmas -finput-charset=UTF-8 -fexec-charset=UTF-8)
target_link_options(wmpet PRIVATE -municode -static -static-libgcc -static-libstdc++)
target_link_libraries(wmpet PRIVATE ole32 oleaut32 uuid shlwapi shell32 user32 gdi32 dwmapi dcomp pdh psapi ws2_32 winhttp bcrypt crypt32 normaliz)
set_target_properties(wmpet PROPERTIES RUNTIME_OUTPUT_DIRECTORY ${OUT})
add_custom_command(TARGET wmpet POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_if_different ${WV2}/x64/WebView2Loader.dll ${OUT}/WebView2Loader.dll)
