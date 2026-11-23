#include "webview_host.h"

#include <windowsx.h>
#include <shlwapi.h>

#include <cwctype>

namespace {

using CreateEnvFn = HRESULT(STDMETHODCALLTYPE*)(PCWSTR, PCWSTR, ICoreWebView2EnvironmentOptions*,
                                                ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler*);
using VersionFn = HRESULT(STDMETHODCALLTYPE*)(PCWSTR, LPWSTR*);

std::wstring mime_of(const std::wstring& path) {
  auto ends = [&](const wchar_t* ext) {
    size_t n = wcslen(ext);
    return path.size() >= n && _wcsicmp(path.c_str() + path.size() - n, ext) == 0;
  };
  if (ends(L".html")) return L"text/html; charset=utf-8";
  if (ends(L".js") || ends(L".mjs")) return L"text/javascript; charset=utf-8";
  if (ends(L".css")) return L"text/css; charset=utf-8";
  if (ends(L".json")) return L"application/json; charset=utf-8";
  if (ends(L".png")) return L"image/png";
  if (ends(L".svg")) return L"image/svg+xml";
  if (ends(L".jpg") || ends(L".jpeg")) return L"image/jpeg";
  if (ends(L".glb")) return L"model/gltf-binary";
  if (ends(L".frag") || ends(L".vert") || ends(L".md") || ends(L".txt")) return L"text/plain; charset=utf-8";
  return L"application/octet-stream";
}

// 只放行 [A-Za-z0-9._-/] 组成、不含 ".." 的相对路径
bool safe_path(const std::wstring& rel) {
  if (rel.empty() || rel.find(L"..") != std::wstring::npos || rel[0] == L'/') return false;
  for (wchar_t c : rel)
    if (!(iswalnum(c) || c == L'.' || c == L'_' || c == L'-' || c == L'/')) return false;
  return true;
}

}  // namespace

HRESULT WebViewHost::start(const Config& cfg) {
  cfg_ = cfg;
  if (cfg_.share && cfg_.share->env_) return on_environment(S_OK, cfg_.share->env_.get());
  loader_ = LoadLibraryW((exe_dir() + L"\\WebView2Loader.dll").c_str());
  if (!loader_) {
    logf("找不到 WebView2Loader.dll (错误 %lu)", GetLastError());
    return HRESULT_FROM_WIN32(GetLastError());
  }
  auto create = reinterpret_cast<CreateEnvFn>(GetProcAddress(loader_, "CreateCoreWebView2EnvironmentWithOptions"));
  auto get_version = reinterpret_cast<VersionFn>(GetProcAddress(loader_, "GetAvailableCoreWebView2BrowserVersionString"));
  if (!create) return E_NOINTERFACE;
  if (get_version) {
    LPWSTR v = nullptr;
    if (SUCCEEDED(get_version(nullptr, &v)) && v) {
      version = v;
      CoTaskMemFree(v);
    }
  }
  logf("WebView2 运行时 %s, 宿主方式 %s", utf8(version).c_str(), cfg_.composition ? "composition" : "hwnd");
  // 页面加载出来之前也不要闪白底
  SetEnvironmentVariableW(L"WEBVIEW2_DEFAULT_BACKGROUND_COLOR", L"00000000");
  // 角色说话不用等用户先点一下页面 (浏览器默认要用户手势才放声音). 已有的附加参数保留
  wchar_t prev[1024] = L"";
  GetEnvironmentVariableW(L"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", prev, 1024);
  std::wstring args = std::wstring(prev) + (prev[0] ? L" " : L"") + L"--autoplay-policy=no-user-gesture-required";
  if (!cfg_.extra_args.empty()) args += L" " + cfg_.extra_args;
  SetEnvironmentVariableW(L"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", args.c_str());
  auto handler = make_handler<ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler, HRESULT, ICoreWebView2Environment*>(
      IID_ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler,
      [this](HRESULT hr, ICoreWebView2Environment* env) { return on_environment(hr, env); });
  return create(nullptr, cfg_.user_data.c_str(), nullptr, handler.get());
}

HRESULT WebViewHost::on_environment(HRESULT hr, ICoreWebView2Environment* env) {
  if (FAILED(hr) || !env) {
    logf("建 WebView2 环境失败 0x%08lx", hr);
    return S_OK;
  }
  env_.borrow(env);
  if (cfg_.composition) {
    auto env3 = env_.as<ICoreWebView2Environment3>(IID_ICoreWebView2Environment3);
    if (!env3) {
      logf("运行时不支持 composition 宿主");
      return S_OK;
    }
    auto h = make_handler<ICoreWebView2CreateCoreWebView2CompositionControllerCompletedHandler, HRESULT,
                          ICoreWebView2CompositionController*>(
        IID_ICoreWebView2CreateCoreWebView2CompositionControllerCompletedHandler,
        [this](HRESULT hr2, ICoreWebView2CompositionController* cc) {
          if (FAILED(hr2) || !cc) {
            logf("建 composition 控制器失败 0x%08lx", hr2);
            return S_OK;
          }
          comp_.borrow(cc);
          auto controller = comp_.as<ICoreWebView2Controller>(IID_ICoreWebView2Controller);
          return on_controller(hr2, controller.get());
        });
    return env3->CreateCoreWebView2CompositionController(cfg_.hwnd, h.get());
  }
  auto h = make_handler<ICoreWebView2CreateCoreWebView2ControllerCompletedHandler, HRESULT, ICoreWebView2Controller*>(
      IID_ICoreWebView2CreateCoreWebView2ControllerCompletedHandler,
      [this](HRESULT hr2, ICoreWebView2Controller* c) { return on_controller(hr2, c); });
  return env_->CreateCoreWebView2Controller(cfg_.hwnd, h.get());
}

HRESULT WebViewHost::on_controller(HRESULT hr, ICoreWebView2Controller* controller) {
  if (FAILED(hr) || !controller) {
    logf("建 WebView2 控制器失败 0x%08lx", hr);
    return S_OK;
  }
  controller_.borrow(controller);
  // Application chrome has a stable logical size. Windows DPI scaling remains
  // automatic; browser zoom and touchpad pinch must not resize the entire UI.
  controller_->put_ZoomFactor(1.0);
  EventRegistrationToken zoom_token;
  controller_->add_ZoomFactorChanged(make_handler<ICoreWebView2ZoomFactorChangedEventHandler, ICoreWebView2Controller*, IUnknown*>(
      IID_ICoreWebView2ZoomFactorChangedEventHandler, [](ICoreWebView2Controller* c, IUnknown*) {
        double factor = 1; c->get_ZoomFactor(&factor);
        if (factor != 1.0) c->put_ZoomFactor(1.0);
        return S_OK;
      }).get(), &zoom_token);
  if (auto c2 = controller_.as<ICoreWebView2Controller2>(IID_ICoreWebView2Controller2)) {
    COREWEBVIEW2_COLOR clear{0, 0, 0, 0};  // A, R, G, B: 全透明
    COREWEBVIEW2_COLOR solid{255, GetRValue(cfg_.background), GetGValue(cfg_.background), GetBValue(cfg_.background)};
    c2->put_DefaultBackgroundColor(cfg_.transparent ? clear : solid);
  }
  RECT rc;
  GetClientRect(cfg_.hwnd, &rc);
  controller_->put_Bounds(rc);
  controller_->get_CoreWebView2(webview_.put());

  if (comp_) {
    HRESULT h2 = DCompositionCreateDevice2(nullptr, kIID_IDCompositionDevice, reinterpret_cast<void**>(dcomp_.put()));
    if (SUCCEEDED(h2)) h2 = dcomp_->CreateTargetForHwnd(cfg_.hwnd, TRUE, target_.put());
    if (SUCCEEDED(h2)) h2 = dcomp_->CreateVisual(visual_.put());
    if (SUCCEEDED(h2)) h2 = target_->SetRoot(visual_.get());
    if (SUCCEEDED(h2)) h2 = comp_->put_RootVisualTarget(visual_.get());
    if (SUCCEEDED(h2)) h2 = dcomp_->Commit();
    if (FAILED(h2)) logf("DirectComposition 设置失败 0x%08lx", h2);
    EventRegistrationToken tok;
    comp_->add_CursorChanged(
        make_handler<ICoreWebView2CursorChangedEventHandler, ICoreWebView2CompositionController*, IUnknown*>(
            IID_ICoreWebView2CursorChangedEventHandler,
            [this](ICoreWebView2CompositionController* cc, IUnknown*) {
              HCURSOR c = nullptr;
              if (SUCCEEDED(cc->get_Cursor(&c))) cursor_ = c;
              return S_OK;
            })
            .get(),
        &tok);
  }

  Com<ICoreWebView2Settings> settings;
  webview_->get_Settings(settings.put());
  if (settings) {
    settings->put_AreDefaultContextMenusEnabled(cfg_.devtools);
    settings->put_AreDevToolsEnabled(cfg_.devtools);
    settings->put_IsStatusBarEnabled(FALSE);
    settings->put_IsZoomControlEnabled(FALSE);
    if (auto settings5 = settings.as<ICoreWebView2Settings5>(IID_ICoreWebView2Settings5))
      settings5->put_IsPinchZoomEnabled(FALSE);
    settings->put_IsBuiltInErrorPageEnabled(FALSE);
  }

  EventRegistrationToken tok;
  if (allow_navigation) {
    webview_->add_NavigationStarting(
        make_handler<ICoreWebView2NavigationStartingEventHandler, ICoreWebView2*, ICoreWebView2NavigationStartingEventArgs*>(
            IID_ICoreWebView2NavigationStartingEventHandler,
            [this](ICoreWebView2*, ICoreWebView2NavigationStartingEventArgs* args) {
              LPWSTR raw = nullptr; args->get_Uri(&raw);
              const bool allowed = raw && allow_navigation(raw);
              CoTaskMemFree(raw);
              if (!allowed) args->put_Cancel(TRUE);
              return S_OK;
            }).get(), &tok);
  }
  webview_->AddWebResourceRequestedFilter((cfg_.server + L"/pet/*").c_str(), COREWEBVIEW2_WEB_RESOURCE_CONTEXT_ALL);
  webview_->add_WebResourceRequested(
      make_handler<ICoreWebView2WebResourceRequestedEventHandler, ICoreWebView2*, ICoreWebView2WebResourceRequestedEventArgs*>(
          IID_ICoreWebView2WebResourceRequestedEventHandler,
          [this](ICoreWebView2*, ICoreWebView2WebResourceRequestedEventArgs* args) { return serve(args); })
          .get(),
      &tok);
  webview_->add_WebMessageReceived(
      make_handler<ICoreWebView2WebMessageReceivedEventHandler, ICoreWebView2*, ICoreWebView2WebMessageReceivedEventArgs*>(
          IID_ICoreWebView2WebMessageReceivedEventHandler,
          [this](ICoreWebView2*, ICoreWebView2WebMessageReceivedEventArgs* args) {
            if (allow_message) {
              LPWSTR raw_source = nullptr;
              HRESULT hr = args->get_Source(&raw_source);
              const std::wstring source = raw_source ? raw_source : L"";
              if (raw_source) CoTaskMemFree(raw_source);
              if (FAILED(hr) || !allow_message(source)) return S_OK;
            }
            LPWSTR s = nullptr;
            if (FAILED(args->TryGetWebMessageAsString(&s)) || !s) return S_OK;
            std::string msg = utf8(s);
            CoTaskMemFree(s);
            if (on_message) on_message(msg);
            return S_OK;
          })
          .get(),
      &tok);
  webview_->add_NavigationCompleted(
      make_handler<ICoreWebView2NavigationCompletedEventHandler, ICoreWebView2*, ICoreWebView2NavigationCompletedEventArgs*>(
          IID_ICoreWebView2NavigationCompletedEventHandler,
          [this](ICoreWebView2*, ICoreWebView2NavigationCompletedEventArgs* args) {
            BOOL ok = FALSE;
            COREWEBVIEW2_WEB_ERROR_STATUS status = COREWEBVIEW2_WEB_ERROR_STATUS_UNKNOWN;
            args->get_IsSuccess(&ok);
            args->get_WebErrorStatus(&status);
            logf("页面导航%s (状态 %d)", ok ? "完成" : "失败", int(status));
            if (on_navigated) on_navigated(ok, int(status));
            return S_OK;
          })
          .get(),
      &tok);
  // 页面要权限 (麦克风等): 由宿主决定, 不弹 WebView2 自己的对话框. 麦克风只在用户从托盘打开时放行, 其余一律拒绝.
  webview_->add_PermissionRequested(
      make_handler<ICoreWebView2PermissionRequestedEventHandler, ICoreWebView2*, ICoreWebView2PermissionRequestedEventArgs*>(
          IID_ICoreWebView2PermissionRequestedEventHandler,
          [this](ICoreWebView2*, ICoreWebView2PermissionRequestedEventArgs* args) {
            COREWEBVIEW2_PERMISSION_KIND kind = COREWEBVIEW2_PERMISSION_KIND_UNKNOWN_PERMISSION;
            args->get_PermissionKind(&kind);
            LPWSTR raw = nullptr;
            args->get_Uri(&raw);
            std::wstring uri = raw ? raw : L"";
            CoTaskMemFree(raw);
            bool allow = allow_permission && allow_permission(kind, uri);
            args->put_State(allow ? COREWEBVIEW2_PERMISSION_STATE_ALLOW : COREWEBVIEW2_PERMISSION_STATE_DENY);
            logf("页面要权限 %d: %s", int(kind), allow ? "放行" : "拒绝");
            return S_OK;
          })
          .get(),
      &tok);
  webview_->add_ProcessFailed(
      make_handler<ICoreWebView2ProcessFailedEventHandler, ICoreWebView2*, ICoreWebView2ProcessFailedEventArgs*>(
          IID_ICoreWebView2ProcessFailedEventHandler,
          [](ICoreWebView2*, ICoreWebView2ProcessFailedEventArgs* args) {
            COREWEBVIEW2_PROCESS_FAILED_KIND kind = COREWEBVIEW2_PROCESS_FAILED_KIND_BROWSER_PROCESS_EXITED;
            args->get_ProcessFailedKind(&kind);
            logf("WebView2 进程出错, 类型 %d", int(kind));
            return S_OK;
          })
          .get(),
      &tok);

  std::wstring url = cfg_.server + L"/pet/" + cfg_.page + (cfg_.page_query.empty() ? L"" : L"?" + cfg_.page_query);
  logf("打开 %s", utf8(url).c_str());
  return webview_->Navigate(url.c_str());
}

HRESULT WebViewHost::serve(ICoreWebView2WebResourceRequestedEventArgs* args) {
  Com<ICoreWebView2WebResourceRequest> req;
  args->get_Request(req.put());
  if (!req) return S_OK;
  LPWSTR raw = nullptr;
  req->get_Uri(&raw);
  std::wstring uri = raw ? raw : L"";
  CoTaskMemFree(raw);
  std::wstring prefix = cfg_.server + L"/pet/";
  if (uri.compare(0, prefix.size(), prefix) != 0) return S_OK;
  std::wstring rel = uri.substr(prefix.size());
  size_t cut = rel.find_first_of(L"?#");
  if (cut != std::wstring::npos) rel.resize(cut);
  if (rel.empty()) rel = L"pet.html";

  std::vector<char> body;
  int status = 404;
  if (safe_path(rel)) {
    // Live2D 的 SDK (live2d/) 和模型 (live2d-models/)、小屋的房间 (rooms/, prepare_room.py 导出的) 在不进版本库的
    // desktop/third_party/, 其余在 desktop/web/
    bool third = rel.rfind(L"live2d/", 0) == 0 || rel.rfind(L"live2d-models/", 0) == 0 || rel.rfind(L"rooms/", 0) == 0;
    std::wstring file = (third ? cfg_.web_dir + L"\\..\\third_party" : cfg_.web_dir) + L"\\" + rel;
    for (auto& c : file)
      if (c == L'/') c = L'\\';
    if (read_file(file, body)) status = 200;
  }
  if (status != 200) {
    logf("页面文件不存在: %s", utf8(rel).c_str());
    static const char kMissing[] = "not found";
    body.assign(kMissing, kMissing + sizeof kMissing - 1);
  }
  Com<IStream> stream;
  stream.attach(SHCreateMemStream(reinterpret_cast<const BYTE*>(body.data()), UINT(body.size())));
  std::wstring headers = L"Content-Type: " + (status == 200 ? mime_of(rel) : L"text/plain") + L"\r\nCache-Control: no-store";
  Com<ICoreWebView2WebResourceResponse> resp;
  env_->CreateWebResourceResponse(stream.get(), status, status == 200 ? L"OK" : L"Not Found", headers.c_str(), resp.put());
  if (resp) args->put_Response(resp.get());
  return S_OK;
}

void WebViewHost::resize(const RECT& client) {
  if (controller_) controller_->put_Bounds(client);
}

void WebViewHost::post_json(const std::wstring& json) {
  if (webview_) webview_->PostWebMessageAsJson(json.c_str());
}

void WebViewHost::set_visible(bool visible) {
  if (controller_) controller_->put_IsVisible(visible ? TRUE : FALSE);
}

void WebViewHost::reload() {
  if (webview_) webview_->Reload();
}

void WebViewHost::navigate_query(const std::wstring& query) {
  cfg_.page_query = query;
  if (!webview_) return;
  std::wstring url = cfg_.server + L"/pet/" + cfg_.page + (query.empty() ? L"" : L"?" + query);
  logf("打开 %s", utf8(url).c_str());
  webview_->Navigate(url.c_str());
}

void WebViewHost::open_devtools() {
  if (webview_) webview_->OpenDevToolsWindow();
}

void WebViewHost::parent_moved() {
  if (controller_) controller_->NotifyParentWindowPositionChanged();
}

void WebViewHost::move_focus() {
  if (controller_) controller_->MoveFocus(COREWEBVIEW2_MOVE_FOCUS_REASON_PROGRAMMATIC);
}

void WebViewHost::set_memory_low(bool low) {
  auto w19 = webview_.as<ICoreWebView2_19>(IID_ICoreWebView2_19);
  if (!w19) {
    logf("运行时不支持内存目标设置");
    return;
  }
  HRESULT hr = w19->put_MemoryUsageTargetLevel(low ? COREWEBVIEW2_MEMORY_USAGE_TARGET_LEVEL_LOW
                                                   : COREWEBVIEW2_MEMORY_USAGE_TARGET_LEVEL_NORMAL);
  logf("内存目标设为%s (0x%08lx)", low ? "低" : "正常", hr);
}

bool WebViewHost::forward_mouse(UINT msg, WPARAM wp, LPARAM lp) {
  if (!comp_) return false;
  COREWEBVIEW2_MOUSE_EVENT_KIND kind;
  UINT32 data = 0;
  POINT pt{GET_X_LPARAM(lp), GET_Y_LPARAM(lp)};
  switch (msg) {
    case WM_MOUSEMOVE:
      kind = COREWEBVIEW2_MOUSE_EVENT_KIND_MOVE;
      if (!tracking_leave_) {
        TRACKMOUSEEVENT t{sizeof t, TME_LEAVE, cfg_.hwnd, 0};
        tracking_leave_ = TrackMouseEvent(&t);
      }
      break;
    case WM_LBUTTONDOWN: kind = COREWEBVIEW2_MOUSE_EVENT_KIND_LEFT_BUTTON_DOWN; SetCapture(cfg_.hwnd); break;
    case WM_LBUTTONUP: kind = COREWEBVIEW2_MOUSE_EVENT_KIND_LEFT_BUTTON_UP; ReleaseCapture(); break;
    case WM_RBUTTONDOWN: kind = COREWEBVIEW2_MOUSE_EVENT_KIND_RIGHT_BUTTON_DOWN; break;
    case WM_RBUTTONUP: kind = COREWEBVIEW2_MOUSE_EVENT_KIND_RIGHT_BUTTON_UP; break;
    case WM_MBUTTONDOWN: kind = COREWEBVIEW2_MOUSE_EVENT_KIND_MIDDLE_BUTTON_DOWN; break;
    case WM_MBUTTONUP: kind = COREWEBVIEW2_MOUSE_EVENT_KIND_MIDDLE_BUTTON_UP; break;
    case WM_MOUSEWHEEL:
      kind = COREWEBVIEW2_MOUSE_EVENT_KIND_WHEEL;
      data = UINT32(GET_WHEEL_DELTA_WPARAM(wp));
      ScreenToClient(cfg_.hwnd, &pt);  // 滚轮消息给的是屏幕坐标
      break;
    case WM_MOUSELEAVE:
      kind = COREWEBVIEW2_MOUSE_EVENT_KIND_LEAVE;
      tracking_leave_ = false;
      comp_->SendMouseInput(kind, COREWEBVIEW2_MOUSE_EVENT_VIRTUAL_KEYS_NONE, 0, POINT{0, 0});
      return true;
    default:
      return false;
  }
  comp_->SendMouseInput(kind, COREWEBVIEW2_MOUSE_EVENT_VIRTUAL_KEYS(GET_KEYSTATE_WPARAM(wp)), data, pt);
  return true;
}

bool WebViewHost::set_cursor() {
  if (!comp_ || !cursor_) return false;
  SetCursor(cursor_);
  return true;
}

double WebViewHost::rasterization_scale() const {
  double s = 0;
  if (auto c3 = controller_.as<ICoreWebView2Controller3>(IID_ICoreWebView2Controller3)) c3->get_RasterizationScale(&s);
  return s;
}

void WebViewHost::capture_png(const std::wstring& path, std::function<void(bool ok)> done) {
  Com<IStream> stream;
  if (!webview_ || FAILED(SHCreateStreamOnFileEx(path.c_str(), STGM_CREATE | STGM_WRITE, FILE_ATTRIBUTE_NORMAL, TRUE, nullptr,
                                                 stream.put()))) {
    done(false);
    return;
  }
  HRESULT hr = webview_->CapturePreview(
      COREWEBVIEW2_CAPTURE_PREVIEW_IMAGE_FORMAT_PNG, stream.get(),
      make_handler<ICoreWebView2CapturePreviewCompletedHandler, HRESULT>(IID_ICoreWebView2CapturePreviewCompletedHandler,
                                                                        [stream, done](HRESULT r) {
                                                                          done(SUCCEEDED(r));
                                                                          return S_OK;
                                                                        })
          .get());
  if (FAILED(hr)) done(false);
}

void WebViewHost::close() {
  if (controller_) controller_->Close();
  webview_.reset();
  comp_.reset();
  controller_.reset();
  visual_.reset();
  target_.reset();
  dcomp_.reset();
  env_.reset();
}
