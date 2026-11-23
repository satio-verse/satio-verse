// WebView2 的宿主: 两种方式 (M0 要实测哪种在本机透明得对):
//   hwnd: 普通窗口宿主, WebView2 自己建子窗口, 鼠标直接进页面;
//   comp: composition 宿主, WebView2 画进我们的 DirectComposition 视觉树, 鼠标由宿主转发.
// 页面地址是 <server>/pet/pet.html: /pet/* 由宿主从 desktop/web/ 读文件返回, 其他路径照常请求服务 (同源, 服务端不用改).
#pragma once
#include "util.h"

// DirectComposition: 只用到建设备、建目标、建视觉、提交. MinGW 的 dcomp.h 会带进 d2d1helper.h, 而它在这版 MinGW 下编不过,
// 所以按 Windows SDK 的 vtable 顺序自己声明最少的几个方法 (后面的方法用不到, 不声明).
struct IDCompositionVisual : public IUnknown {};
struct IDCompositionTarget : public IUnknown {
  virtual HRESULT STDMETHODCALLTYPE SetRoot(IDCompositionVisual* visual) = 0;
};
struct IDCompositionDevice : public IUnknown {
  virtual HRESULT STDMETHODCALLTYPE Commit() = 0;
  virtual HRESULT STDMETHODCALLTYPE WaitForCommitCompletion() = 0;
  virtual HRESULT STDMETHODCALLTYPE GetFrameStatistics(void* statistics) = 0;
  virtual HRESULT STDMETHODCALLTYPE CreateTargetForHwnd(HWND hwnd, BOOL topmost, IDCompositionTarget** target) = 0;
  virtual HRESULT STDMETHODCALLTYPE CreateVisual(IDCompositionVisual** visual) = 0;
};
// {C37EA93A-E7AA-450D-B16F-9746CB0407F3}
constexpr IID kIID_IDCompositionDevice = {0xc37ea93a, 0xe7aa, 0x450d, {0xb1, 0x6f, 0x97, 0x46, 0xcb, 0x04, 0x07, 0xf3}};
extern "C" HRESULT WINAPI DCompositionCreateDevice2(IUnknown* rendering_device, REFIID iid, void** device);

class WebViewHost {
  friend struct WorkbenchIntegrationProbe;
 public:
  struct Config {
    HWND hwnd = nullptr;
    bool composition = false;
    bool devtools = false;
    std::wstring server;     // http://127.0.0.1:8790
    std::wstring web_dir;    // desktop/web 的绝对路径
    std::wstring page_query; // 页面地址的查询参数 (可空)
    std::wstring extra_args; // 额外的浏览器参数 (自检的假麦克风用)
    std::wstring user_data;  // WebView2 的用户数据目录
    std::wstring page = L"pet.html";  // /pet/ 下的哪个页面
    bool transparent = true;          // 页面背景透明 (桌宠); 设置面板这种普通窗口用不透明
    COLORREF background = RGB(32, 33, 36);  // 不透明时页面加载出来之前的底色
    WebViewHost* share = nullptr;     // 和这个宿主共用 WebView2 环境 (同一组浏览器进程, 不多占内存)
  };

  std::function<void(const std::string&)> on_message;  // 页面发来的字符串 (UTF-8)
  std::function<bool(const std::wstring& source)> allow_message;  // 可选的消息来源限制，在解析消息前检查
  std::function<bool(const std::wstring& url)> allow_navigation;  // Trusted UI must not navigate into untrusted content.
  std::function<void(bool ok, int status)> on_navigated;
  std::function<bool(COREWEBVIEW2_PERMISSION_KIND kind, const std::wstring& uri)> allow_permission;  // 没设时一律拒绝

  HRESULT start(const Config& cfg);
  void resize(const RECT& client);
  void post_json(const std::wstring& json);
  void set_visible(bool visible);
  void reload();
  void navigate_query(const std::wstring& query);  // 换查询参数重新打开页面 (换 3D / 2D 门面时)
  void open_devtools();
  void parent_moved();
  void set_memory_low(bool low);  // WebView2 的内存目标: 低时它会主动收缩缓存 (ICoreWebView2_19)
  void move_focus();              // 键盘焦点交给页面
  bool forward_mouse(UINT msg, WPARAM wp, LPARAM lp);  // composition 宿主时把鼠标消息转给页面
  bool set_cursor();                                  // composition 宿主时按页面要求设光标
  double rasterization_scale() const;
  // 把页面现在的样子存成 PNG (自检截图用, 不受窗口遮挡影响); done 在 UI 线程上调
  void capture_png(const std::wstring& path, std::function<void(bool ok)> done);
  bool ready() const { return bool(webview_); }
  void close();

  std::wstring version;  // WebView2 运行时版本

 private:
  HRESULT on_environment(HRESULT hr, ICoreWebView2Environment* env);
  HRESULT on_controller(HRESULT hr, ICoreWebView2Controller* controller);
  HRESULT serve(ICoreWebView2WebResourceRequestedEventArgs* args);

  Config cfg_;
  HMODULE loader_ = nullptr;
  Com<ICoreWebView2Environment> env_;
  Com<ICoreWebView2Controller> controller_;
  Com<ICoreWebView2CompositionController> comp_;
  Com<ICoreWebView2> webview_;
  Com<IDCompositionDevice> dcomp_;
  Com<IDCompositionTarget> target_;
  Com<IDCompositionVisual> visual_;
  HCURSOR cursor_ = nullptr;
  bool tracking_leave_ = false;
};
