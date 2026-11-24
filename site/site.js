/* S.A.T.I.O Verse — 站点脚本（零依赖） */
(() => {
  "use strict";

  /* 截图/自检用：?y=1200 直接跳到某个位置（生产页面不会带这个参数） */
  const y = new URLSearchParams(location.search).get("y");
  if (y && /^\d+$/.test(y)) window.scrollTo({ top: Number(y), behavior: "instant" });

  /* 自检用：?measure=1 把关键尺寸写进 title，方便 headless --dump-dom 读取 */
  if (new URLSearchParams(location.search).has("measure")) {
    requestAnimationFrame(() => {
      const tb = document.querySelector(".topbar").getBoundingClientRect();
      const pill = document.querySelector(".pill").getBoundingClientRect();
      const wide = [...document.querySelectorAll("body *")]
        .filter((el) => el.getBoundingClientRect().right > document.documentElement.clientWidth + 1)
        .slice(0, 4)
        .map((el) => el.tagName.toLowerCase() + "." + (el.className || "") + "@" + Math.round(el.getBoundingClientRect().right));
      document.title = `vw=${innerWidth} scrollW=${document.documentElement.scrollWidth} topbar=${Math.round(tb.left)}..${Math.round(tb.right)} pill=${Math.round(pill.left)}..${Math.round(pill.right)} wide=${wide.join("|")}`;
    });
  }

  /* 1. 进场动画
     默认全部可见；只把「加载时明显在屏幕下方」的元素先藏起来，再由观察器放出来。
     failsafe：1.6 秒后无论如何都放开，避免观察器不触发时页面看起来是空的。 */
  const reveals = [...document.querySelectorAll(".reveal")];
  const still = new URLSearchParams(location.search).has("static");
  if (still) {
    document.documentElement.style.scrollBehavior = "auto";
    document.documentElement.dataset.shot = "1"; // 截图时把固定层改成静态层，长图才准
  }
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const showAll = () => reveals.forEach((el) => { el.classList.remove("pre"); el.classList.add("in"); });

  if (!still && !reduce) {
    for (const el of reveals) {
      if (el.getBoundingClientRect().top > innerHeight * 0.92) el.classList.add("pre");
    }
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) { e.target.classList.remove("pre"); e.target.classList.add("in"); io.unobserve(e.target); }
      }
    }, { rootMargin: "0px 0px -10% 0px", threshold: 0.04 });
    reveals.forEach((el) => io.observe(el));
    setTimeout(showAll, 1600);
  }

  /* 2. 左侧编号索引跟随滚动 */
  const sections = [...document.querySelectorAll("main section[id]")];
  const railItems = [...document.querySelectorAll(".rail li")];
  if (sections.length && railItems.length) {
    const spy = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        railItems.forEach((li) => li.classList.toggle("on", li.dataset.for === e.target.id));
      }
    }, { rootMargin: "-45% 0px -45% 0px" });
    sections.forEach((s) => spy.observe(s));
    railItems.forEach((li) => {
      li.addEventListener("click", () => {
        const t = document.getElementById(li.dataset.for);
        if (t) t.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
  }

  /* 3. 健康状态 */
  const statusEl = document.getElementById("status");
  const footHealth = document.getElementById("foot-health");
  const setStatus = (state, text) => {
    if (statusEl) { statusEl.dataset.state = state; statusEl.querySelector("b").textContent = text; }
    if (footHealth) footHealth.textContent = text;
  };

  /* 4. 插件目录：直接读 /index.json */
  const list = document.getElementById("pkgs");
  const countEl = document.getElementById("catalog-count");

  const renderEmpty = () => {
    if (!list) return;
    list.innerHTML = '<li class="empty"><b>这个源上还没有包。</b> 第一个可以是你：把包推到主仓的 <code>plugins/</code>，或者走 <code>POST /contributions</code> 投稿。</li>';
  };

  const renderPackages = (items) => {
    if (!list) return;
    if (!items.length) return renderEmpty();
    list.innerHTML = "";
    for (const p of items) {
      const li = document.createElement("li");
      const name = document.createElement("div");
      name.className = "pkg-name";
      name.textContent = p.title || p.name;
      if (p.name && p.title && p.title !== p.name) {
        const small = document.createElement("small");
        small.textContent = p.name;
        name.appendChild(small);
      }
      const ver = document.createElement("div");
      ver.className = "pkg-ver";
      ver.textContent = p.version ? "v" + p.version : "";
      const desc = document.createElement("div");
      desc.className = "pkg-desc";
      desc.textContent = p.description || "（没有写描述）";
      li.append(name, ver, desc);
      list.appendChild(li);
    }
  };

  const loadIndex = async () => {
    try {
      const res = await fetch("/index.json", { headers: { accept: "application/json" } });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      const items = Array.isArray(data.packages) ? data.packages : [];
      if (countEl) countEl.textContent = `共 ${items.length} 个包 · ${new Date().toLocaleDateString("zh-CN")} 读取`;
      renderPackages(items);
    } catch (err) {
      if (countEl) countEl.textContent = "索引读取失败：" + err.message;
      renderEmpty();
    }
  };

  const loadHealth = async () => {
    try {
      const res = await fetch("/health", { headers: { accept: "application/json" } });
      const data = await res.json();
      const n = typeof data.packages === "number" ? data.packages : null;
      setStatus("ok", n === null ? "在线" : `在线 · ${n} 个包`);
    } catch {
      setStatus("down", "离线");
    }
  };

  loadIndex();
  loadHealth();

  /* 5. 复制按钮 */
  for (const btn of document.querySelectorAll(".copy")) {
    btn.addEventListener("click", async () => {
      const text = btn.dataset.copy || "";
      try {
        await navigator.clipboard.writeText(text);
        const old = btn.textContent;
        btn.textContent = "已复制";
        btn.classList.add("done");
        setTimeout(() => { btn.textContent = old; btn.classList.remove("done"); }, 1600);
      } catch {
        btn.textContent = "复制失败";
        setTimeout(() => { btn.textContent = "复制"; }, 1600);
      }
    });
  }
})();
