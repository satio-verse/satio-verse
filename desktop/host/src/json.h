// 够用的小 JSON 库 (解析 + 生成), 给本机 API / MCP 用: 对象保持插入顺序, 字符串一律 UTF-8, 数字用 double.
// 不引第三方库 (下载要用户确认, 这点东西自己写就够了).
#pragma once
#include <string>
#include <utility>
#include <vector>

class Json {
 public:
  enum Type { Null, Bool, Number, String, Array, Object };

  Json() = default;
  Json(std::nullptr_t) {}
  Json(bool v) : t_(Bool), b_(v) {}
  Json(int v) : t_(Number), n_(v) {}
  Json(double v) : t_(Number), n_(v) {}
  Json(const char* v) : t_(String), s_(v) {}
  Json(std::string v) : t_(String), s_(std::move(v)) {}
  static Json array() { Json j; j.t_ = Array; return j; }
  static Json object() { Json j; j.t_ = Object; return j; }

  static bool parse(const std::string& text, Json& out, std::string* error = nullptr);
  std::string dump() const;

  Type type() const { return t_; }
  bool is_object() const { return t_ == Object; }
  bool is_array() const { return t_ == Array; }
  bool is_string() const { return t_ == String; }
  bool is_null() const { return t_ == Null; }

  bool boolean(bool def = false) const { return t_ == Bool ? b_ : def; }
  double number(double def = 0) const { return t_ == Number ? n_ : def; }
  const std::string& str() const { return s_; }
  const std::vector<Json>& items() const { return a_; }
  const std::vector<std::pair<std::string, Json>>& members() const { return o_; }

  // 对象取值: 没有这个键 (或不是对象) 时返回 null
  const Json& operator[](const std::string& key) const;
  bool has(const std::string& key) const;
  std::string get_str(const std::string& key, const std::string& def = "") const;

  Json& set(const std::string& key, Json v);  // 已有的键覆盖
  Json& push(Json v);
  // 可写访问 (改嵌套的值用): at 在没有这个键时加一个 null; 不叫 operator[], 免得读的时候顺手加了键
  Json& at(const std::string& key);
  std::vector<Json>& mut_items() { return a_; }
  bool erase(const std::string& key);

 private:
  Type t_ = Null;
  bool b_ = false;
  double n_ = 0;
  std::string s_;
  std::vector<Json> a_;
  std::vector<std::pair<std::string, Json>> o_;
  friend class JsonParser;
};
