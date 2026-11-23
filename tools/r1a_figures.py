"""R1a (动作生成模型研究, docs/roadmap/motion-model.md) 的图: 从 logs/motion-model/r1a/ 的原始结果画, 存到 logs/motion-model/r1a/figures/.

  python tools/r1a_figures.py

图:
  1. four_groups_pain.png      四组 (g1-g4) 和原库在三种体型上, 各场景的痛觉 (每帧平均)
  2. four_groups_terms.png     拿放、坐下、转身、停下: 痛觉按项拆 (加权后), 看手臂被推开、两腿穿插、重心各占多少
  3. arm_out.png               手离髋部中线的距离 (厘米): 手臂约束有没有靠"一律张开"过关
  4. contact_timing.png        触地时刻: 着地 / 离地对上的比例、平均差几帧 (离线, 走运行时同一条路径)
  5. versions_overview.png     各版本 (原库、第一到第四版、压缩匹配库) 的动作数据占用和 silvercat 上的痛觉
每张图的数据来源写在图的标题和 figures/README.md 里.
"""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
R = ROOT / "logs" / "motion-model" / "r1a"
OUT = R / "figures"
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
AV = ["silvercat", "miomio", "wendy"]
GROUPS = [("base", "原库"), ("g1", "g1 第二版"), ("g2", "g2 只改触地"), ("g3", "g3 只改手臂"), ("g4", "g4 都改")]
COLORS = ["#6b7280", "#2563eb", "#16a34a", "#d97706", "#9333ea"]
SCEN = [("walk", "走"), ("reach", "走到目标"), ("sit", "坐下"), ("pickput", "拿放"), ("lat_redirect", "换目标"), ("lat_stop", "停下"),
        ("lat_turn", "转身"), ("idle", "待机")]
PW = {"selfPen": 100, "legPen": 100, "pushed": 30, "furniture": 100, "itemPen": 100, "floorPen": 100, "floating": 50, "slide": 100,
      "joint": 0.2, "balance": 100, "jump": 0.2, "jerk": 0.5, "grab": 100}
TERM_NAMES = {"pushed": "手臂被推开", "selfPen": "手臂插进身体", "legPen": "两腿穿插", "balance": "重心出支撑面", "slide": "脚打滑",
              "jerk": "冲击", "furniture": "碰家具", "grab": "隔空拿放", "joint": "关节越界", "floating": "脚离地"}


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def rows(d: dict, sc: str) -> list[dict]:
    return [r for r in d["results"] if (r["scenario"].startswith("reach:") if sc == "reach" else r["scenario"] == sc)]


def fig_four_groups() -> list[str]:
    D = {(av, g): load(R / "round4-groups-avatars" / f"{av}-{g}.json") for av in AV for g, _ in GROUPS}
    fig, axes = plt.subplots(3, 1, figsize=(12, 11), sharex=True)
    for ax, av in zip(axes, AV):
        w = 0.16
        for gi, (g, name) in enumerate(GROUPS):
            vals = [mean(r["pain_mean"] for r in rows(D[(av, g)], sc)) for sc, _ in SCEN]
            ax.bar([i + (gi - 2) * w for i in range(len(SCEN))], vals, w, label=name, color=COLORS[gi])
        ax.set_title(f"{av}: 各场景痛觉 (每帧平均, 3 个种子)")
        ax.set_ylabel("痛觉")
    axes[-1].set_xticks(range(len(SCEN)), [n for _, n in SCEN])
    axes[0].legend(ncol=5, fontsize=9)
    fig.suptitle("R1a 第四轮: 四组 x 三种体型 (数据: logs/motion-model/r1a/round4-groups-avatars, 快照代码 7f03ef9f)", fontsize=10)
    fig.tight_layout()
    fig.savefig(OUT / "four_groups_pain.png", dpi=130)
    plt.close(fig)
    # 痛觉按项拆
    fig, axes = plt.subplots(3, 4, figsize=(15, 10))
    for ri, av in enumerate(AV):
        for ci, sc in enumerate(["pickput", "sit", "lat_turn", "lat_stop"]):
            ax = axes[ri][ci]
            terms = {}
            for g, _ in GROUPS:
                rs = rows(D[(av, g)], sc)
                terms[g] = {k: mean(r["pain"].get(k, 0) for r in rs) * PW[k] for k in PW}
            keys = sorted(PW, key=lambda k: -max(t[k] for t in terms.values()))[:4]
            bottom = [0.0] * len(GROUPS)
            for k in keys:
                v = [terms[g][k] for g, _ in GROUPS]
                ax.bar(range(len(GROUPS)), v, bottom=bottom, label=TERM_NAMES.get(k, k))
                bottom = [b + x for b, x in zip(bottom, v)]
            ax.set_xticks(range(len(GROUPS)), [g for g, _ in GROUPS], fontsize=8)
            ax.set_title(f"{av} / {dict(SCEN)[sc]}", fontsize=10)
            ax.legend(fontsize=7)
    fig.suptitle("痛觉按项拆 (加权后累计, 3 个种子平均; 权重见 web/pain.js)", fontsize=11)
    fig.tight_layout()
    fig.savefig(OUT / "four_groups_terms.png", dpi=130)
    plt.close(fig)
    # 手离髋部中线
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.2), sharey=True)
    sc_arm = [("idle", "待机"), ("walk", "走"), ("reach", "走到目标"), ("sit", "坐下"), ("pickput", "拿放"), ("emote", "表情动作"), ("lat_heart", "比心")]
    for ax, av in zip(axes, AV):
        w = 0.16
        for gi, (g, name) in enumerate(GROUPS):
            st = D[(av, g)]["stats"]
            vals = []
            for sc, _ in sc_arm:
                v = [s["armOut"]["mean"] for k, s in st.items() if "armOut" in s and (k.split("#")[0] == sc or (sc == "reach" and k.startswith("reach:")))]
                vals.append(mean(v) if v else 0)
            ax.bar([i + (gi - 2) * w for i in range(len(sc_arm))], vals, w, label=name, color=COLORS[gi])
        ax.set_xticks(range(len(sc_arm)), [n for _, n in sc_arm], fontsize=8)
        ax.set_title(av)
    axes[0].set_ylabel("手离髋部中线 (cm)")
    axes[0].legend(fontsize=8)
    fig.suptitle("手离髋部中线的横向距离: 各组和原库几乎一样 (手臂约束没有靠张开过关, 也没把手推出去)", fontsize=10)
    fig.tight_layout()
    fig.savefig(OUT / "arm_out.png", dpi=130)
    plt.close(fig)
    return ["four_groups_pain.png", "four_groups_terms.png", "arm_out.png"]


def fig_contact() -> str:
    names = [("v2", "g1 第二版"), ("g2", "g2 只改触地"), ("g3", "g3 只改手臂"), ("v4", "g4 都改")]
    C = {k: load(R / "checks" / f"{k}-check.json")["neutral"]["contactTiming"] for k, _ in names}
    fig, axes = plt.subplots(1, 3, figsize=(14, 4))
    # 2026-10-27 以前的核对结果只有 extraPerSec (两只脚合计, 当时图上错标成"每只脚"); 有新字段就用新的
    for c in C.values():
        for ev in c.values():
            ev.setdefault("extraPerFootSec", ev["extraPerSec"] / 2 if "extraPerSec" in ev else 0)
    for ax, (key, title) in zip(axes, [("found", "对上的比例 (±6 帧)"), ("absFrames", "平均差 (帧, 30 fps)"), ("extraPerFootSec", "多出来的事件 (每只脚每秒)")]):
        for i, (k, n) in enumerate(names):
            for dx, ev, col, lab in ((-0.18, "on", "#2563eb", "着地"), (0.18, "off", "#d97706", "离地")):
                v = C[k][ev][key]
                ax.bar([i + dx], [v], 0.36, color=col, label=lab if i == 0 else None)
                ax.text(i + dx, v, f"{v:.3f}" if key == "found" else f"{v:.2f}", ha="center", va="bottom", fontsize=7)
        ax.set_xticks(range(len(names)), [n for _, n in names], fontsize=8)
        ax.set_title(title)
        if key == "found":
            ax.set_ylim(0, 1)  # 从 0 开始: 四组只差一两个百分点, 截断的坐标轴会把差别放大
    axes[0].legend()
    fig.suptitle("触地时刻 (离线: 从原库某帧投影过来播 60 帧, 1412 段; tools/lmm_check.mjs; 数据 logs/motion-model/r1a/checks/)", fontsize=10)
    fig.tight_layout()
    fig.savefig(OUT / "contact_timing.png", dpi=130)
    plt.close(fig)
    return "contact_timing.png"


def fig_versions() -> str:
    # silvercat, 各版本同一个快照里的 A/B: 第一版 round1 (lmm@256), 第二版 round2, 第二版+偏置 round4 g1, 第四版 round4 g4, 压缩匹配库 v3c final
    src = [("原库", R / "round4-groups-avatars" / "silvercat-base.json", 51.9),
           ("第一版", R / "round1-v1" / "lmm256-1.json", None), ("第二版", R / "round2-v2" / "lmm256-1.json", None),
           ("第二版+偏置0.1 (g1)", R / "round4-groups-avatars" / "silvercat-g1.json", None),
           ("第四版 (g4)", R / "round4-groups-avatars" / "silvercat-g4.json", None),
           ("压缩匹配库 v3c", R / "final" / "v3cneutral-1.json", None)]
    labels, mem, pain = [], [], {sc: [] for sc, _ in SCEN}
    for name, p, m in src:
        d = load(p)
        labels.append(name)
        at = d["mem"]["atLoad"]
        mem.append(m if m is not None else (at["styles"].get("neutral", 0) + at["idle"]) / 1e6)
        for sc, _ in SCEN:
            rs = rows(d, sc)
            pain[sc].append(mean(r["pain_mean"] for r in rs) if rs else 0)
    fig, axes = plt.subplots(1, 2, figsize=(15, 4.8), gridspec_kw={"width_ratios": [1, 2.4]})
    axes[0].barh(labels, mem, color="#2563eb")
    axes[0].set_xlabel("中性库 + 待机 运行时占用 (MB)")
    axes[0].invert_yaxis()
    w = 0.13
    for i, (name, _, _) in enumerate(src):
        axes[1].bar([j + (i - 2.5) * w for j in range(len(SCEN))], [pain[sc][i] for sc, _ in SCEN], w, label=name)
    axes[1].set_xticks(range(len(SCEN)), [n for _, n in SCEN])
    axes[1].set_ylabel("痛觉 (每帧平均)")
    axes[1].legend(fontsize=8)
    fig.suptitle("各版本: 占用和 silvercat 上的痛觉 (不同轮次的快照代码不同, 只看大致; 同一轮里的 A/B 见各轮汇总)", fontsize=10)
    fig.tight_layout()
    fig.savefig(OUT / "versions_overview.png", dpi=130)
    plt.close(fig)
    return "versions_overview.png"


def fig_locate() -> str:
    """定位轮 (tools/lmm_locate.py): 误差从哪一环进来. 每个检查点一种线型."""
    tags = [(t, n, ls) for t, n, ls in (("g1", "g1 第二版", "-"), ("g3", "g3 手臂+姿态微调", "--"), ("g3p", "g3p 只加姿态损失", ":"))
            if (R / "locate" / f"{t}.json").exists()]
    L = {t: load(R / "locate" / f"{t}.json") for t, _, _ in tags}
    cases = [("encoded", "编码直接解码", "#6b7280"), ("projected_each_frame", "逐帧投影解码", "#16a34a"),
             ("multi_from_true", "多步 (真起点)", "#2563eb"), ("multi_from_projected", "多步 (投影起点) = 固定段", "#9333ea"),
             ("mm_rule", "每 3 帧按规则重投影", "#d97706")]
    hz = ["1-14", "15-29", "30-60"]
    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    for t, _, ls in tags:
        for k, n, col in cases:
            axes[0][0].plot(hz, [L[t][k]["poseDegByHorizon"][h] for h in hz], ls, color=col, marker="o", label=n if t == tags[0][0] else None)
            axes[0][1].plot(hz, [L[t][k]["phaseLag"][h]["meanAbsLag"] for h in hz], ls, color=col, marker="o")
    axes[0][0].set_title("姿态误差 (度, 按离起点多少帧分段)")
    axes[0][0].legend(fontsize=8)
    axes[0][1].set_title("触地相位滞后 (帧, 平均绝对值)")
    for ax in axes[0]:
        ax.set_xlabel("离起点 (帧)")
        ax.set_ylim(bottom=0)
    # 着地 / 离地对上的比例
    ax = axes[1][0]
    w = 0.8 / max(1, len(tags))
    for ti, (t, n, _) in enumerate(tags):
        for ci, (k, _, col) in enumerate(cases):
            for dy, ev in ((0, "on"), (1, "off")):
                x = ci * 2 + dy + (ti - (len(tags) - 1) / 2) * w
                ax.bar([x], [L[t][k]["timing"][ev]["found"]], w, color=col, alpha=1 - 0.25 * ti, hatch="" if ev == "on" else "//")
    ax.set_xticks([ci * 2 + 0.5 for ci in range(len(cases))], [n for _, n, _ in cases], fontsize=7, rotation=12)
    ax.set_ylim(0, 1)  # 从 0 开始, 和 contact_timing.png 一样: 截断的坐标轴会把差别放大
    ax.axhline(L[tags[0][0]]["original_mm_noise0.3"]["timing"]["on"]["found"], color="#6b7280", lw=0.8, ls="--")
    ax.set_title("着地 (实心) / 离地 (斜线) 对上的比例 (±6 帧)  柱从左到右: " + " / ".join(t for t, _, _ in tags)
                 + "; 虚线: 原库按规则换 (查询加噪声) 的着地", fontsize=9)
    # 换段让触地碎成短段
    ax = axes[1][1]
    fl = [("truth", "原片段 (真值)"), ("fixed_segment", "网络固定段"), ("mm_rule", "网络按规则换"), ("always", "网络每 3 帧都换"),
          ("mm_rule_noise0.3", "网络按规则换 (查询加噪声)"), ("original_mm_noise0.3", "原库按规则换 (查询加噪声)")]
    for ti, (t, n, _) in enumerate(tags):
        v = [L[t][k]["flicker"]["shortRunFrac"] * 100 for k, _ in fl]
        ax.bar([i + (ti - (len(tags) - 1) / 2) * w for i in range(len(fl))], v, w, label=n, color=COLORS[[g for g, _ in GROUPS].index(t)] if t in dict(GROUPS) else "#0891b2")
        for i, x in enumerate(v):
            sw = L[t][fl[i][0]].get("switchesPerSec")
            ax.text(i + (ti - (len(tags) - 1) / 2) * w, x, f"{x:.1f}" + (f"\n{sw:.1f}/s" if sw is not None and ti == 0 else ""), ha="center", va="bottom", fontsize=6)
    ax.set_xticks(range(len(fl)), [n for _, n in fl], fontsize=7, rotation=15)
    ax.set_ylabel("短于 4 帧的触地段占比 (%)")
    ax.set_title("换段让触地碎成短段 (柱上第二行: 第一个检查点每秒换段次数)", fontsize=9)
    ax.legend(fontsize=8)
    n0 = L[tags[0][0]]
    fig.suptitle(f"定位: 误差从哪一环进来 (离线, 原中性库 {n0['starts']} 个起点 x {n0['len']} 帧, 偏置 {n0['bias']}; "
                 "tools/lmm_locate.py; 数据 logs/motion-model/r1a/locate/)  实线 g1 / 虚线 g3 / 点线 g3p", fontsize=9)
    fig.tight_layout()
    fig.savefig(OUT / "locate.png", dpi=130)
    plt.close(fig)
    return "locate.png"


def fig_locate_ab(batch: str = "locate-ab", code: str = "72d44bbe", out: str = "locate_ab.png", note: str = "") -> str:
    """定位轮的页面 A/B (三体型 x 原库 / g1 / g1 交接处换回原库 / g3 / g3p): 各场景痛觉; 坐下开头单脚的重心、拿放时东西压进胸口.
    locate-ab 是第一批 (评估还有跨场景残留), locate-ab2 是 2026-10-28 修了残留和待机槽以后的复核."""
    A = R / batch
    S = load(A / "summary.json")
    B = load(A / "summary-by-source.json")
    gs = [("base", "原库", "#6b7280"), ("g1", "g1", "#2563eb"), ("g1h", "g1 + 最后 1 米换回原库", "#0891b2"),
          ("g3", "g3 手臂+姿态", "#d97706"), ("g3p", "g3p 只加姿态损失", "#16a34a")]
    fig, axes = plt.subplots(3, 2, figsize=(16, 11), gridspec_kw={"width_ratios": [2.6, 1]})
    w = 0.16
    for ri, av in enumerate(AV):
        ax = axes[ri][0]
        for gi, (g, n, col) in enumerate(gs):
            o = S.get(f"{av}-{g}")
            if o:
                ax.bar([i + (gi - 2) * w for i in range(len(SCEN))], [o["pain"].get(sc, 0) for sc, _ in SCEN], w, label=n, color=col)
        ax.set_xticks(range(len(SCEN)), [n for _, n in SCEN])
        ax.set_title(f"{av}: 各场景痛觉 (每帧平均, 3 个种子)", fontsize=10)
        ax = axes[ri][1]
        vals = []
        for g, _, _ in gs:
            sit = B.get(f"{av}-{g}-sit", {}).get("balanceBy", {})
            pp = B.get(f"{av}-{g}-pickput", {}).get("contactBy", {})
            vals.append((sit.get("act:sit_down|1脚", 0), pp.get("chest:item", 0) + pp.get("belly:item", 0)))
        for k, title in enumerate(["坐下动作开头单脚时重心出支撑面", "拿放: 东西压进胸口、肚子"]):
            ax.bar([j + (k - 0.5) * 0.4 for j in range(len(gs))], [v[k] for v in vals], 0.4, label=title,
                   color=[c for _, _, c in gs], hatch="" if k == 0 else "//", edgecolor="white")
        ax.set_xticks(range(len(gs)), [g for g, _, _ in gs], fontsize=8)
        ax.set_title(f"{av}: 实心 = 坐下开头单脚重心; 斜线 = 东西压进胸口、肚子", fontsize=9)
    axes[0][0].legend(ncol=5, fontsize=8)
    fig.suptitle(f"定位轮页面 A/B (快照代码 {code}; 只换中性库, 偏置 0.1; 数据 logs/motion-model/r1a/{batch}/){note}", fontsize=10)
    fig.tight_layout()
    fig.savefig(OUT / out, dpi=130)
    plt.close(fig)
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    made = fig_four_groups() + [fig_contact(), fig_versions(), fig_locate()]
    if (R / "locate-ab" / "summary.json").exists():
        made.append(fig_locate_ab(note=" -- 第一批, 评估还有跨场景残留"))
    if (R / "locate-ab2" / "summary.json").exists():
        code = load(R / "locate-ab2" / "silvercat-base.json")["versions"]["code"][:8]
        made.append(fig_locate_ab("locate-ab2", code, "locate_ab2.png", " -- 复核: 修了评估残留和网络的待机槽"))
    (OUT / "README.md").write_text("# R1a 的图\n\n由 `tools/r1a_figures.py` 从 `logs/motion-model/r1a/` 的原始结果画出来.\n\n"
                                   + "\n".join(f"- `{m}`" for m in made) + "\n", encoding="utf-8")
    print("\n".join(str(OUT / m) for m in made))


if __name__ == "__main__":
    main()
