// Standalone local-only integration probe, built/run by tools/test_native_browser.py.
#include "browser_pane.h"
#include <fstream>
#include <iostream>

LRESULT CALLBACK probe_window(HWND h, UINT m, WPARAM w, LPARAM l) {return DefWindowProcW(h,m,w,l);}
struct BrowserPaneProbe {
  static void profile(BrowserPane* pane, const std::wstring& value) {pane->user_data_=value;}
  static bool hidden(BrowserPane* pane) {BOOL visible=TRUE;return pane->controller_ && SUCCEEDED(pane->controller_->get_IsVisible(&visible)) && !visible;}
  static bool attach(BrowserPane* pane, int& messages, Json& network) {
    Com<ICoreWebView2Settings> settings; pane->view_->get_Settings(settings.put());
    BOOL bridge=TRUE,objects=TRUE;
    settings->get_IsWebMessageEnabled(&bridge);settings->get_AreHostObjectsAllowed(&objects);
    EventRegistrationToken token;
    pane->view_->add_WebMessageReceived(make_handler<ICoreWebView2WebMessageReceivedEventHandler,ICoreWebView2*,ICoreWebView2WebMessageReceivedEventArgs*>(
        IID_ICoreWebView2WebMessageReceivedEventHandler,[&messages](ICoreWebView2*,ICoreWebView2WebMessageReceivedEventArgs*){messages++;return S_OK;}).get(),&token);
    pane->network_receiver_->add_DevToolsProtocolEventReceived(make_handler<ICoreWebView2DevToolsProtocolEventReceivedEventHandler,ICoreWebView2*,ICoreWebView2DevToolsProtocolEventReceivedEventArgs*>(
        IID_ICoreWebView2DevToolsProtocolEventReceivedEventHandler,[&network](ICoreWebView2*,ICoreWebView2DevToolsProtocolEventReceivedEventArgs* args){LPWSTR raw=nullptr;args->get_ParameterObjectAsJson(&raw);Json event;Json::parse(raw?utf8(raw):"",event);CoTaskMemFree(raw);network.push(event["request"].get_str("url"));return S_OK;}).get(),&token);
    return !bridge && !objects;
  }
};
int wmain(int argc, wchar_t** argv) {
  if (argc != 5) return 2;
  SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
  CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
  const std::wstring origin = argv[1];
  const int forbidden = _wtoi(argv[2]);
  const std::set<int> ports{8790,8793,forbidden};
  Json report = Json::object(), checks = Json::object(), events = Json::array(), network = Json::array();
  checks.set("https_allowed",BrowserPane::allowed_url(L"https://example.com/hello?q=1",ports));
  checks.set("local_dev_allowed",BrowserPane::allowed_url(origin+L"/a",ports));
  bool blocked = true;
  for (const auto& url : {L"file:///C:/private.txt",L"javascript:alert(1)",L"data:text/html,hello",L"https://user:pass@example.com/",L"http://127.0.0.1:8790/agent",L"http://localhost:8793/",L"http://2130706433:8790/api/agent",L"http://evil.example:8790/",L"http://[::1]:8790/",L"https://example.com\\evil"})
    blocked = blocked && !BrowserPane::allowed_url(url,ports);
  checks.set("unsafe_and_service_urls_blocked",blocked);
  WNDCLASSW wc{}; wc.lpfnWndProc=probe_window;wc.hInstance=GetModuleHandleW(nullptr);wc.lpszClassName=L"WMIsolatedBrowserProbe";RegisterClassW(&wc);
  HWND window=CreateWindowW(wc.lpszClassName,L"Isolated browser test",WS_OVERLAPPEDWINDOW,0,0,900,700,nullptr,nullptr,wc.hInstance,nullptr);
  const auto invalid_profile=std::wstring(argv[3])+L"-invalid";
  {std::ofstream invalid(utf8(invalid_profile));invalid<<"A file cannot be a profile directory.";}
  auto* browser = new BrowserPane(window, invalid_profile, ports);
  int step=0,messages=0; bool done=false, probe=false, attached=false,recovered=false;
  auto nav=[&](const std::wstring& url){Json m=Json::object(),box=Json::object();box.set("x",0).set("y",0).set("width",800).set("height",600).set("scale",1);m.set("action","navigate").set("url",utf8(url)).set("bounds",box);browser->command(m);};
  browser->on_state = [&](const Json& state) {
    events.push(state);
    if(step==0 && state.get_str("state")=="error" && !recovered) {recovered=true;BrowserPaneProbe::profile(browser,argv[3]);nav(origin+L"/a");return;}
    if(!attached && state.get_str("state")=="loading") {attached=true;checks.set("bridge_and_host_objects_disabled",BrowserPaneProbe::attach(browser,messages,network));}
    if(state.get_str("title")=="isolated:blocked") probe=true;
    const auto url=state.get_str("url"), title=state.get_str("title"), value=state.get_str("state");
    if(step==0 && value=="ready" && probe) {checks.set("startup_failure_can_retry",recovered).set("native_document_loaded_with_frame_ancestors_none",true);step=1;nav(origin+L"/b");}
    else if(step==1 && value=="ready" && title=="second" && state["can_back"].boolean()) {checks.set("title_and_history",true);step=2;browser->command(Json::object().set("action","back"));}
    else if(step==2 && value=="ready" && title=="isolated:blocked" && state["can_forward"].boolean()) {checks.set("back_navigation",true);step=3;browser->command(Json::object().set("action","forward"));}
    else if(step==3 && value=="ready" && title=="second") {checks.set("forward_navigation",true);step=4;nav(origin+L"/redirect");}
    else if(step==4 && value=="error") {checks.set("redirect_to_service_blocked",true);step=5;browser->command(Json::object().set("action","hide"));browser->command(Json::object().set("action","state"));}
    else if(step==5 && !state["visible"].boolean()) {checks.set("hide_preserves_controller",BrowserPaneProbe::hidden(browser));step=6;browser->command(Json::object().set("action","close"));}
    else if(step==6 && value=="closed") {checks.set("close_acknowledged",true);done=true;}
  };
  nav(origin+L"/a");
  ULONGLONG start=GetTickCount64();
  while(!done && GetTickCount64()-start<30000) {MSG m;while(PeekMessageW(&m,nullptr,0,0,PM_REMOVE)){TranslateMessage(&m);DispatchMessageW(&m);}Sleep(10);}
  browser->close();
  // Asynchronous environment creation after close must not re-open a controller.
  auto* pending=new BrowserPane(window,std::wstring(argv[3])+L"-closed",ports);
  bool reopened=false;
  pending->on_state=[&](const Json& s){if(s.get_str("state")!="starting" && s.get_str("state")!="closed")reopened=true;};
  pending->command(Json::object().set("action","navigate").set("url",utf8(origin+L"/b")));pending->close();
  start=GetTickCount64();while(GetTickCount64()-start<1800){MSG m;while(PeekMessageW(&m,nullptr,0,0,PM_REMOVE)){TranslateMessage(&m);DispatchMessageW(&m);}Sleep(10);}
  checks.set("close_during_start",!reopened).set("navigation_flow_finished",done).set("no_web_messages_delivered",messages==0);
  bool ok=done;for(const auto& p:checks.members())ok=ok&&p.second.boolean();
  report.set("ok",ok).set("checks",checks).set("events",events).set("network_events",network);
  std::ofstream output(utf8(argv[4]));output<<report.dump();output.close();
  DestroyWindow(window);CoUninitialize();
  return ok?0:1;
}
