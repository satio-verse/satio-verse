// Integrated real WorkbenchWindow, trusted bridge, current frontend and webpage controllers.
// Built only in a disposable output directory by tools/test_native_browser.py.
#include "app.h"
#include "workbench.h"
#include "tools.h"
#include <fstream>
#include <shlwapi.h>

void App::on_workbench(bool) {}
void App::dock_action(const std::string&, const std::string&) {}
void App::send_dock() {}
void App::panel_message(const std::string&, WebViewHost*) {}
void ToolHub::refresh() {}
static void pump() {MSG m;while(PeekMessageW(&m,nullptr,0,0,PM_REMOVE)){TranslateMessage(&m);DispatchMessageW(&m);}Sleep(10);}
static bool until(const std::function<bool()>& check,unsigned ms=5000) {const auto start=GetTickCount64();do {pump();if(check())return true;}while(GetTickCount64()-start<ms);return false;}
struct WorkbenchIntegrationProbe {
  struct HandshakeDelay {bool released=false;std::vector<std::string> pending;};
  static std::shared_ptr<HandshakeDelay> delay_handshake(WorkbenchWindow& w) {
    auto delay=std::make_shared<HandshakeDelay>();auto original=w.web_->on_message;
    w.web_->on_message=[delay,original](const std::string& text){Json msg;if(!delay->released&&text.rfind("workbench ",0)==0&&Json::parse(text.substr(10),msg)&&msg.get_str("op")=="ready")delay->pending.push_back(text);else original(text);};return delay;
  }
  static void release_handshake(WorkbenchWindow& w,const std::shared_ptr<HandshakeDelay>& delay) {delay->released=true;for(const auto& m:delay->pending)w.on_message(m);delay->pending.clear();}
  static Json evaluate(ICoreWebView2* view,const std::wstring& script) {
    if(!view)return Json();
    struct Result {bool done=false;Json value;};auto result=std::make_shared<Result>();
    view->ExecuteScript(script.c_str(),make_handler<ICoreWebView2ExecuteScriptCompletedHandler,HRESULT,LPCWSTR>(IID_ICoreWebView2ExecuteScriptCompletedHandler,[result](HRESULT,LPCWSTR raw){Json::parse(raw?utf8(raw):"null",result->value);result->done=true;return S_OK;}).get());
    until([&]{return result->done;});return result->value;
  }
  static bool ready(WorkbenchWindow& w) {return w.web_&&w.web_->webview_;}
  static Json ui(WorkbenchWindow& w,const std::wstring& js) {return evaluate(w.web_?w.web_->webview_.get():nullptr,js);}
  static BrowserPane* pane(WorkbenchWindow& w,const std::string& id) {auto it=w.browsers_.find(id);return it==w.browsers_.end()?nullptr:it->second.get();}
  static Json page(WorkbenchWindow& w,const std::string& id,const std::wstring& js) {auto* b=pane(w,id);return evaluate(b?b->view_.get():nullptr,js);}
  static bool visible(BrowserPane* b) {BOOL value=FALSE;if(b&&b->controller_)b->controller_->get_IsVisible(&value);return value;}
  static std::string active(WorkbenchWindow& w) {for(const auto& p:w.browsers_)if(p.second.get()==w.browser_)return p.first;return {};}
  static bool loaded(WorkbenchWindow& w,const std::string& id) {auto* b=pane(w,id);return b&&b->view_&&b->state_=="ready";}
  static void send(WorkbenchWindow& w,Json msg) {msg.set("op","browser");ui(w,L"chrome.webview.postMessage('workbench '+JSON.stringify("+widen(msg.dump())+L"))");}
  static Json state(WorkbenchWindow& w) {
    Json result=Json::object(),tabs=Json::array();result.set("active",active(w)).set("count",double(w.browsers_.size()));
    for(const auto& p:w.browsers_) {auto* b=p.second.get();RECT rc{};double zoom=0;if(b->controller_){b->controller_->get_Bounds(&rc);b->controller_->get_ZoomFactor(&zoom);}tabs.push(Json::object().set("id",p.first).set("state",b->state_).set("visible",visible(b)).set("x",double(rc.left)).set("y",double(rc.top)).set("width",double(rc.right-rc.left)).set("height",double(rc.bottom-rc.top)).set("error",b->error_).set("zoom",zoom));}
    return result.set("tabs",tabs);
  }
  static bool lock_zoom(WorkbenchWindow& w) {
    double factor=0;w.web_->controller_->get_ZoomFactor(&factor);const bool reset=factor==1;
    Com<ICoreWebView2Settings> settings;w.web_->webview_->get_Settings(settings.put());BOOL zoom=TRUE,pinch=TRUE;settings->get_IsZoomControlEnabled(&zoom);
    auto s=settings.as<ICoreWebView2Settings5>(IID_ICoreWebView2Settings5);if(s)s->get_IsPinchZoomEnabled(&pinch);return reset&&!zoom&&!pinch;
  }
  static bool capture(WorkbenchWindow& w,const std::string& id,const std::wstring& path) {
    auto* b=pane(w,id);if(!b||!b->view_)return false;Com<IStream> stream;
    if(FAILED(SHCreateStreamOnFileEx(path.c_str(),STGM_CREATE|STGM_WRITE|STGM_SHARE_EXCLUSIVE,FILE_ATTRIBUTE_NORMAL,TRUE,nullptr,stream.put())))return false;
    auto done=std::make_shared<int>(0);b->view_->CapturePreview(COREWEBVIEW2_CAPTURE_PREVIEW_IMAGE_FORMAT_PNG,stream.get(),make_handler<ICoreWebView2CapturePreviewCompletedHandler,HRESULT>(IID_ICoreWebView2CapturePreviewCompletedHandler,[done](HRESULT hr){*done=SUCCEEDED(hr)?1:-1;return S_OK;}).get());until([&]{return *done!=0;});return *done==1;
  }
};
int wmain(int argc,wchar_t** argv) {
  if(argc!=5&&argc!=6)return 2;SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);CoInitializeEx(nullptr,COINIT_APARTMENTTHREADED);
  App app;app.inst=GetModuleHandleW(nullptr);app.opt.server=argv[1];app.opt.web_dir=argv[2];app.opt.workbench_query=L"desk=0";app.opt.services=false;
  WorkbenchWindow window(app);window.open();Json report=Json::object(),checks=Json::object();
  const auto delay=argc==6?WorkbenchIntegrationProbe::delay_handshake(window):nullptr;
  bool ready=until([&]{return WorkbenchIntegrationProbe::ready(window);},15000);checks.set("main_webview_created",ready);
  if(ready) {
    checks.set("actual_browser_button",until([&]{return WorkbenchIntegrationProbe::ui(window,LR"((()=>{const p=document.querySelector('#canvasBrowser');if(p&&!p.hidden&&p.getBoundingClientRect().width>0)return true;const b=document.querySelector('[data-canvas="browser"],[data-act="browser"]');if(!b)return false;b.click();return false;})())").boolean();}));
    WorkbenchIntegrationProbe::ui(window,LR"(window.__browserProbeEvents=[];chrome.webview.addEventListener('message',e=>{if(e.data?.t?.startsWith('workbench-'))window.__browserProbeEvents.push(e.data)}) )");
    const auto submit=[&]{return until([&]{return WorkbenchIntegrationProbe::ui(window,L"(()=>{const i=document.querySelector('#browserUrl');if(!i)return false;i.value="+widen(Json(utf8(argv[3])).dump())+L";i.closest('form').requestSubmit();return true;})()").boolean();});};
    if(delay){checks.set("actual_address_form",submit());checks.set("early_navigation_before_handshake",WorkbenchIntegrationProbe::ui(window,L"!!document.querySelector('.wb-browser-reader')").boolean()&&WorkbenchIntegrationProbe::active(window).empty());WorkbenchIntegrationProbe::release_handshake(window,delay);}
    WorkbenchIntegrationProbe::ui(window,L"chrome.webview.postMessage('workbench '+JSON.stringify({op:'ready'}))");
    checks.set("native_capabilities_received",until([&]{return WorkbenchIntegrationProbe::ui(window,L"window.__browserProbeEvents.some(e=>e.t==='workbench-capabilities'&&e.browser_tabs===true&&e.browser_selection===true)").boolean();}));
    if(!delay)checks.set("actual_address_form",submit());
    until([&]{return !WorkbenchIntegrationProbe::active(window).empty();});const auto first=WorkbenchIntegrationProbe::active(window);
    const bool loaded=until([&]{return WorkbenchIntegrationProbe::loaded(window,first);},18000);
    checks.set("document_loaded",loaded).set("native_controller_visible",WorkbenchIntegrationProbe::visible(WorkbenchIntegrationProbe::pane(window,first)));
    report.set("page",WorkbenchIntegrationProbe::page(window,first,L"({title:document.title,url:location.href,ready:document.readyState,body:document.body?.innerText.slice(0,800),html:document.documentElement.outerHTML.slice(0,5000),width:innerWidth,height:innerHeight})"));
    report.set("dom",WorkbenchIntegrationProbe::ui(window,LR"((()=>{const el=document.querySelector('.wb-browser-native-slot');return {slot:el?{hidden:el.hidden,...el.getBoundingClientRect().toJSON()}:null,occluders:[...document.querySelectorAll('dialog[open],.settings-mode,.menu:not([hidden]),.navigation-menu:not([hidden]),.context-add-menu:not([hidden]),#sideScrim:not([hidden]),#personaPop:not([hidden])')].map(x=>x.outerHTML.slice(0,200)),body:document.querySelector('.wb-browser-status')?.textContent,errors:document.querySelector('.wb-browser-error')?.textContent};})())"));
    report.set("preview_captured",WorkbenchIntegrationProbe::capture(window,first,std::wstring(argv[4])+L".png"));
    checks.set("main_zoom_locked_without_dpi_change",WorkbenchIntegrationProbe::lock_zoom(window));
    if(loaded&&std::wstring(argv[3]).rfind(L"http://127.0.0.1:",0)==0) {
      auto* pane=WorkbenchIntegrationProbe::pane(window,first);
      auto send=[&](const std::string& id,const std::string& action,Json msg=Json::object()){msg.set("tab_id",id).set("action",action);WorkbenchIntegrationProbe::send(window,msg);};
      WorkbenchIntegrationProbe::ui(window,L"document.querySelector('[data-browser-action=more]').click()");
      checks.set("actual_menu_hides_native_overlay",until([&]{return !WorkbenchIntegrationProbe::visible(pane);}));
      WorkbenchIntegrationProbe::ui(window,L"document.querySelector('[data-browser-action=more]').click()");
      checks.set("closing_menu_restores_native_overlay",until([&]{return WorkbenchIntegrationProbe::visible(pane);}));
      WorkbenchIntegrationProbe::page(window,first,L"document.querySelector('#persist').value='retained across tabs';let r=document.createRange();r.selectNodeContents(document.querySelector('#selection'));let s=getSelection();s.removeAllRanges();s.addRange(r);true");
      send(first,"selection",Json::object().set("request_id","selection-test"));
      checks.set("selected_text_reply_has_tab_and_source",until([&]{return WorkbenchIntegrationProbe::ui(window,L"window.__browserProbeEvents.some(e=>e.t==='workbench-browser-selection'&&e.request_id==='selection-test'&&e.tab_id==="+widen(Json(first).dump())+L"&&e.text==='Selected fixture text'&&e.url.includes('/a'))").boolean();}));
      send(first,"zoom",Json::object().set("factor",1.25));checks.set("webpage_zoom_independent",until([&]{return WorkbenchIntegrationProbe::ui(window,L"window.__browserProbeEvents.some(e=>e.tab_id==="+widen(Json(first).dump())+L"&&e.zoom_factor===1.25)").boolean();}));
      std::wstring base=argv[3];base=base.substr(0,base.find(L'/',7));
      auto address=[&](const std::wstring& url){WorkbenchIntegrationProbe::ui(window,L"(()=>{const i=document.querySelector('#browserUrl');i.value="+widen(Json(utf8(url)).dump())+L";i.closest('form').requestSubmit();})()");};
      WorkbenchIntegrationProbe::ui(window,L"document.querySelector('[data-browser-action=plus]').click()");address(base+L"/b");
      until([&]{return !WorkbenchIntegrationProbe::active(window).empty()&&WorkbenchIntegrationProbe::active(window)!=first;});const auto second=WorkbenchIntegrationProbe::active(window);
      checks.set("second_tab_loaded",second!=first&&until([&]{return WorkbenchIntegrationProbe::loaded(window,second);}));checks.set("only_active_tab_visible",!WorkbenchIntegrationProbe::visible(pane)&&WorkbenchIntegrationProbe::visible(WorkbenchIntegrationProbe::pane(window,second)));
      address(base+L"/c");until([&]{return WorkbenchIntegrationProbe::page(window,second,L"location.pathname").str()=="/c";});WorkbenchIntegrationProbe::ui(window,L"document.querySelector('[data-browser-action=back]').click()");
      checks.set("tab_history_independent",until([&]{return WorkbenchIntegrationProbe::page(window,second,L"location.pathname").str()=="/b";})&&WorkbenchIntegrationProbe::page(window,first,L"location.pathname").str()=="/a");
      address(base+L"/http-error");
      checks.set("http_error_reported_instead_of_blank",until([&]{return WorkbenchIntegrationProbe::ui(window,L"window.__browserProbeEvents.some(e=>e.tab_id==="+widen(Json(second).dump())+L"&&e.state==='error'&&e.http_status===412&&e.error.includes('412'))").boolean();}));
      WorkbenchIntegrationProbe::ui(window,L"document.querySelector('[data-tab=\""+widen(first)+L"\"]').click()");
      checks.set("tab_dom_state_preserved",WorkbenchIntegrationProbe::page(window,first,L"document.querySelector('#persist').value").str()=="retained across tabs");
      WorkbenchIntegrationProbe::send(window,Json::object().set("action","hide"));checks.set("global_hide_occludes_all_tabs",!WorkbenchIntegrationProbe::visible(pane)&&!WorkbenchIntegrationProbe::visible(WorkbenchIntegrationProbe::pane(window,second)));
      send(first,"activate");checks.set("activate_restores_visibility",WorkbenchIntegrationProbe::visible(pane));
      WorkbenchIntegrationProbe::ui(window,L"document.querySelector('[data-close-tab=\""+widen(second)+L"\"]').click()");
      checks.set("close_one_preserves_other",until([&]{return !WorkbenchIntegrationProbe::pane(window,second);})&&WorkbenchIntegrationProbe::pane(window,first)==pane);
      const Json reveal=Json::object().set("op","reveal_file").set("rid","outside-workspace").set("workspace",utf8(argv[2])).set("path","C:\\Windows\\notepad.exe");
      WorkbenchIntegrationProbe::ui(window,L"chrome.webview.postMessage('workbench '+JSON.stringify("+widen(reveal.dump())+L"))");
      checks.set("reveal_outside_workspace_rejected",until([&]{return WorkbenchIntegrationProbe::ui(window,L"window.__browserProbeEvents.some(e=>e.t==='workbench-file-revealed'&&e.rid==='outside-workspace'&&e.ok===false)").boolean();}));
    }
    report.set("native",WorkbenchIntegrationProbe::state(window));
  }
  bool ok=true;for(const auto& p:checks.members())ok=ok&&p.second.boolean();report.set("ok",ok).set("checks",checks);
  std::ofstream output(utf8(argv[4]));output<<report.dump();output.close();window.close();const auto end=GetTickCount64();while(GetTickCount64()-end<1000)pump();CoUninitialize();return ok?0:1;
}
