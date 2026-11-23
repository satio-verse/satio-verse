#include "browser_pane.h"
#include <winhttp.h>
#include <shlwapi.h>
#include <algorithm>
#include <cctype>
#include <cmath>

namespace {
using CreateEnvironment = HRESULT(STDMETHODCALLTYPE*)(PCWSTR, PCWSTR, ICoreWebView2EnvironmentOptions*,
    ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler*);
HMODULE browser_loader() {
  static HMODULE module = nullptr;
  if(!module) module=LoadLibraryW((exe_dir()+L"\\WebView2Loader.dll").c_str());
  return module;
}
bool parse_url(const std::wstring& url, URL_COMPONENTS& parts) {
  if (url.empty() || url.size() > 16384 || std::any_of(url.begin(), url.end(), [](wchar_t c) {return c <= 32 || c == L'\\';})) return false;
  parts = {}; parts.dwStructSize = sizeof parts;
  parts.dwHostNameLength = parts.dwUserNameLength = parts.dwPasswordLength = DWORD(-1);
  return WinHttpCrackUrl(url.c_str(), DWORD(url.size()), 0, &parts) &&
      (parts.nScheme == INTERNET_SCHEME_HTTP || parts.nScheme == INTERNET_SCHEME_HTTPS) &&
      parts.dwHostNameLength && !parts.dwUserNameLength && !parts.dwPasswordLength;
}
}

BrowserPane::BrowserPane(HWND owner, std::wstring user_data, std::set<int> protected_ports)
    : owner_(owner), user_data_(std::move(user_data)), protected_ports_(std::move(protected_ports)) {protected_ports_.erase(0);}

bool BrowserPane::allowed_url(const std::wstring& url, const std::set<int>& ports) {
  URL_COMPONENTS parts{};
  return parse_url(url, parts) && !ports.count(parts.nPort);
}

int BrowserPane::url_port(const std::wstring& url) {
  URL_COMPONENTS parts{};
  return parse_url(url, parts) ? parts.nPort : 0;
}

void BrowserPane::start() {
  if (started_ || closed_) return;
  started_ = true;
  emit("starting");
  const DWORD attributes=GetFileAttributesW(user_data_.c_str());
  if(attributes!=INVALID_FILE_ATTRIBUTES && !(attributes&FILE_ATTRIBUTE_DIRECTORY)) {
    fail("浏览器数据目录被同名文件占用，请检查后重试");return;
  }
  loader_ = browser_loader();
  auto create = loader_ ? reinterpret_cast<CreateEnvironment>(GetProcAddress(loader_, "CreateCoreWebView2EnvironmentWithOptions")) : nullptr;
  if (!create) { fail("浏览器运行时未能加载"); return; }
  auto done = make_handler<ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler, HRESULT, ICoreWebView2Environment*>(
      IID_ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler,
      [this](HRESULT hr, ICoreWebView2Environment* env) {
        if (closed_) return S_OK;
        if (FAILED(hr) || !env) {fail("浏览器环境未能启动"); return S_OK;}
        environment_.borrow(env);
        auto ready = make_handler<ICoreWebView2CreateCoreWebView2ControllerCompletedHandler, HRESULT, ICoreWebView2Controller*>(
            IID_ICoreWebView2CreateCoreWebView2ControllerCompletedHandler,
            [this](HRESULT result, ICoreWebView2Controller* controller) {
              if (closed_) {if (controller) controller->Close(); return S_OK;}
              if (FAILED(result) || !controller) {fail("网页面板未能创建"); return S_OK;}
              return configure(controller);
            });
        const HRESULT result = env->CreateCoreWebView2Controller(owner_, ready.get());
        if (FAILED(result)) fail("网页面板未能创建");
        return S_OK;
      });
  if (FAILED(create(nullptr, user_data_.c_str(), nullptr, done.get()))) fail("浏览器启动失败");
}

void BrowserPane::fail(const std::string& error) {
  network_ready_ = false; started_ = false;
  if(controller_) controller_->Close();
  network_receiver_.reset(); view_.reset(); controller_.reset(); environment_.reset();
  emit("error", error);
}

HRESULT BrowserPane::configure(ICoreWebView2Controller* controller) {
  controller_.borrow(controller);
  controller_->put_IsVisible(FALSE);
  controller_->get_CoreWebView2(view_.put());
  if (!view_) {fail("浏览器不可用"); return S_OK;}
  Com<ICoreWebView2Settings> settings;
  view_->get_Settings(settings.put());
  if (!settings || FAILED(settings->put_IsWebMessageEnabled(FALSE)) || FAILED(settings->put_AreHostObjectsAllowed(FALSE))) {
    fail("网页隔离未能启用，请关闭后重试"); return S_OK;
  }
  settings->put_AreDefaultContextMenusEnabled(TRUE);
  settings->put_AreDevToolsEnabled(FALSE);
  settings->put_IsZoomControlEnabled(TRUE);
  settings->put_IsStatusBarEnabled(TRUE);
  settings->put_IsBuiltInErrorPageEnabled(TRUE);
  EventRegistrationToken token;
  controller_->add_ZoomFactorChanged(make_handler<ICoreWebView2ZoomFactorChangedEventHandler, ICoreWebView2Controller*, IUnknown*>(
      IID_ICoreWebView2ZoomFactorChangedEventHandler, [this](ICoreWebView2Controller*, IUnknown*) {emit();return S_OK;}).get(), &token);
  view_->add_NavigationStarting(make_handler<ICoreWebView2NavigationStartingEventHandler, ICoreWebView2*, ICoreWebView2NavigationStartingEventArgs*>(
      IID_ICoreWebView2NavigationStartingEventHandler, [this](ICoreWebView2*, ICoreWebView2NavigationStartingEventArgs* args) {
        LPWSTR raw = nullptr; args->get_Uri(&raw); std::wstring url = raw ? raw : L""; CoTaskMemFree(raw);
        if (!allowed_url(url, protected_ports_)) {args->put_Cancel(TRUE); emit("error", "此地址不能在网页面板中打开");}
        else {pending_url_ = url; http_status_ = 0; emit("loading");}
        return S_OK;
      }).get(), &token);
  view_->add_NavigationCompleted(make_handler<ICoreWebView2NavigationCompletedEventHandler, ICoreWebView2*, ICoreWebView2NavigationCompletedEventArgs*>(
      IID_ICoreWebView2NavigationCompletedEventHandler, [this](ICoreWebView2*, ICoreWebView2NavigationCompletedEventArgs* args) {
        BOOL ok = FALSE; args->get_IsSuccess(&ok);
        COREWEBVIEW2_WEB_ERROR_STATUS status; args->get_WebErrorStatus(&status);
        if (status == COREWEBVIEW2_WEB_ERROR_STATUS_OPERATION_CANCELED) {if(state_ == "loading") emit("ready");}
        else if(http_status_>=400) emit("error", "网站返回 HTTP " + std::to_string(http_status_) + "，请重试或在系统浏览器中打开");
        else emit(ok ? "ready" : "error", ok ? "" : "网页加载失败（" + std::to_string(int(status)) + "），可以重试或在系统浏览器打开");
        return S_OK;
      }).get(), &token);
  auto changed = [this](ICoreWebView2*, IUnknown*) {emit(); return S_OK;};
  view_->add_HistoryChanged(make_handler<ICoreWebView2HistoryChangedEventHandler, ICoreWebView2*, IUnknown*>(IID_ICoreWebView2HistoryChangedEventHandler, changed).get(), &token);
  view_->add_DocumentTitleChanged(make_handler<ICoreWebView2DocumentTitleChangedEventHandler, ICoreWebView2*, IUnknown*>(IID_ICoreWebView2DocumentTitleChangedEventHandler, changed).get(), &token);
  view_->add_SourceChanged(make_handler<ICoreWebView2SourceChangedEventHandler, ICoreWebView2*, ICoreWebView2SourceChangedEventArgs*>(
      IID_ICoreWebView2SourceChangedEventHandler, [this](ICoreWebView2*, ICoreWebView2SourceChangedEventArgs*) {emit(); return S_OK;}).get(), &token);
  view_->add_NewWindowRequested(make_handler<ICoreWebView2NewWindowRequestedEventHandler, ICoreWebView2*, ICoreWebView2NewWindowRequestedEventArgs*>(
      IID_ICoreWebView2NewWindowRequestedEventHandler, [this](ICoreWebView2*, ICoreWebView2NewWindowRequestedEventArgs* args) {
        args->put_Handled(TRUE);
        BOOL user = FALSE; args->get_IsUserInitiated(&user);
        LPWSTR raw = nullptr; args->get_Uri(&raw); std::wstring url = raw ? raw : L""; CoTaskMemFree(raw);
        if (user && allowed_url(url, protected_ports_)) navigate(url);
        else emit("", "已阻止网页自动弹窗；可使用页面中的普通链接");
        return S_OK;
      }).get(), &token);
  view_->add_PermissionRequested(make_handler<ICoreWebView2PermissionRequestedEventHandler, ICoreWebView2*, ICoreWebView2PermissionRequestedEventArgs*>(
      IID_ICoreWebView2PermissionRequestedEventHandler, [](ICoreWebView2*, ICoreWebView2PermissionRequestedEventArgs* args) {
        args->put_State(COREWEBVIEW2_PERMISSION_STATE_DENY); return S_OK;
      }).get(), &token);
  view_->add_ProcessFailed(make_handler<ICoreWebView2ProcessFailedEventHandler, ICoreWebView2*, ICoreWebView2ProcessFailedEventArgs*>(
      IID_ICoreWebView2ProcessFailedEventHandler, [this](ICoreWebView2*, ICoreWebView2ProcessFailedEventArgs*) {
        emit("error", "网页进程已退出，请关闭网页面板后重新打开"); return S_OK;
      }).get(), &token);
  // Stop web documents from reaching the privileged local services, even through
  // iframes, fetches, redirects, alternate loopback spelling, or DNS aliases.
  auto view22 = view_.as<ICoreWebView2_22>(IID_ICoreWebView2_22);
  if (!view22 || FAILED(view22->AddWebResourceRequestedFilterWithRequestSourceKinds(L"*", COREWEBVIEW2_WEB_RESOURCE_CONTEXT_ALL, COREWEBVIEW2_WEB_RESOURCE_REQUEST_SOURCE_KINDS_ALL))) {
    fail("请更新 Microsoft Edge WebView2 运行时后使用网页面板"); return S_OK;
  }
  view_->add_WebResourceRequested(make_handler<ICoreWebView2WebResourceRequestedEventHandler, ICoreWebView2*, ICoreWebView2WebResourceRequestedEventArgs*>(
      IID_ICoreWebView2WebResourceRequestedEventHandler, [this](ICoreWebView2*, ICoreWebView2WebResourceRequestedEventArgs* args) {
        Com<ICoreWebView2WebResourceRequest> req; args->get_Request(req.put());
        if (!req) return S_OK;
        LPWSTR raw = nullptr; req->get_Uri(&raw); std::wstring url = raw ? raw : L""; CoTaskMemFree(raw);
        const int port = url_port(url);
        if (port && protected_ports_.count(port)) {
          Com<ICoreWebView2WebResourceResponse> response;
          environment_->CreateWebResourceResponse(nullptr, 403, L"Application endpoint unavailable to web content", L"Content-Type: text/plain", response.put());
          if(response) args->put_Response(response.get());
        }
        return S_OK;
      }).get(), &token);
  if (auto view4 = view_.as<ICoreWebView2_4>(IID_ICoreWebView2_4)) {
    view4->add_DownloadStarting(make_handler<ICoreWebView2DownloadStartingEventHandler, ICoreWebView2*, ICoreWebView2DownloadStartingEventArgs*>(
        IID_ICoreWebView2DownloadStartingEventHandler, [this](ICoreWebView2*, ICoreWebView2DownloadStartingEventArgs* args) {
          // Downloads remain explicit in the external browser until a dedicated
          // save-location UI exists; never silently write a website's files.
          args->put_Cancel(TRUE); emit("", "请在系统浏览器中下载文件"); return S_OK;
        }).get(), &token);
  }
  // NavigationStarting may arrive after a redirect has already hit its target;
  // WebResourceRequested also skips redirected requests. Pause protected ports
  // before their request is sent, including redirect hops, at the network layer.
  Json patterns = Json::array();
  // Include the initial URL as well: Chromium only continues intercepting a
  // redirect chain when the original request matched the interception pattern.
  patterns.push(Json::object().set("urlPattern", "*").set("requestStage", "Request"));
  patterns.push(Json::object().set("urlPattern", "*").set("requestStage", "Response"));
  Json network = Json::object(); network.set("patterns", patterns);
  view_->GetDevToolsProtocolEventReceiver(L"Fetch.requestPaused", network_receiver_.put());
  if(!network_receiver_) {fail("网页隔离未能启用，请关闭后重试"); return S_OK;}
  network_receiver_->add_DevToolsProtocolEventReceived(
      make_handler<ICoreWebView2DevToolsProtocolEventReceivedEventHandler, ICoreWebView2*, ICoreWebView2DevToolsProtocolEventReceivedEventArgs*>(
        IID_ICoreWebView2DevToolsProtocolEventReceivedEventHandler, [this](ICoreWebView2*, ICoreWebView2DevToolsProtocolEventReceivedEventArgs* args) {
          if(closed_ || !view_) return S_OK;
          LPWSTR raw=nullptr; args->get_ParameterObjectAsJson(&raw); Json event; Json::parse(raw ? utf8(raw) : "",event); CoTaskMemFree(raw);
          const auto id=event.get_str("requestId"); if(id.empty()) return S_OK;
          Json command=Json::object(); command.set("requestId",id);
          const auto request_url=widen(event["request"].get_str("url"));
          bool blocked=protected_ports_.count(url_port(request_url))>0;
          const int status=int(event["responseStatusCode"].number());
          // A challenge/error document can stall in its head scripts before
          // NavigationCompleted, leaving a blank page. Report the actual main
          // document response immediately, while still allowing it to render.
          if(status && event.get_str("resourceType")=="Document" && request_url==pending_url_) {
            http_status_=status;
            if(status>=400) emit("error", "网站返回 HTTP " + std::to_string(status) + "，请重试或在系统浏览器中打开");
          }
          if(status>=300 && status<400) for(const auto& header:event["responseHeaders"].items()) {
            auto name=header.get_str("name");std::transform(name.begin(),name.end(),name.begin(),[](unsigned char c){return char(std::tolower(c));});
            if(name!="location") continue;
            auto location=widen(header.get_str("value"));
            const auto begin=location.find_first_not_of(L" \t\r\n"),end=location.find_last_not_of(L" \t\r\n");
            location=begin==std::wstring::npos ? L"" : location.substr(begin,end-begin+1);
            wchar_t combined[32768];DWORD size=32768;
            if(location.find(L'\\')!=std::wstring::npos || FAILED(UrlCombineW(request_url.c_str(),location.c_str(),combined,&size,0)) || !allowed_url(combined,protected_ports_)) blocked=true;
          }
          if(blocked) command.set("errorReason","BlockedByClient");
          view_->CallDevToolsProtocolMethod(blocked ? L"Fetch.failRequest" : L"Fetch.continueRequest",widen(command.dump()).c_str(),nullptr);
          return S_OK;
        }).get(),&token);
  const HRESULT policy = view_->CallDevToolsProtocolMethod(L"Fetch.enable", widen(network.dump()).c_str(),
      make_handler<ICoreWebView2CallDevToolsProtocolMethodCompletedHandler, HRESULT, LPCWSTR>(
        IID_ICoreWebView2CallDevToolsProtocolMethodCompletedHandler, [this](HRESULT hr, LPCWSTR) {
          if(closed_) return S_OK;
          if(FAILED(hr)) {fail("网页隔离未能启用，请关闭后重试"); return S_OK;}
          network_ready_ = true;
          update_bounds();
          if(!pending_url_.empty()) view_->Navigate(pending_url_.c_str());
          return S_OK;
        }).get());
  if(FAILED(policy)) fail("网页隔离未能启用，请关闭后重试");
  return S_OK;
}

void BrowserPane::emit(const std::string& state, const std::string& error) {
  if (closed_ && state != "closed") return;
  if (!state.empty()) {state_ = state; error_ = error;}
  else if (!error.empty()) error_ = error;
  BOOL back = FALSE, forward = FALSE;
  double zoom = 1.0;
  if (controller_) controller_->get_ZoomFactor(&zoom);
  std::wstring url = pending_url_, title;
  if (view_) {
    view_->get_CanGoBack(&back); view_->get_CanGoForward(&forward);
    LPWSTR raw = nullptr; view_->get_Source(&raw); if(raw) {url = raw; CoTaskMemFree(raw);}
    raw = nullptr; view_->get_DocumentTitle(&raw); if(raw) {title = raw; CoTaskMemFree(raw);}
  }
  Json status = Json::object();
  if (title.size() > 500) title.resize(500);
  status.set("t", "workbench-browser").set("state", state_).set("url", utf8(state_ == "loading" ? pending_url_ : url))
      .set("title", utf8(title)).set("can_back", bool(back)).set("can_forward", bool(forward)).set("error", error_).set("visible", visible_).set("zoom_factor", zoom).set("http_status", http_status_);
  if (on_state) on_state(status);
}

void BrowserPane::navigate(const std::wstring& url) {
  if (closed_) return;
  if (!allowed_url(url, protected_ports_)) {emit("error", "仅支持普通 HTTP / HTTPS 网页；应用接口应从工作台访问"); return;}
  pending_url_ = url;
  if (!started_) start();
  else if (view_ && network_ready_) view_->Navigate(url.c_str());
}

void BrowserPane::bounds(const Json& data, bool visible) {
  double x = data["x"].number(), y = data["y"].number(), w = data["width"].number(), h = data["height"].number(), scale = data["scale"].number(1);
  if (!(std::isfinite(x) && std::isfinite(y) && std::isfinite(w) && std::isfinite(h) && std::isfinite(scale)) ||
      scale < .25 || scale > 8 || w < 0 || h < 0 || std::abs(x) > 100000 || std::abs(y) > 100000 || w > 100000 || h > 100000) return;
  bounds_ = {LONG(std::round(x * scale)), LONG(std::round(y * scale)), LONG(std::round((x+w)*scale)), LONG(std::round((y+h)*scale))};
  visible_ = visible && w > 0 && h > 0;
  update_bounds();
}

void BrowserPane::update_bounds() {
  if (!controller_ || closed_) return;
  RECT client{}, clipped{}; GetClientRect(owner_, &client);
  const bool intersects = IntersectRect(&clipped, &client, &bounds_) != FALSE;
  controller_->put_Bounds(clipped);
  controller_->put_IsVisible(visible_ && intersects && !IsIconic(owner_));
}

void BrowserPane::command(const Json& message) {
  const std::string action = message.get_str("action");
  if (message["bounds"].is_object()) bounds(message["bounds"], message["visible"].boolean(true));
  if (action == "navigate") navigate(widen(message.get_str("url")));
  else if (action == "activate") {visible_ = bounds_.right > bounds_.left && bounds_.bottom > bounds_.top; update_bounds(); emit();}
  else if (action == "hide") {visible_ = false; update_bounds();}
  else if (action == "close") close();
  else if (action == "state") emit();
  else if (view_) {
    if (action == "back") view_->GoBack();
    else if (action == "forward") view_->GoForward();
    else if (action == "reload") view_->Reload();
    else if (action == "stop") {view_->Stop(); emit("ready");}
    else if (action == "focus") controller_->MoveFocus(COREWEBVIEW2_MOVE_FOCUS_REASON_PROGRAMMATIC);
    else if (action == "zoom") {
      const double factor=message["factor"].number(1.0);
      if(std::isfinite(factor) && factor >= .5 && factor <= 2.0) {controller_->put_ZoomFactor(factor);emit();}
    }
    else if (action == "selection") {
      const auto rid=message.get_str("request_id");
      if(rid.size()>100) return;
      view_->ExecuteScript(L"window.getSelection()?.toString().slice(0,16000)||''",
        make_handler<ICoreWebView2ExecuteScriptCompletedHandler,HRESULT,LPCWSTR>(IID_ICoreWebView2ExecuteScriptCompletedHandler,
          [this,rid](HRESULT hr,LPCWSTR raw) {
            if(closed_ || !view_) return S_OK;
            Json selected;Json::parse(raw?utf8(raw):"",selected);
            LPWSTR source=nullptr,title=nullptr;view_->get_Source(&source);view_->get_DocumentTitle(&title);
            std::wstring source_text=source?source:L"",title_text=title?title:L"";
            if(source_text.size()>16384)source_text.resize(16384);
            if(title_text.size()>500)title_text.resize(500);
            Json reply=Json::object();reply.set("t","workbench-browser-selection").set("request_id",rid)
                .set("url",utf8(source_text)).set("title",utf8(title_text)).set("text",SUCCEEDED(hr)&&selected.is_string()?selected.str():"");
            if(FAILED(hr))reply.set("error","未能读取网页选区，请重试");
            CoTaskMemFree(source);CoTaskMemFree(title);if(on_state)on_state(reply);return S_OK;
          }).get());
    }
  }
}

void BrowserPane::parent_moved() {
  if (controller_) {controller_->NotifyParentWindowPositionChanged(); update_bounds();}
}

void BrowserPane::close() {
  if (closed_) return;
  closed_ = true; visible_ = false;
  if (controller_) controller_->Close();
  network_receiver_.reset(); view_.reset(); controller_.reset(); environment_.reset();
  emit("closed");
}
