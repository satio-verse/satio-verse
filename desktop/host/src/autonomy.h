#pragma once
#include <cstdint>
#include <filesystem>
#include <functional>
#include <string>
#include <vector>
#include "json.h"

// Read-only projection of the Python owner's shared policy. This is tool-dispatch
// authorization, not an OS sandbox or protection from another same-account process.
namespace autonomy {
struct Policy {
  bool unrestricted = false;
  bool initialized = false;
  bool scope_verified = false;
  std::uint64_t revision = 0;
  std::string storage_path;
  Json rules = Json::array();
  std::string error;
};
struct Call {
  std::vector<std::string> aliases;  // actual raw, canonical and public native names
  Json args = Json::object();
  std::string workspace;           // actual cwd; empty means unknown, never inferred
  std::string operation = "unknown";  // unknown/read/write, supplied by trusted adapter
  bool recursive = false;          // adapter declares directory/search scope
};

Policy parse(const Json& document);
Policy load(const std::filesystem::path& path);  // tests use a disposable file
Policy load();  // owner .worldmodel/agent/blacklist.json; never writes or initializes it
using FetchTransport = std::function<bool(const std::wstring&, std::string&, unsigned&)>;
Policy fetch(const std::wstring& server, const FetchTransport& transport = {});
bool enabled(const Policy& policy);
bool enabled();
std::string deny(const Policy& policy, const Call& call);
Json guard_config(Json extra, const Policy& policy);
Json guard_config(Json extra);
std::string url_error(const std::string& url);
Json declaration();

// Injectable only for isolated tests. Production defaults to a bounded local HTTP
// request, never writes the policy itself, and exposes only the append-self API.
using SelfTransport = std::function<bool(const std::wstring&, const Json&, std::string&)>;
bool add_self(const std::wstring& server, const Json& args, std::string& result,
              const SelfTransport& transport = {});
}  // namespace autonomy
