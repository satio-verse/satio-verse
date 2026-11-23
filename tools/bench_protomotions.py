"""ProtoMotions 可行性基准: 在 MuJoCo (CPU) 上跑预训练物理控制策略, 分项计时并检查是否摔倒.

ProtoMotions 及其预训练模型为非商业研究用途 (代码 Apache-2.0, 模型 NVIDIA Open Model License,
训练数据 AMASS / SMPL 另有非商用条款), 见 third_party/protomotions/LICENSE.md 与 legal/.
不改上游源码: 本脚本复用 protomotions/inference_agent.py 的装配流程, 只替换它的评测循环.

用独立虚拟环境运行:
  runtime/protomotions/.venv/Scripts/python.exe tools/bench_protomotions.py \\
      --checkpoint third_party/protomotions/data/pretrained_models/masked_mimic/smpl/last.ckpt \\
      --motion-file runtime/protomotions/motions/smpl_kimodo.pt --steps 900 --cond root

--cond 只对 MaskedMimic 有效, 固定给哪些身体部位的未来目标 (其余部位不给):
  default  训练时的随机遮挡 (官方推理默认)
  all      六个可条件部位全给位置和朝向
  root     只给骨盆位置 (相当于"沿路线走")
  root_head 骨盆和头的位置加朝向
  vr       头的位置加朝向, 双手位置 (相当于 VR 三点)
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PM = ROOT / "third_party" / "protomotions"

# constraint_state: 0 只给位置, 1 位置和朝向, 2 只给朝向
CONDITIONS = {
    "default": None,
    "all": [(n, 1) for n in ("Pelvis", "L_Ankle", "R_Ankle", "L_Hand", "R_Hand", "Head")],
    "root": [("Pelvis", 0)],
    "root_head": [("Pelvis", 1), ("Head", 1)],
    "vr": [("Head", 1), ("L_Hand", 0), ("R_Hand", 0)],
}
FALL_DROP = 0.35  # 骨盆比参考动作低这么多 (米) 视为摔倒; 参考动作本身趴下/蹲下不算


def parse():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--checkpoint", required=True)
    p.add_argument("--motion-file", default=None)
    p.add_argument("--steps", type=int, default=900, help="计时的控制步数 (不含预热)")
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--cond", choices=sorted(CONDITIONS), default="default")
    p.add_argument("--threads", type=int, default=0, help="torch CPU 线程数, 0 为默认")
    p.add_argument("--realtime", action="store_true", help="按控制频率节拍运行 (模拟实际部署)")
    p.add_argument("--video", default=None, help="另存一段 mp4 (渲染不计入耗时)")
    p.add_argument("--video-frames", type=int, default=300)
    p.add_argument("--json", default=None, help="把汇总写到这个文件")
    p.add_argument("--label", default="")
    p.add_argument("--profile", action="store_true", help="用 cProfile 统计计时段, 打印最耗时的函数")
    p.add_argument("--trace", type=int, default=0, help="逐步打印前 N 步的骨盆高度和参考高度")
    p.add_argument("--zero-action", action="store_true", help="诊断用: 不用策略, PD 目标固定为默认姿态")
    p.add_argument("--full-forward", action="store_true",
                   help="用上游评测循环的完整前向 (含训练用的编码器); 默认走模型的 forward_inference 部署路径")
    p.add_argument("--train-rate", action="store_true",
                   help="把 MuJoCo 控制频率改回训练时的频率 (物理步长取 600 Hz 左右的整数倍)")
    p.add_argument("--overrides", nargs="*", default=[])
    return p.parse_args()


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else float("nan")


def ms(xs):
    return {"p50": round(pct(xs, 0.5) * 1e3, 3), "p95": round(pct(xs, 0.95) * 1e3, 3),
            "max": round(max(xs) * 1e3, 3) if xs else None, "mean": round(statistics.fmean(xs) * 1e3, 3) if xs else None}


def main():
    opt = parse()
    for key in ("checkpoint", "motion_file", "video", "json"):
        if getattr(opt, key):
            setattr(opt, key, str(Path(getattr(opt, key)).resolve()))
    os.chdir(PM)  # 上游按仓库根目录解析资源路径
    argv = ["inference_agent.py", "--checkpoint", opt.checkpoint,
            "--simulator", "mujoco", "--num-envs", "1", "--headless"]
    if opt.motion_file:
        argv += ["--motion-file", opt.motion_file]
    if opt.overrides:
        argv += ["--overrides", *opt.overrides]
    if opt.train_rate:
        import torch
        sim = torch.load(Path(opt.checkpoint).parent / "resolved_configs_inference.pt",
                         map_location="cpu", weights_only=False)["simulator"].sim
        hz = float(sim.fps) / float(sim.decimation)
        decimation = max(1, round(600 / hz))
        overrides = [f"simulator.sim.fps={int(round(hz * decimation))}", f"simulator.sim.decimation={decimation}"]
        argv += ["--overrides", *overrides] if "--overrides" not in argv else overrides
    sys.argv = argv
    sys.path.insert(0, str(PM))

    import protomotions.inference_agent as ia  # 模块导入时解析上面的 sys.argv
    import torch
    from protomotions.agents.evaluators.base_evaluator import BaseEvaluator
    from protomotions.simulator.mujoco.simulator import MujocoSimulator

    if opt.threads:
        torch.set_num_threads(opt.threads)

    fixed = CONDITIONS[opt.cond]
    if fixed is not None:
        from protomotions.envs.control import masked_mimic_control as mmc
        original_init = mmc.MaskedMimicControl.__init__

        def init(self, config, env):
            config.fixed_conditioning = [mmc.FixedBodyCondition(n, s) for n, s in fixed]
            config.visible_target_pose_prob = 1.0
            original_init(self, config, env)
        mmc.MaskedMimicControl.__init__ = init

    physics = []
    original_physics = MujocoSimulator._physics_step

    def timed_physics(self):
        t = time.perf_counter()
        original_physics(self)
        physics.append(time.perf_counter() - t)
    MujocoSimulator._physics_step = timed_physics

    summary = {}

    def loop(self, collect_metrics=False):
        env, sim = self.env, self.env.simulator
        self.agent.eval()
        if opt.threads:
            torch.set_num_threads(opt.threads)  # Fabric 启动时会改线程数, 这里再设一次
        inner = getattr(self.agent.model, "module", self.agent.model)
        fast = None if opt.full_forward else getattr(inner, "forward_inference", None)

        def act(td):
            if fast is None:
                return self._policy_action(td)
            out = fast(td)
            return out["mean_action"] if "mean_action" in out else out["action"]
        cfg = sim.config.sim
        hz = float(cfg.fps) / float(cfg.decimation)
        renderer = None
        frames = []
        if opt.video:
            import mujoco
            renderer = mujoco.Renderer(sim.model, 480, 640)
            cam = mujoco.MjvCamera()
            cam.type = mujoco.mjtCamera.mjCAMERA_TRACKING
            cam.trackbodyid, cam.distance, cam.elevation, cam.azimuth = 1, 3.5, -12.0, 150.0
        rec = {"prep": [], "policy": [], "env": [], "physics": [], "total": []}
        heights, drops, metrics, falls, fallen, resets = [], [], {}, 0, False, 0
        mm = getattr(env, "motion_manager", None)
        done = None
        total = opt.warmup + opt.steps
        tick = time.perf_counter()
        wall0 = None
        profiler = None
        with torch.no_grad():
            for step in range(total):
                if step == opt.warmup:
                    wall0 = time.perf_counter()
                    if opt.profile:
                        import cProfile
                        profiler = cProfile.Profile()
                        profiler.enable()
                t0 = time.perf_counter()
                obs, _ = env.reset(done)
                self.agent.pre_collect_step(step)
                obs = self.agent.add_agent_info_to_obs(obs)
                obs_td = self.agent.obs_dict_to_tensordict(obs)
                t1 = time.perf_counter()
                action = act(obs_td)
                if opt.zero_action:
                    action = torch.zeros_like(action)
                t2 = time.perf_counter()
                n = len(physics)
                _, _, dones, _, extras = env.step(action)
                t3 = time.perf_counter()
                done = dones.nonzero(as_tuple=False).squeeze(-1)
                h = float(sim.data.qpos[2])
                ref_h = h
                if mm is not None and env.motion_lib is not None:
                    ref = env.motion_lib.get_motion_state(mm.motion_ids[:1], mm.motion_times[:1])
                    ref_h = float(ref.rigid_body_pos[0, 0, 2])
                down = ref_h - h > FALL_DROP
                if step < opt.trace:
                    print(f"TRACE step={step} t={float(mm.motion_times[0]) if mm is not None else 0:.2f} "
                          f"pelvis={h:.3f} ref={ref_h:.3f} done={done.numel()}")
                if step >= opt.warmup:
                    rec["prep"].append(t1 - t0)
                    rec["policy"].append(t2 - t1)
                    rec["env"].append(t3 - t2)
                    rec["physics"].append(sum(physics[n:]))
                    rec["total"].append(t3 - t0)
                    heights.append(h)
                    drops.append(ref_h - h)
                    resets += int(done.numel())
                    if down and not fallen:
                        falls += 1
                    for k, v in (extras.get("eval_values") or {}).items():
                        metrics.setdefault(k, []).append(float(v.float().mean()))
                fallen = down
                if renderer is not None and len(frames) < opt.video_frames:
                    renderer.update_scene(sim.data, camera=cam)
                    frames.append(renderer.render().copy())
                if opt.realtime:
                    tick += 1.0 / hz
                    delay = tick - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                    else:
                        tick = time.perf_counter()
        wall = time.perf_counter() - wall0
        if profiler is not None:
            import pstats
            profiler.disable()
            pstats.Stats(profiler).sort_stats("cumulative").print_stats(45)
        budget = 1.0 / hz
        summary.update({
            "label": opt.label, "checkpoint": str(Path(opt.checkpoint).name), "cond": opt.cond,
            "control_hz": hz, "sim_fps": float(cfg.fps), "decimation": int(cfg.decimation),
            "torch_threads": torch.get_num_threads(), "steps": opt.steps, "realtime_mode": opt.realtime,
            "policy_path": "full" if fast is None else "forward_inference",
            "ms": {k: ms(v) for k, v in rec.items()},
            "budget_ms": round(budget * 1e3, 2),
            "over_budget_pct": round(100 * sum(x > budget for x in rec["total"]) / len(rec["total"]), 2),
            "achievable_hz_p50": round(1.0 / pct(rec["total"], 0.5), 1),
            "sim_seconds": round(opt.steps / hz, 2), "wall_seconds": round(wall, 2),
            "pelvis_height": {"min": round(min(heights), 3), "p50": round(pct(heights, 0.5), 3)},
            "height_err_m": {"p50": round(pct([abs(d) for d in drops], 0.5), 3),
                             "p95": round(pct([abs(d) for d in drops], 0.95), 3)},
            "fallen_pct": round(100 * sum(d > FALL_DROP for d in drops) / len(drops), 2),
            "falls": falls, "resets": resets,
            "eval": {k: round(statistics.fmean(v), 4) for k, v in metrics.items()},
        })
        if renderer is not None and frames:
            import imageio.v2 as imageio
            Path(opt.video).parent.mkdir(parents=True, exist_ok=True)
            imageio.mimwrite(opt.video, frames, fps=int(round(hz)))
            summary["video"] = str(Path(opt.video).resolve())

    BaseEvaluator.simple_test_policy = loop
    ia.main()
    print("BENCH_SUMMARY " + json.dumps(summary, ensure_ascii=False))
    if opt.json:
        out = Path(opt.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
