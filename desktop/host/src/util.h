// 宿主的小工具: 日志、编码转换、base64、命中掩码, 以及不依赖 WRL 的 COM 智能指针和回调对象
// (MinGW 自带的 WRL 不完整, 这里自己写最少的一份).
#pragma once
#include <windows.h>

#include <cstdint>
#include <functional>
#include <string>
#include <utility>
#include <vector>

#include "WebView2.h"

std::string utf8(const std::wstring& w);
std::wstring widen(const std::string& s);
std::wstring exe_dir();
// 桌宠的图标 (res/wmpet.rc 里的 app.ico), 按 size 像素取最合适的那一档; 资源没有时用 Windows 默认的程序图标
HICON app_icon(int size);
std::wstring full_path(const std::wstring& p);
bool read_file(const std::wstring& path, std::vector<char>& out);
void log_open(const std::wstring& path);
void logf(const char* fmt, ...);  // 格式串和 %s 参数都是 UTF-8
std::string json_escape(const std::string& s);
bool base64_decode(const std::string& in, std::vector<uint8_t>& out);
std::string base64_encode(const uint8_t* data, size_t n);

// 页面发来的命中掩码: 把客户区切成 cols x rows 格, 1 表示这一格有角色 (已外扩一格).
// 格式 "mask <cols> <rows> <foot> <base64>", 位按行优先、每字节低位在前; foot 是最下面一行有角色的格 (外扩之前), 没有时 -1.
struct HitMask {
  int cols = 0, rows = 0, foot = -1;
  std::vector<uint8_t> bits;  // cols * rows 个 0/1
  ULONGLONG received = 0;     // GetTickCount64
  bool valid() const { return cols > 0 && rows > 0 && bits.size() == size_t(cols) * rows; }
  bool cell(int c, int r) const { return c >= 0 && r >= 0 && c < cols && r < rows && bits[size_t(r) * cols + c]; }
  bool hit(double fx, double fy) const { return valid() && cell(int(fx * cols), int(fy * rows)); }
  int opaque_cells() const;
  static bool parse(const std::string& msg, HitMask& out);
};

template <class T>
class Com {
 public:
  Com() = default;
  Com(const Com& o) : p_(o.p_) { if (p_) p_->AddRef(); }
  Com(Com&& o) noexcept : p_(o.p_) { o.p_ = nullptr; }
  ~Com() { reset(); }
  Com& operator=(Com o) noexcept { std::swap(p_, o.p_); return *this; }
  T* get() const { return p_; }
  T* operator->() const { return p_; }
  explicit operator bool() const { return p_ != nullptr; }
  T** put() { reset(); return &p_; }
  void reset() { if (p_) { p_->Release(); p_ = nullptr; } }
  void attach(T* p) { reset(); p_ = p; }                          // 接管已经带一次引用的指针
  void borrow(T* p) { reset(); p_ = p; if (p_) p_->AddRef(); }    // 借来的指针: 自己再加一次引用
  template <class U>
  Com<U> as(REFIID iid) const {
    Com<U> out;
    if (p_) p_->QueryInterface(iid, reinterpret_cast<void**>(out.put()));
    return out;
  }

 private:
  T* p_ = nullptr;
};

// WebView2 的各种 *Handler 都是 "IUnknown + 一个 Invoke": 用 std::function 包一层.
template <class I, class... A>
class Callback final : public I {
 public:
  Callback(REFIID iid, std::function<HRESULT(A...)> fn) : iid_(iid), fn_(std::move(fn)) {}
  HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** ppv) override {
    if (!ppv) return E_POINTER;
    if (IsEqualIID(riid, IID_IUnknown) || IsEqualIID(riid, iid_)) {
      *ppv = static_cast<I*>(this);
      AddRef();
      return S_OK;
    }
    *ppv = nullptr;
    return E_NOINTERFACE;
  }
  ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&ref_); }
  ULONG STDMETHODCALLTYPE Release() override {
    ULONG n = InterlockedDecrement(&ref_);
    if (!n) delete this;
    return n;
  }
  HRESULT STDMETHODCALLTYPE Invoke(A... a) override { return fn_(a...); }

 private:
  IID iid_;
  LONG ref_ = 1;
  std::function<HRESULT(A...)> fn_;
};

// make_handler<接口, Invoke 的参数类型...>(IID, lambda)
template <class I, class... A, class F>
Com<I> make_handler(REFIID iid, F fn) {
  Com<I> c;
  c.attach(new Callback<I, A...>(iid, std::function<HRESULT(A...)>(std::move(fn))));
  return c;
}
