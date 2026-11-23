#include "json.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {
const Json kNull;

void put_utf8(std::string& out, unsigned cp) {
  if (cp < 0x80) {
    out += char(cp);
  } else if (cp < 0x800) {
    out += char(0xC0 | (cp >> 6));
    out += char(0x80 | (cp & 0x3F));
  } else if (cp < 0x10000) {
    out += char(0xE0 | (cp >> 12));
    out += char(0x80 | ((cp >> 6) & 0x3F));
    out += char(0x80 | (cp & 0x3F));
  } else {
    out += char(0xF0 | (cp >> 18));
    out += char(0x80 | ((cp >> 12) & 0x3F));
    out += char(0x80 | ((cp >> 6) & 0x3F));
    out += char(0x80 | (cp & 0x3F));
  }
}

void dump_string(std::string& out, const std::string& s) {
  out += '"';
  for (unsigned char c : s) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      default:
        if (c < 0x20) {
          char b[8];
          snprintf(b, sizeof b, "\\u%04x", c);
          out += b;
        } else {
          out += char(c);  // 其余 UTF-8 原样输出
        }
    }
  }
  out += '"';
}

void dump(std::string& out, const Json& j) {
  switch (j.type()) {
    case Json::Null: out += "null"; break;
    case Json::Bool: out += j.boolean() ? "true" : "false"; break;
    case Json::Number: {
      double n = j.number();
      char b[32];
      if (std::isfinite(n) && n == std::floor(n) && std::fabs(n) < 1e15) snprintf(b, sizeof b, "%.0f", n);
      else if (std::isfinite(n)) snprintf(b, sizeof b, "%.10g", n);
      else snprintf(b, sizeof b, "null");
      out += b;
      break;
    }
    case Json::String: dump_string(out, j.str()); break;
    case Json::Array: {
      out += '[';
      bool first = true;
      for (const Json& v : j.items()) {
        if (!first) out += ',';
        first = false;
        dump(out, v);
      }
      out += ']';
      break;
    }
    case Json::Object: {
      out += '{';
      bool first = true;
      for (const auto& [k, v] : j.members()) {
        if (!first) out += ',';
        first = false;
        dump_string(out, k);
        out += ':';
        dump(out, v);
      }
      out += '}';
      break;
    }
  }
}
}  // namespace

class JsonParser {
 public:
  explicit JsonParser(const std::string& s) : s_(s) {}

  bool parse(Json& out, std::string* error) {
    bool ok = value(out, 0);
    ws();
    if (ok && i_ != s_.size()) ok = fail("JSON 后面还有多余的内容");
    if (!ok && error) *error = err_;
    return ok;
  }

 private:
  bool fail(const char* why) {
    if (err_.empty()) err_ = std::string(why) + " (位置 " + std::to_string(i_) + ")";
    return false;
  }
  void ws() {
    while (i_ < s_.size() && (s_[i_] == ' ' || s_[i_] == '\t' || s_[i_] == '\n' || s_[i_] == '\r')) i_++;
  }
  bool lit(const char* w) {
    size_t n = strlen(w);
    if (s_.compare(i_, n, w) != 0) return false;
    i_ += n;
    return true;
  }
  bool hex4(unsigned& v) {
    if (i_ + 4 > s_.size()) return false;
    v = 0;
    for (int k = 0; k < 4; k++) {
      char c = s_[i_++];
      v <<= 4;
      if (c >= '0' && c <= '9') v |= c - '0';
      else if (c >= 'a' && c <= 'f') v |= c - 'a' + 10;
      else if (c >= 'A' && c <= 'F') v |= c - 'A' + 10;
      else return false;
    }
    return true;
  }
  bool string(std::string& out) {
    if (s_[i_] != '"') return fail("应为字符串");
    i_++;
    while (i_ < s_.size()) {
      char c = s_[i_++];
      if (c == '"') return true;
      if (c != '\\') {
        out += c;
        continue;
      }
      if (i_ >= s_.size()) break;
      char e = s_[i_++];
      switch (e) {
        case '"': out += '"'; break;
        case '\\': out += '\\'; break;
        case '/': out += '/'; break;
        case 'b': out += '\b'; break;
        case 'f': out += '\f'; break;
        case 'n': out += '\n'; break;
        case 'r': out += '\r'; break;
        case 't': out += '\t'; break;
        case 'u': {
          unsigned cp;
          if (!hex4(cp)) return fail("\\u 转义不完整");
          if (cp >= 0xD800 && cp <= 0xDBFF && s_.compare(i_, 2, "\\u") == 0) {  // 代理对
            i_ += 2;
            unsigned lo;
            if (!hex4(lo) || lo < 0xDC00 || lo > 0xDFFF) return fail("代理对不完整");
            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
          }
          put_utf8(out, cp);
          break;
        }
        default: return fail("未知的转义");
      }
    }
    return fail("字符串没有结束");
  }
  bool value(Json& out, int depth) {
    if (depth > 64) return fail("嵌套太深");
    ws();
    if (i_ >= s_.size()) return fail("意外结束");
    char c = s_[i_];
    if (c == '{') {
      i_++;
      out = Json::object();
      ws();
      if (i_ < s_.size() && s_[i_] == '}') { i_++; return true; }
      for (;;) {
        ws();
        std::string key;
        if (i_ >= s_.size() || !string(key)) return fail("应为键名");
        ws();
        if (i_ >= s_.size() || s_[i_++] != ':') return fail("应为冒号");
        Json v;
        if (!value(v, depth + 1)) return false;
        out.o_.emplace_back(std::move(key), std::move(v));
        ws();
        if (i_ < s_.size() && s_[i_] == ',') { i_++; continue; }
        if (i_ < s_.size() && s_[i_] == '}') { i_++; return true; }
        return fail("对象没有正确结束");
      }
    }
    if (c == '[') {
      i_++;
      out = Json::array();
      ws();
      if (i_ < s_.size() && s_[i_] == ']') { i_++; return true; }
      for (;;) {
        Json v;
        if (!value(v, depth + 1)) return false;
        out.a_.push_back(std::move(v));
        ws();
        if (i_ < s_.size() && s_[i_] == ',') { i_++; continue; }
        if (i_ < s_.size() && s_[i_] == ']') { i_++; return true; }
        return fail("数组没有正确结束");
      }
    }
    if (c == '"') {
      std::string s;
      if (!string(s)) return false;
      out = Json(std::move(s));
      return true;
    }
    if (lit("true")) { out = Json(true); return true; }
    if (lit("false")) { out = Json(false); return true; }
    if (lit("null")) { out = Json(); return true; }
    if (c == '-' || (c >= '0' && c <= '9')) {
      const char* begin = s_.c_str() + i_;
      char* end = nullptr;
      double n = strtod(begin, &end);
      if (end == begin) return fail("数字格式不对");
      i_ += size_t(end - begin);
      out = Json(n);
      return true;
    }
    return fail("无法识别的值");
  }

  const std::string& s_;
  size_t i_ = 0;
  std::string err_;
};

bool Json::parse(const std::string& text, Json& out, std::string* error) {
  return JsonParser(text).parse(out, error);
}

std::string Json::dump() const {
  std::string out;
  ::dump(out, *this);
  return out;
}

const Json& Json::operator[](const std::string& key) const {
  if (t_ == Object)
    for (const auto& [k, v] : o_)
      if (k == key) return v;
  return kNull;
}

bool Json::has(const std::string& key) const {
  if (t_ == Object)
    for (const auto& kv : o_)
      if (kv.first == key) return true;
  return false;
}

std::string Json::get_str(const std::string& key, const std::string& def) const {
  const Json& v = (*this)[key];
  return v.is_string() ? v.str() : def;
}

Json& Json::set(const std::string& key, Json v) {
  if (t_ != Object) *this = object();
  for (auto& kv : o_)
    if (kv.first == key) {
      kv.second = std::move(v);
      return *this;
    }
  o_.emplace_back(key, std::move(v));
  return *this;
}

Json& Json::at(const std::string& key) {
  if (t_ != Object) *this = object();
  for (auto& kv : o_)
    if (kv.first == key) return kv.second;
  o_.emplace_back(key, Json());
  return o_.back().second;
}

bool Json::erase(const std::string& key) {
  for (auto it = o_.begin(); it != o_.end(); ++it)
    if (it->first == key) {
      o_.erase(it);
      return true;
    }
  return false;
}

Json& Json::push(Json v) {
  if (t_ != Array) *this = array();
  a_.push_back(std::move(v));
  return *this;
}
