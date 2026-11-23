# 外放回声: 最小二乘线性消除原型 (2026-11-11, 没进 wm/)

11-11 第二、三次外放真机试, 线性消除 (wm/echo.py 的 MDF) 在一段里只做到 4 dB 上下就不动了 (`fit_db` 很高: 同一段上固定的线性滤波器能做到
十几 dB), 所以试了另一种线性消除: **最小二乘 (维纳)**, 想看看它不会"卡在半路"是不是更稳. 评测结论是**没有哪个环境明显比 MDF 更好**
(下面的表), 所以没进 `wm/echo.py`, 做法和测试留在这里. 真正卡住的原因后来查出来是别的 (参考的尾巴比回环写进环的位置新, 读出来是 0, 见
`wm/echo.py` 的 `hold_ms` / `pre_safety_ms`), 那个修好以后 MDF 就不卡了.

## 做法 (`ls_canceller.py` 里的 `LSCanceller`, 接口和 `wm.echo.MDF` 一样: process / prime / shift / reset, erle / leak / adapted)

- 每块 (20 ms) 把参考和麦克风的相关累起来: 用 FFT 精确算出这一帧 (2 块长) 里的参考自相关 R 和互相关 P, 参考有声音的帧按 `ls_tau_s` (3 s) 的
  时间常数指数遗忘地累进; 参考没声音时不累、也不遗忘.
- 每隔 `ls_update_s` (1 s; 前 8 次每 0.25 s, 对齐挪过以后 4 秒内每 0.5 s) 解一次 `R h = P` (托普利兹, Levinson / `scipy.linalg.solve_toeplitz`).
  对角线上按 `ls_reg` 加岭 (数据少时多加, 免得几秒的数据拟合出一个乱的滤波器); h 的范数设上限.
- 增益 (浏览器自动增益几秒一摆) 用标量跟一下 (`ls_gain_track`).
- 对齐挪了 (`shift`): 累的 R、P 跟着挪, 不用从头学; 方向和 `MDF.shift` 一样 (`test_ls_keeps_the_learned_path_when_the_alignment_moves`).
- 残余压制看的"漏回声"系数给下限 `ls_leak_floor` (0.3): 解出来的残差和回声估计不相关, 按相关估出来的总是很小, 残余压制就不压了.
  erle 和 MDF 一样在预加重过的信号上算.
- 试过又丢掉的: 按帧能量加权 (自己强化自己)、用近似的二次型当验证集挑岭 (估偏了).

## 评测 (tools/echo_bench.py, 同一批合成场景: 说话的视频声音当参考, 经房间响应进麦克风; 不录真麦克风、不录系统声音)

`mdf` 是当时草稿里的 MDF (12 块 + 等 20 ms + 按环里写到哪减 pre + 没学会也压), `ls` 是同一份草稿换成最小二乘 (其余都一样), `cur` 是改之前的
`wm/echo.py`. 每格 2 个种子; "外放没说话": VAD 误起次数 / 在听秒数 (最长一段) / 假句; "外放时你说": 认出 / 共; 线性消除 dB 是 (外放没说话 + 外放时
你说) 两种场景的中位数.

| 环境 | 做法 | 误起 | 在听秒 (最长) | 你说: 认出 / 共 (字错率) | 线性消除 dB |
|---|---|---|---|---|---|
| clean | cur / mdf / ls | 9 / 12 / 11 | 7.2 / 11.5 / 7.7 | 20 / 20 / 12 of 24 (0.236 / 0.267 / 0.467) | 13.8 / 13.8 / 12.6 |
| fan | cur / mdf / ls | 20 / 14 / 21 | 51.0 / 17.6 / 29.6 | 17 / 9 / 6 (0.342 / 0.562 / 0.666) | 8.9 / 8.8 / 8.7 |
| fan_ns | cur / mdf / ls | 13 / 10 / 8 | 13.6 / 10.8 / 8.6 | 19 / 20 / 10 (0.310 / 0.341 / 0.546) | 10.7 / 10.7 / 10.8 |
| fan_ns_agc | cur / mdf / ls | 30 / 25 / 15 | 29.0 / 25.7 / 15.4 | 14 / 7 / 7 (0.453 / 0.574 / 0.610) | 7.9 / 7.7 / 8.6 |
| q33 (真机三次的时序) | cur / mdf / ls | 33 / 16 / 18 | 122.8 / 25.8 / 26.4 | 2 / 4 / 3 (0.932 / 0.740 / 0.805) | 3.5 / 7.8 / 7.3 |
| q150 (真机二次的时序) | cur / mdf / ls | 18 / 18 / 23 | 66.7 / 19.2 / 27.0 | 8 / 8 / 4 (0.650 / 0.602 / 0.732) | 8.6 / 8.3 / 8.2 |

- 线性消除的 dB 几乎一样 (差在 ±0.6 dB 里), 只有 fan_ns_agc (浏览器自动增益开着, 默认是关的) 高 0.9 dB、误起 25 → 15.
- 你说话的那几个场景, 最小二乘认出得更少 (clean 20 → 12, fan 9 → 6, fan_ns 20 → 10, q150 8 → 4), 被复核丢的句子也更多.
- 开销: 平时 MDF 草稿约 28–31 ms/s, 最小二乘约 40–44 ms/s (机器不忙时测的; 改之前的 cur 约 22–23).
- 判定标准 (协调对话给的): 没有明显更好的环境就不进 wm/ → 这里放原型.

## 怎么再试

```bash
python -B -m pytest prototypes/echo_ls -q -p no:cacheprovider        # 3 条测试 (合成的线性回声; 不录麦克风)
python tools/echo_bench.py --module prototypes/echo_ls/echo_with_ls_20261111.py --variants cur,mdf,ls --env fan_ns_agc --seeds 2 --threads 1
```

换进 EchoGuard (接口一样):

```python
from ls_canceller import LSCanceller
guard.mdf = LSCanceller(guard.N, guard.M, conf=guard.conf)
```

## 文件

- `ls_canceller.py` — `LSCanceller` 和它的 `DEFAULTS` (`ls_*` 五个参数).
- `test_ls_canceller.py` — 3 条测试: 线性回声 + -40 dBFS 底噪能消到底噪那么低; 你说话时不被带歪; 对齐挪了跟着挪.
- `echo_with_ls_20261111.py` — 评测时用的 wm/echo.py 草稿快照: 里面除了最小二乘 (`linear: "ls"`), 还有当时的 12 块、等 20 ms、"没学会也压"
  (`unlearned_suppress`), 给 `tools/echo_bench.py --module` 用 (表里的 mdf / ls 都是它).
- `echo_with_fallback_20261111.py` — 同一份草稿的最后一版 (等 40 ms、按环里写到哪减 pre、"没学会也压"加了 `fb_below_db` / `fb_gate_overestimate` 两个旋钮).
  "没学会也压"也没进 wm/: 评测里它让外放没说话时的误起和在听时间少一半以上 (fan 51 → 17.6 s), 可你在视频放着时说话认出得少 (fan 17 → 9 / 24,
  fan_ns_agc 14 → 7 / 24), 而且戴耳机的场景里 GCC 偶然有两次对得上的假峰 (量到的延迟 809 ms、erle -0.0), 就被判成外放, 你的话大半被压掉
  (那个耳机场景的字错率 0.093 → 0.234). 数字和过程见交接笔记《外放线性消除-进展与交接》.
