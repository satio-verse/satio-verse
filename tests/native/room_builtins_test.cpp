#include "builtins.h"
#include <cassert>
#include <iostream>
#include <limits>
#include <stdexcept>

// Link-only isolation stubs. Any accidental filesystem/OS helper invocation fails.
namespace settings { std::wstring path() { throw std::logic_error("unexpected settings access"); } }
namespace autonomy { std::string url_error(const std::string&) { throw std::logic_error("unexpected URL action"); } }
bool read_file(const std::wstring&, std::vector<char>&) { throw std::logic_error("unexpected read"); }
void logf(const char*, ...) { throw std::logic_error("unexpected action log"); }
std::wstring widen(const std::string&) { throw std::logic_error("unexpected OS action"); }

int main() {
  assert(builtins::known("room_capabilities"));
  assert(builtins::group("room_capabilities") == "room_do");
  assert(!builtins::room_result_ok(true, "已完成咖啡"));
  assert(!builtins::room_result_ok(true, R"({"ok":true,"started":true,"completed":false})"));
  assert(!builtins::room_result_ok(true, R"({"ok":true,"completed":true})"));
  assert(!builtins::room_result_ok(false, R"({"ok":true,"started":true,"completed":true})"));
  assert(builtins::room_result_ok(true, R"({"ok":true,"started":true,"completed":true,"detail":{"known":false,"text":"未知"}})"));
  Json empty = Json::object();
  assert(builtins::room_timeout_ms(empty) == 180000);
  Json args = Json::object();
  args.set("timeout_s", 480);
  assert(builtins::room_timeout_ms(args) == 180000);
  args.set("timeout_s", 60);
  assert(builtins::room_timeout_ms(args) == 60000);
  args.set("timeout_s", -1);
  assert(builtins::room_timeout_ms(args) == 1000);
  args.set("timeout_s", std::numeric_limits<double>::quiet_NaN());
  assert(builtins::room_timeout_ms(args) == 180000);

  Json decl = builtins::declare(empty, empty);
  bool query = false, action = false;
  for (const auto& tool : decl.items()) {
    if (tool.get_str("name") == "room_capabilities") query = true;
    if (tool.get_str("name") != "room_do") continue;
    action = true;
    const auto& props = tool["parameters"]["properties"];
    assert(props["timeout_s"]["maximum"].number() == 180);
    bool phone = false, chair = false, temperature = false;
    for (const auto& id : props["act"]["enum"].items()) {
      phone |= id.str() == "phone";
      chair |= id.str() == "chair_turn";
      temperature |= id.str() == "inspect_temperature";
    }
    assert(phone && chair && temperature);
    assert(tool.get_str("description").find("已排队不是完成") != std::string::npos);
  }
  assert(query && action);
  Json settings = Json::object(), permissions = Json::object();
  permissions.set("room_do", "off"); settings.set("builtins", permissions);
  assert(builtins::permission(settings, "room_capabilities") == "off");
  auto disabled = builtins::declare(settings, empty);
  for (const auto& tool : disabled.items()) {
    assert(tool.get_str("name") != "room_do");
    assert(tool.get_str("name") != "room_capabilities");
  }
  std::cout << "room builtin contract assertions passed\n";
}
