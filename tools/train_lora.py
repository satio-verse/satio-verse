"""LoRA 微调 Qwen3.5-0.8B (在单独的虚拟环境里跑, 不动主环境):

  runtime/train-venv/Scripts/python tools/train_lora.py --check-format   # 核对提示词和 llama-server 一字不差 (要开着 llama-server)
  runtime/train-venv/Scripts/python tools/train_lora.py --out models/train/lora-v1
  runtime/train-venv/Scripts/python tools/train_lora.py --export models/train/lora-v1   # 转成 llama.cpp 能挂的 GGUF 适配器

数据: tools/build_lora_data.py 生成的 models/train/data/{train,val}.jsonl, 每条是推理时的整串消息 (系统提示 + 示例 + 本轮)
和目标 JSON. 提示词用基座自带的对话模板拼 (enable_thinking=False, 和 llama-server 一样), 提示词和目标分开分词再拼起来
(推理时目标是模型一个个 token 生成的, 不会和提示词的最后几个字合并), 只在目标和结尾的 <|im_end|> 上算损失.
显存: 只给目标那几十个位置算词表 logits (整段 2.6k token 都算要 2.7 GB).
前缀缓存: 系统提示 + 示例 (约 2.5k token) 每条都一样. 每个优化步用当前的 LoRA 权重把前缀算一遍 (不求梯度), 每条样本从缓存的副本
接着算后面一两百个 token 并反向传播: 前向和整段算完全一样, 只是梯度不经过前缀 (线性注意力层没有快速内核时, 整段算每条 9 秒).
"""
from __future__ import annotations

import argparse
import copy
import gc
import json
import math
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "models" / "train" / "Qwen3.5-0.8B"
DATA = ROOT / "models" / "train" / "data"
CONVERT = ROOT / "runtime" / "llama-convert"


def load_rows(name: str) -> list[dict]:
    return [json.loads(line) for line in (DATA / f"{name}.jsonl").read_text(encoding="utf-8").splitlines() if line]


def encode(tok, row: dict, max_len: int) -> tuple[list[int], int] | None:
    """-> (token ids, 目标从第几个开始). 超长的丢掉."""
    prompt = tok.apply_chat_template(row["messages"], tokenize=False, add_generation_prompt=True,
                                     chat_template_kwargs={"enable_thinking": False})
    p = tok(prompt, add_special_tokens=False)["input_ids"]
    t = tok(row["target"] + "<|im_end|>", add_special_tokens=False)["input_ids"]
    return (p + t, len(p)) if len(p) + len(t) <= max_len else None


def check_format(endpoint: str) -> None:
    """同一串消息: 基座对话模板拼出的提示词 / 分词结果, 和 llama-server 的 /apply-template、/tokenize 比较."""
    import urllib.request

    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(BASE)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(path, body):
        req = urllib.request.Request(endpoint + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        return json.loads(opener.open(req, timeout=60).read())

    rows = load_rows("val")[:20]
    same_text = same_ids = 0
    for row in rows:
        mine = tok.apply_chat_template(row["messages"], tokenize=False, add_generation_prompt=True,
                                       chat_template_kwargs={"enable_thinking": False})
        theirs = post("/apply-template", {"messages": row["messages"], "chat_template_kwargs": {"enable_thinking": False}})["prompt"]
        same_text += mine == theirs
        if mine != theirs:
            i = next(i for i, (a, b) in enumerate(zip(mine, theirs)) if a != b) if any(a != b for a, b in zip(mine, theirs)) else min(len(mine), len(theirs))
            print("提示词不同, 位置", i, repr(mine[i - 40:i + 40]), "vs", repr(theirs[i - 40:i + 40]))
        ids = tok(theirs, add_special_tokens=False)["input_ids"]
        same_ids += ids == post("/tokenize", {"content": theirs, "add_special": False})["tokens"]
    print(f"提示词一致 {same_text}/{len(rows)}, 分词一致 {same_ids}/{len(rows)}")


def load_model(dtype):
    import torch
    from transformers import AutoConfig, AutoModelForCausalLM, AutoModelForImageTextToText

    cfg = AutoConfig.from_pretrained(BASE)
    try:
        model = AutoModelForCausalLM.from_pretrained(BASE, dtype=dtype)
    except (ValueError, KeyError):
        model = AutoModelForImageTextToText.from_pretrained(BASE, dtype=dtype)  # 只喂文字, 视觉部分不参与
    print("模型", type(model).__name__, cfg.model_type)
    return model.to("cuda" if torch.cuda.is_available() else "cpu")


def lora_targets(model) -> list[str]:
    """语言模型解码层里的全部线性层 (注意力、线性注意力、MLP 的投影), 不含视觉部分、词嵌入和输出头."""
    import torch.nn as nn

    names = [n for n, m in model.named_modules() if isinstance(m, nn.Linear) and ".layers." in n
             and "visual" not in n and "vision" not in n and "mtp" not in n]
    suffixes = sorted({n.rsplit(".", 1)[-1] for n in names})
    print(f"LoRA 挂在 {len(names)} 个线性层上: {', '.join(suffixes)}")
    return names


def prefix_of(tok, rows: list[dict]) -> str:
    """所有样本共同的前缀 (到最后一个用户回合之前): 系统提示 + 示例."""
    prompts = [tok.apply_chat_template(r["messages"], tokenize=False, add_generation_prompt=True,
                                       chat_template_kwargs={"enable_thinking": False}) for r in rows[:50]]
    pre = prompts[0][: prompts[0].rfind("<|im_start|>user\n")]
    assert all(p.startswith(pre) for p in prompts), "样本的前缀不一样"
    return pre


def prefix_cache(model, prefix_ids, device):
    import torch

    with torch.no_grad():
        out = model(input_ids=torch.tensor([prefix_ids], device=device), use_cache=True, logits_to_keep=1)
    return out.past_key_values


def cached_loss(model, cache, ids, start, P, device):
    """前缀用缓存, 只算 ids[P:] 这一段; 损失和 target_loss 一样只在目标位置上算."""
    import torch
    import torch.nn.functional as F

    x = torch.tensor([ids[P:]], device=device)
    n = len(ids) - start
    # 线性注意力层算完会把状态原地写回缓存 (copy_), 反向传播还要用原来的值; 训练时不需要写回, 副本上换成只返回结果.
    # 函数里只引用层列表、不引用副本本身: 引用自己会成循环引用, 副本 (约 50 MB) 要等垃圾回收才释放, v1 训练时显存因此一路涨到 6 GB
    c = copy.deepcopy(cache)
    layers = c.layers
    c.update_recurrent_state = lambda states, layer_idx, *a, **k: states
    c.update_conv_state = lambda states, layer_idx, state_idx=0, **k: torch.cat([layers[layer_idx].conv_states[state_idx], states], dim=-1)
    out = model(input_ids=x, past_key_values=c, use_cache=True, logits_to_keep=n + 1)
    return F.cross_entropy(out.logits[0, :-1].float(), x[0, start - P:])


def target_loss(model, ids, start, device):
    """只给目标位置算 logits 和交叉熵."""
    import torch
    import torch.nn.functional as F

    x = torch.tensor([ids], device=device)
    n = len(ids) - start  # 目标 token 数; 预测它们要用前一个位置的输出
    out = model(input_ids=x, logits_to_keep=n + 1, use_cache=False)
    logits = out.logits[0, :-1].float()  # 位置 start-1 .. len-2 预测 start .. len-1
    return F.cross_entropy(logits, x[0, start:])


def train(args) -> None:
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoTokenizer

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    tok = AutoTokenizer.from_pretrained(BASE)
    train_rows, val_rows = load_rows("train"), load_rows("val")
    train_set = [e for r in train_rows if (e := encode(tok, r, args.max_len))]
    val_set = [e for r in val_rows if (e := encode(tok, r, args.max_len))][: args.val_n]
    print(f"训练 {len(train_set)} 条, 验证 {len(val_set)} 条, 平均长度 {sum(len(i) for i, _ in train_set) / len(train_set):.0f} token")
    pre = prefix_of(tok, train_rows)
    prefix_ids = tok(pre, add_special_tokens=False)["input_ids"]
    P = len(prefix_ids)
    assert all(ids[:P] == prefix_ids for ids, _ in train_set + val_set), "分词后前缀对不上"
    print(f"公共前缀 {P} token, 每条只算后面平均 {sum(len(i) for i, _ in train_set) / len(train_set) - P:.0f} token")
    model = load_model(torch.bfloat16)
    lcfg = LoraConfig(r=args.rank, lora_alpha=args.rank * 2, lora_dropout=0.05, target_modules=lora_targets(model), bias="none")
    model = get_peft_model(model, lcfg)
    model.print_trainable_parameters()
    device = next(model.parameters()).device
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=args.lr, weight_decay=0.0)
    steps = math.ceil(len(train_set) * args.epochs / args.accum)
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, (s + 1) / max(1, steps // 20)) * 0.5 * (1 + math.cos(math.pi * min(1.0, s / steps))))

    def evaluate() -> float:
        model.eval()
        with torch.no_grad():
            cache = prefix_cache(model, prefix_ids, device)
            loss = sum(cached_loss(model, cache, ids, st, P, device).item() for ids, st in val_set) / len(val_set)
        model.train()
        return loss

    if args.check_cache:  # 前缀缓存和整段算的损失应该一样 (只差 bf16 的舍入)
        model.eval()
        with torch.no_grad():
            cache = prefix_cache(model, prefix_ids, device)
            for ids, st in val_set[:4]:
                print(f"整段 {target_loss(model, ids, st, device).item():.5f}  前缀缓存 {cached_loss(model, cache, ids, st, P, device).item():.5f}")
        return

    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    log = (out / "train_log.jsonl").open("a", encoding="utf-8")
    best = evaluate()
    print(f"训练前 验证损失 {best:.4f}")
    log.write(json.dumps({"step": 0, "val": best}) + "\n")
    model.train()
    step, t0, run = 0, time.time(), 0.0
    cache = prefix_cache(model, prefix_ids, device)
    order = [i for _ in range(args.epochs) for i in random.sample(range(len(train_set)), len(train_set))]
    for k, i in enumerate(order):
        if args.max_steps and step >= args.max_steps:
            print(f"试跑 {step} 步: {(k) / (time.time() - t0):.2f} 条/秒, 显存峰值 {torch.cuda.max_memory_allocated() / 2**30:.2f} GB")
            return
        ids, st = train_set[i]
        loss = cached_loss(model, cache, ids, st, P, device) / args.accum
        loss.backward()
        run += loss.item()
        if (k + 1) % args.accum == 0 or k == len(order) - 1:
            torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
            opt.step(), sched.step(), opt.zero_grad(set_to_none=True)
            step += 1
            del cache
            gc.collect()  # 万一还有循环引用的副本, 换前缀缓存前清掉
            cache = prefix_cache(model, prefix_ids, device)  # LoRA 权重变了, 前缀重新算
            if step % 10 == 0:
                rate = (k + 1) / (time.time() - t0)
                print(f"步 {step}/{steps} 训练损失 {run / 10:.4f} lr {sched.get_last_lr()[0]:.2e} "
                      f"{rate:.2f} 条/秒 剩约 {(len(order) - k - 1) / rate / 60:.0f} 分钟 显存 {torch.cuda.memory_allocated() / 2**30:.2f} GB "
                      f"(峰值 {torch.cuda.max_memory_allocated() / 2**30:.2f}, 缓存池 {torch.cuda.memory_reserved() / 2**30:.2f})",
                      flush=True)
                log.write(json.dumps({"step": step, "train": run / 10}) + "\n")
                run = 0.0
            if step % args.eval_every == 0 or k == len(order) - 1:
                v = evaluate()
                print(f"步 {step} 验证损失 {v:.4f}", flush=True)
                log.write(json.dumps({"step": step, "val": v}) + "\n")
                log.flush()
                if v < best:
                    best = v
                    model.save_pretrained(out)
                    print(f"  保存 (验证损失最低) -> {args.out}")
    (out / "train_args.json").write_text(json.dumps(vars(args), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"完成, 最低验证损失 {best:.4f}")


def export(adapter: str) -> None:
    """PEFT 适配器 -> GGUF (llama-server --lora 挂在原来的 Q8_0 模型上)."""
    src = ROOT / adapter
    out = src / "lora.gguf"
    env = {"PYTHONPATH": str(CONVERT / "gguf-py")}
    import os
    subprocess.check_call([sys.executable, str(CONVERT / "convert_lora_to_gguf.py"), str(src), "--base", str(BASE),
                           "--outtype", "f16", "--outfile", str(out)], env={**os.environ, **env})
    print(f"已写入 {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check-format", action="store_true")
    ap.add_argument("--endpoint", default="http://127.0.0.1:8784")
    ap.add_argument("--export", help="把这个目录里的 LoRA 转成 GGUF")
    ap.add_argument("--out", default="models/train/lora-v1")
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--accum", type=int, default=8)
    ap.add_argument("--max-len", type=int, default=3072)
    ap.add_argument("--val-n", type=int, default=80)
    ap.add_argument("--eval-every", type=int, default=40)
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--max-steps", type=int, default=0, help="只跑这么多步就停 (试速度和显存)")
    ap.add_argument("--data", default="models/train/data", help="训练数据目录 (build_lora_data.py 的输出)")
    ap.add_argument("--check-cache", action="store_true", help="比较前缀缓存和整段算的损失")
    args = ap.parse_args()
    global DATA
    DATA = ROOT / args.data
    if args.check_format:
        check_format(args.endpoint)
    elif args.export:
        export(args.export)
    else:
        train(args)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    main()
