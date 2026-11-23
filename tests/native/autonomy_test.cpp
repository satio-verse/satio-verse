#include "autonomy.h"
#include <windows.h>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>

namespace {
int checks=0;
void check(bool ok,const std::string& message) {
  ++checks;
  if(!ok)throw std::runtime_error(message);
}
Json rule(std::string kind,std::string value,std::string operations="all") {
  Json r=Json::object();
  r.set("id","1234567890abcdef1234567890abcdef").set("kind",kind).set("value",value).set("reason","isolated test")
   .set("enabled",true).set("operations",operations).set("source","user").set("evidence","fixture")
   .set("created",1234.5).set("updated",1234.5).set("origin","custom")
   .set("enforcement",kind=="instruction"?"instruction":"tool_arguments");
  return r;
}
Json document(Json r=Json()) {
  Json rules=Json::array();if(!r.is_null())rules.push(r);
  Json d=Json::object();d.set("version",1).set("revision",1).set("global_unrestricted",true).set("rules",rules).set("audit",Json::array());
  return d;
}
Json arg(const std::string& key,const Json& value) {Json j=Json::object();j.set(key,value);return j;}
autonomy::Call call(const Json& args=Json::object(),std::string operation="unknown",std::string workspace="") {
  autonomy::Call c;c.aliases={"mcp__windows__FileSystem","FileSystem","native.FileSystem","read_file","agent.read_file"};
  c.args=args;c.workspace=workspace;c.operation=operation;return c;
}
bool blocked(const Json& r,const autonomy::Call& c) {
  const auto p=autonomy::parse(document(r));check(p.error.empty(),"test rule must be valid: "+p.error);
  return !autonomy::deny(p,c).empty();
}
std::string read(const std::filesystem::path& path) {
  std::ifstream in(path,std::ios::binary);return {std::istreambuf_iterator<char>(in),std::istreambuf_iterator<char>()};
}
void write(const std::filesystem::path& path,const std::string& raw) {std::ofstream out(path,std::ios::binary);out<<raw;}
}

int main(int argc,char** argv) {
  // This directory is the only filesystem mutation; never reads a real profile.
  const auto temp=std::filesystem::temp_directory_path()/("wm-autonomy-test-"+std::to_string(GetCurrentProcessId())+"-"+std::to_string(GetTickCount64()));
  try {
    check(!std::filesystem::exists(temp),"unique test directory");std::filesystem::create_directory(temp);
    const auto home=temp/"custom-agent-home";std::filesystem::create_directory(home);
    const auto file=home/"blacklist.json";
    auto base=document();auto p=autonomy::parse(base);
    check(p.initialized&&p.error.empty()&&p.unrestricted,"parse initialized policy");
    check(!p.scope_verified&&!autonomy::enabled(p),"unverified parse cannot authorize auto");
    auto legacy=base;legacy.erase("global_unrestricted");legacy.erase("revision");
    p=autonomy::parse(legacy);check(p.initialized&&!p.unrestricted&&p.revision==0,"legacy fields remain restricted");
    auto noaudit=base;noaudit.erase("audit");check(autonomy::parse(noaudit).error.empty(),"execution snapshot need not carry audit");
    for(const auto& key:{"version","rules"}) {auto bad=base;bad.erase(key);check(!autonomy::parse(bad).error.empty(),std::string("missing ")+key);}
    for(const auto& key:{"id","kind","value","reason","enabled","operations","source","evidence","created","updated","origin","enforcement"}) {
      auto r=rule("tool","read_file");r.erase(key);check(!autonomy::parse(document(r)).error.empty(),std::string("missing rule ")+key);
    }
    for(auto badkind:{"process","text","guidance","unknown"})check(!autonomy::parse(document(rule(badkind,"x"))).error.empty(),"unknown shared kind");
    for(auto invalid:{Json(),Json(true),Json(-1),Json(1.5),Json("2"),Json(9007199254740992.0)}) {
      auto d=base;d.set("revision",invalid);check(!autonomy::parse(d).error.empty(),"invalid revision");
    }
    auto d=base;d.set("global_unrestricted","true");check(!autonomy::parse(d).error.empty(),"invalid boolean");
    auto duplicate=document(rule("tool","read_file"));duplicate.at("rules").push(rule("tool","write_file"));check(!autonomy::parse(duplicate).error.empty(),"duplicate IDs");
    auto r=rule("command","format");r.set("enabled",1);check(!autonomy::parse(document(r)).error.empty(),"enabled is boolean only");
    r=rule("command",std::string("bad\xc0\xaf",5));check(!autonomy::parse(document(r)).error.empty(),"invalid UTF8 persisted value");
    r=rule("instruction","do not infer consent");r.set("enforcement","tool_arguments");check(!autonomy::parse(document(r)).error.empty(),"advisory cannot become enforced");
    r=rule("tool","[ab]");check(!autonomy::parse(document(r)).error.empty(),"unsupported glob syntax rejected");
    r=rule("path","relative");check(!autonomy::parse(document(r)).error.empty(),"relative stored path rejected");
    r=rule("domain","example.com.evil/route");check(!autonomy::parse(document(r)).error.empty(),"invalid domain rule");

    p=autonomy::load(file);check(!p.initialized&&!autonomy::enabled(p)&&p.error.empty(),"missing install never auto opens");
    write(file,base.dump());p=autonomy::load(file);check(p.initialized&&!autonomy::enabled(p),"local file does not verify service scope");
    write(file,"{broken");p=autonomy::load(file);check(!p.error.empty()&&!autonomy::deny(p,call()).empty(),"damaged file rejects dispatch");
    write(file,std::string(2000001,' '));check(!autonomy::load(file).error.empty(),"oversized file rejects");
    check(!autonomy::load(home).error.empty(),"directory is not a policy file");

    auto api=base;api.set("scope","native-tools").set("enforcement","tool_arguments").set("storage_path",file.u8string());
    int fetches=0;
    auto fetcher=[&](const std::wstring& endpoint,std::string& response,unsigned& status){++fetches;check(endpoint==L"http://127.0.0.1:8894/api/agent/permissions?execution=1","execution GET endpoint");response=api.dump();status=200;return true;};
    p=autonomy::fetch(L"http://127.0.0.1:8894",fetcher);
    check(p.scope_verified&&autonomy::enabled(p)&&p.storage_path==file.u8string(),"actual custom service storage verifies scope");
    check(!autonomy::fetch(L"https://example.com",fetcher).error.empty()&&fetches==1,"remote service rejected before transport");
    check(!autonomy::fetch(L"http://user@localhost:8894",fetcher).error.empty(),"credentials rejected before transport");
    auto get=[&](Json response,unsigned code=200,bool ok=true){return autonomy::fetch(L"http://localhost:8894",[=](const std::wstring&,std::string& out,unsigned& status){out=response.dump();status=code;return ok;});};
    for(const auto& key:{"scope","enforcement","storage_path"}){auto bad=api;bad.erase(key);check(!get(bad).error.empty(),std::string("missing API ")+key);}
    auto bad=api;bad.set("scope","workbench");check(!get(bad).error.empty(),"wrong scope");
    bad=api;bad.set("storage_path","relative/blacklist.json");check(!get(bad).error.empty(),"relative authoritative path");
    bad=api;bad.set("storage_path",(home/"prefs.json").u8string());check(!get(bad).error.empty(),"wrong storage file");
    check(!get(api,404).error.empty()&&!get(api,200,false).error.empty(),"legacy/failed endpoint rejects");
    check(!autonomy::fetch(L"http://localhost:8894",[](const std::wstring&,std::string& raw,unsigned& status){raw="not json";status=200;return true;}).error.empty(),"malformed response rejects");

    check(blocked(rule("tool","agent.read_*"),call()),"canonical alias blocked");
    check(blocked(rule("tool","MCP__WINDOWS__?ileSystem"),call()),"raw namespaced alias blocked");
    check(!blocked(rule("tool","write_file"),call()),"different tool permitted");
    r=rule("tool","*");r.set("enabled",false);check(!blocked(r,call()),"disabled user rule stays disabled");
    check(!blocked(rule("instruction","never read_file"),call()),"instructions remain advisory");
    check(blocked(rule("command","format"),call(arg("command","FORMAT C:"))),"literal command blocked");
    check(!blocked(rule("command","format"),call(arg("command","Format-Table value"))),"format is not Format-Table");
    check(!blocked(rule("command","format"),call(arg("command","reformat format.exe 格式format"))),"token edge exactness");
    check(blocked(rule("command","reg add"),call(arg("script","REG\t\n ADD HKCU"))),"whitespace collapsed");
    check(blocked(rule("command","strasse"),call(arg("code",u8"STRAßE"))),"full Unicode sharp-s casefold");
    check(blocked(rule("command",u8"σ"),call(arg("script",u8"ς"))),"Greek final sigma casefold");
    check(blocked(rule("command",u8"删除 数据"),call(arg("script",u8"删除\u3000数据"))),"Unicode whitespace");
    check(!blocked(rule("command",u8"删除"),call(arg("script",u8"请删除文件"))),"Unicode word boundary");

    const auto denied=(temp/"blocked").u8string(),sibling=(temp/"blocked-copy").u8string();
    check(blocked(rule("path",denied),call(arg("path",denied+"/secret.txt"),"read")),"path descendant");
    check(blocked(rule("path",denied),call(arg("executable",denied+"/app.exe"))),"explicit executable path is inspected");
    check(!blocked(rule("path",denied),call(arg("path",sibling+"/file.txt"),"read")),"sibling prefix distinct");
    check(blocked(rule("path",denied),call(arg("path","blocked/secret.txt"),"read",temp.u8string())),"relative path uses actual cwd");
    check(blocked(rule("path",denied),call(arg("path","elsewhere.txt"),"read")),"unknown relative cwd rejects");
    check(!blocked(rule("path",denied,"write"),call(arg("path",denied+"/file"),"read")),"write rule allows explicit read");
    check(blocked(rule("path",denied,"write"),call(arg("path",denied+"/file"))),"unknown operation matches both");
    auto recursive=call(arg("path",temp.u8string()),"read");
    check(!blocked(rule("path",denied),recursive),"nonrecursive ancestor not automatically denied");
    recursive.recursive=true;check(blocked(rule("path",denied),recursive),"recursive ancestor denied");
    recursive.args=Json::object();recursive.workspace=temp.u8string();check(blocked(rule("path",denied),recursive),"implicit recursive root checked");
    check(blocked(rule("path",denied),call(arg("command","Get-ChildItem"),"unknown",denied)),"implicit shell cwd checked");
    check(blocked(rule("path",denied),call(arg("command","Get-ChildItem"))),"unknown shell cwd rejects");
    check(blocked(rule("path",denied),call(arg("command","Get-Content \""+denied+"/file\""),"unknown",temp.u8string())),"literal command path checked");
    check(!blocked(rule("path",denied),call(arg("command","Get-Content \""+sibling+"/file\""),"unknown",temp.u8string())),"literal path prefix boundary");
    SetEnvironmentVariableW(L"WM_AUTONOMY_TEST_DIR",std::filesystem::path(denied).wstring().c_str());
    check(blocked(rule("path",denied),call(arg("path","$ENV:WM_AUTONOMY_TEST_DIR/file"),"read")),"PowerShell env case insensitive");
    SetEnvironmentVariableW(L"WM_AUTONOMY_TEST_DIR",nullptr);

    check(blocked(rule("domain","example.com"),call(arg("url","https://sub.example.com./foo"))),"domain self and child trailing dot");
    check(!blocked(rule("domain","example.com"),call(arg("url","https://example.com.evil/foo"))),"deceptive suffix not matched");
    check(!blocked(rule("domain","example.com"),call(arg("url","https://notexample.com"))),"domain prefix boundary");
    check(blocked(rule("domain","xn--bcher-kva.example"),call(arg("query",u8"ß see https://BÜCHER.example/page"))),"IDNA embedded URL after expanded fold");
    check(blocked(rule("domain","fass.de"),call(arg("url",u8"https://faß.de"))),"IDNA sharp-s Python2003 parity");
    check(blocked(rule("domain","example.com"),call(arg("url","https://user@example.com/"))),"credential URL conservatively rejected");
    check(!autonomy::url_error("file:///C:/x").empty()&&!autonomy::url_error("https://u:p@example.com").empty(),"open_url rejects nonHTTP/credentials");
    check(autonomy::url_error("https://example.com/a?b=1").empty(),"valid HTTP URL");
    for(const auto& url:{u8"https://中文.example/",u8"https://日本語.example/",u8"https://παράδειγμα.example/",u8"https://مثال.example/",u8"https://BÜCHER.example/",u8"https://faß.de/"})
      check(autonomy::url_error(url).empty(),std::string("ordinary IDN is accepted, not just conservatively denied: ")+url);
    check(!autonomy::url_error(u8"https://xᏸx.example/").empty(),"known Python/Windows IDNA disagreement rejects");

    auto empty=autonomy::parse(base);
    auto nested=arg("command","format");for(int i=0;i<9;++i)nested=arg("a",nested);
    check(!autonomy::deny(empty,call(nested)).empty(),"deep argument rejects instead of truncating");
    Json many=Json::array();for(int i=0;i<512;++i)many.push(i);check(!autonomy::deny(empty,call(arg("many",many))).empty(),"node count rejects");
    auto strings=arg("a",std::string(100001,'x'));strings.set("b",std::string(100000,'y'));
    check(!autonomy::deny(empty,call(strings)).empty(),"cumulative character budget rejects");
    check(!autonomy::deny(empty,call(arg("n",std::numeric_limits<double>::infinity()))).empty(),"nonfinite args rejected");
    check(!autonomy::deny(empty,call(arg("x",std::string("\xc0\xaf",2)))).empty(),"malformed UTF8 args reject");
    check(!autonomy::deny(empty,call(Json::array())).empty(),"args must be object");
    check(!autonomy::deny(empty,call(Json::object(),"guess")).empty(),"unknown DTO operation spelling rejected");
    auto noalias=call();noalias.aliases.clear();check(!autonomy::deny(empty,noalias).empty(),"missing trusted tool identity rejects");
    Json extra=arg("processes",Json::array().push("keep.exe"));extra.set("words",Json::array().push("keep word"));
    const auto verified=get(api);auto cfg=autonomy::guard_config(extra,verified);
    check(cfg["_unrestricted"].boolean()&&cfg["processes"].dump()==extra["processes"].dump()&&cfg["words"].dump()==extra["words"].dump(),"guard retains native extra lists");
    cfg=autonomy::guard_config(extra,empty);check(!cfg["_unrestricted"].boolean(),"guard needs scope verification");
    check(autonomy::declaration()["parameters"]["properties"]["kind"]["enum"].items().size()==5,"exact five self kinds");

    int posts=0;std::string result;auto self=arg("kind","command");self.set("value","format").set("reason","explicit correction").set("evidence","owner asked");
    auto post=[&](const std::wstring& endpoint,const Json& body,std::string& response){
      ++posts;check(endpoint==L"http://localhost:8894/api/agent/permissions/learn","append-only endpoint");
      check(body["rule"].members().size()==4&&!body["rule"].has("source")&&body.get_str("evidence")=="owner asked","body does not forge metadata");
      auto saved=rule("command","format");saved.set("source","assistant").set("reason","explicit correction").set("evidence","owner asked");
      Json reply=arg("rule",saved);auto state=document(saved);state.set("revision",2);reply.set("snapshot",state);response=reply.dump();return true;
    };
    check(autonomy::add_self(L"http://localhost:8894",self,result,post),"self append with matching receipt");
    Json saved;check(Json::parse(result,saved)&&saved["saved"].boolean()&&saved["revision"].number()==2,"receipt reports committed revision");
    for(const char* key:{"source","enabled","global_unrestricted","rule_id","mode"}) {auto invalid=self;invalid.set(key,true);check(!autonomy::add_self(L"http://localhost:8894",invalid,result,post),"extra privilege field rejected");}
    auto invalid=self;invalid.set("operations",false);check(!autonomy::add_self(L"http://localhost:8894",invalid,result,post),"wrong operation type rejected before HTTP");
    invalid=self;invalid.set("kind","process");check(!autonomy::add_self(L"http://localhost:8894",invalid,result,post),"unknown self kind not downgraded");
    invalid=self;invalid.set("kind","path").set("value","relative");check(!autonomy::add_self(L"http://localhost:8894",invalid,result,post),"invalid path rejected before HTTP");
    check(posts==1,"invalid proposals did not reach writer");
    check(!autonomy::add_self(L"http://localhost:8894",self,result,[](const std::wstring&,const Json&,std::string& response){response="user-disabled duplicate";return false;}),"server refusal cannot claim save");
    check(!autonomy::add_self(L"http://localhost:8894",self,result,[](const std::wstring&,const Json&,std::string& response){response="{}";return true;}),"missing receipt cannot claim save");
    check(!autonomy::add_self(L"http://localhost:8894",self,result,[](const std::wstring&,const Json&,std::string& response){auto r=rule("command","format");Json x=arg("rule",r);r.set("enabled",false);x.set("snapshot",document(r));response=x.dump();return true;}),"disabled inconsistent receipt rejects");

    if(argc>1) {
      Json fixtures;check(Json::parse(read(std::filesystem::u8path(argv[1])),fixtures)&&fixtures.is_array(),"Unicode parity fixture parsed");
      for(const auto& row:fixtures.items()) {
        check(row.is_array()&&row.items().size()==2,"Unicode fixture pair");
        check(blocked(rule("command",row.items()[0].str()),call(arg("command",row.items()[1]))),"Python Unicode fold parity");
      }
    }
    if(argc>2) {
      Json fixtures;check(Json::parse(read(std::filesystem::u8path(argv[2])),fixtures)&&fixtures.is_array(),"IDNA parity fixture parsed");
      for(const auto& row:fixtures.items()) {
        check(row.is_array()&&row.items().size()==2,"IDNA fixture pair");
        check(blocked(rule("domain",row.items()[1].str()),call(arg("url","https://"+row.items()[0].str()+"/"))),"Python IDNA match or conservative rejection: "+row.items()[0].str());
      }
    }
    std::filesystem::remove_all(temp);
    std::cout<<"PASS "<<checks<<" isolated assertions; no real profile, API, MCP, model, or UI used\n";
    return 0;
  } catch(const std::exception& e) {
    std::cerr<<"FAIL after "<<checks<<" assertions: "<<e.what()<<"\n";
    std::error_code ec;std::filesystem::remove_all(temp,ec);return 1;
  }
}
