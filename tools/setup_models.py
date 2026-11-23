"""下载运行库和模型, 校验后解压, 再把找到的路径写进 config/local.json.

  python tools/setup_models.py --list                     # 只列出各组件的文件、来源和大小
  python tools/setup_models.py llama-cuda llm asr tts pip  # 显卡版全套
  python tools/setup_models.py llama-cpu llm               # 只要 CPU 版 LLM
  加 --hf-mirror 走 hf-mirror.com 下载 Qwen 权重.

已下载且校验通过的文件会跳过. 解压时拒绝越出目标目录的路径.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GH_LLAMA = "https://github.com/ggml-org/llama.cpp/releases/download/b11429/"
GH_SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
HF_QWEN = ("https://huggingface.co/ggml-org/Qwen3.5-0.8B-GGUF/resolve/"
           "8fea620810c4afa23dd6443f999a48574c1611a3/Qwen3.5-0.8B-Q8_0.gguf")
SHERPA_VERSION = "1.13.8"
SOULLINK_SHA = "6697822e70d54f21a7b107b9186bf84dd073ed12"
SOULLINK_RAW = f"https://raw.githubusercontent.com/nanlingyin/SoullinkEmotion3D/{SOULLINK_SHA}/"
# 表情引擎 (FaceEngine) + 模型适配 (profile / face-rig) 的完整依赖闭包, 外加许可与出处文件.
SOULLINK_FILES = [
    "LICENSE", "NOTICE", "LICENSES/ICT-FaceKit.txt", "vendor/soullink/LICENSE", "vendor/soullink/SOURCE.json",
    "src/arkit.ts", "src/detailed-vad.ts", "src/directions.ts", "src/emotions.ts", "src/engine.ts",
    "src/face-rig.ts", "src/model-utils.ts", "src/motion.ts", "src/profile.ts",
    *(f"vendor/soullink/{p}" for p in (
        "emotion/EmotionPresetRegistry.ts", "emotion/EmotionStateController.ts", "emotion/VADExpressionMapper.ts",
        "emotion/VADState.ts", "expression/ActionUnitSolver.ts", "expression/EmotionArchetype.ts",
        "expression/EmotionArchetypeRegistry.ts", "facs/ActionUnitUtils.ts", "facs/FACSActionUnitState.ts",
        "facs/FACSLikeState.ts", "facs/FACSUtils.ts", "facs/defaultActionUnitState.ts", "facs/defaultFACSState.ts",
        "reaction/EmotionIntent.ts", "utils/clamp.ts", "utils/easing.ts", "utils/lerp.ts", "utils/randomRange.ts",
        "utils/seededRandom.ts")),
]
JSDELIVR = "https://cdn.jsdelivr.net/npm/"
# HAST-SEC 运动层: 非商业学习研究许可. 只复制运行运动层所需的目录, 来源与修改说明见 docs/third_party/hast-sec/.
HAST_SHA = "f4d5c9aa32f9d04e249585e0b2e6d4c56572ca51"
HAST_KEEP = ["README.md", "LICENSE", "THIRD_PARTY_NOTICES.txt", "licenses", "configs", "face", "hands", "motion",
             "retarget", "training", "web", "models"]
# 移动动捕 (开放空间行动层的运动匹配数据库). 来源与署名见 docs/third_party/mocap/SOURCE.md.
# CMU: 可用于商业产品, 不得转卖数据本身. 69 号受试者: 直走, 边走边转, 原地转身, 90 度转弯, 走过去捡物体.
CMU_69 = {1: 0.37, 2: 0.27, 6: 4.16, 7: 4.18, 8: 4.24, 9: 4.08, 10: 4.59, 11: 3.96, 12: 3.85, 13: 3.8, 14: 3.93,
          15: 3.6, 16: 0.9, 17: 0.8, 18: 0.85, 19: 0.9, 20: 0.44, 21: 0.48, 22: 0.4, 23: 0.45, 24: 0.34, 25: 0.37,
          26: 0.34, 27: 0.34, 28: 0.37, 29: 0.31, 30: 0.31, 31: 0.3, 32: 0.31, 33: 0.32, 68: 0.66, 69: 0.64,
          70: 0.83, 71: 1.05, 72: 0.9, 73: 0.87, 74: 0.78, 75: 0.88}
# CMU 其他受试者: 坐下/坐着/站起 (75, 143, 86, 14, 13) 和前倾伸手 (15), 给物体交互用. 值为 MB.
CMU_ACTIONS = {
    75: {17: 0.26, 18: 0.24, 19: 0.62, 20: 0.4},
    143: {18: 0.6},
    86: {9: 3.82, 10: 6.04, 15: 5.64},
    14: {29: 2.01, 30: 2.05, 31: 1.67, 32: 2.1, 36: 1.67},
    13: {4: 3.81, 5: 3.52, 6: 3.53},
    15: {6: 2.88},
}
# 100STYLE (CC BY 4.0): 整包 1.47 GB, 只按字节范围取下面这些成员 (Zenodo 支持 Range 请求), 用包内 CRC32 校验.
STYLE100_ZIP = "https://zenodo.org/api/records/8127870/files/100STYLE.zip/content"
STYLE100_SIZE = 1468515354
STYLE100_STYLES = {"Neutral": 14.12, "Elated": 11.38, "Depressed": 16.6, "Angry": 10.32}
STYLE100_KINDS = ["FW", "FR", "BW", "BR", "SW", "SR", "ID", "TR1"]


@dataclass
class Asset:
    url: str
    dest: str          # 下载到的文件 (相对仓库根目录)
    size_mb: float
    sha256: str = ""
    extract_to: str = ""  # 非空则解压到这个目录


COMPONENTS: dict[str, list[Asset]] = {
    # sha256 与 HAST-SEC v0.1 assets/resources.json 中固定的版本一致.
    "llama-cuda": [
        Asset(GH_LLAMA + "llama-b11429-bin-win-cuda-12.4-x64.zip", "downloads/llama-cuda.zip", 252,
              "dd6df685c1024e6aa55ca55ad35038692d5e284d4dc895e7a646c3245d3d14ff", "runtime/llama"),
        Asset(GH_LLAMA + "cudart-llama-bin-win-cuda-12.4-x64.zip", "downloads/cudart.zip", 373,
              "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6", "runtime/llama"),
    ],
    "llama-cpu": [
        Asset(GH_LLAMA + "llama-b11429-bin-win-cpu-x64.zip", "downloads/llama-cpu.zip", 18,
              "1283323272b04cd07905816a597a0da810918102de958f4ff6f7bbaa70ed2efe", "runtime/llama"),
    ],
    "llm": [
        Asset(HF_QWEN, "models/llm/Qwen3.5-0.8B-Q8_0.gguf", 795,
              "37ae482d336108d23516fa35e8e0c4126688d81018b87178a18d752a1357814f"),
    ],
    "asr": [
        Asset(GH_SHERPA + "asr-models/sherpa-onnx-streaming-zipformer-small-ctc-zh-int8-2025-04-01.tar.bz2",
              "downloads/asr-zipformer-small-ctc-zh-int8.tar.bz2", 20, "", "models/asr"),
    ],
    "asr-sv": [  # VAD 判定说完 + SenseVoice 整句识别
        Asset(GH_SHERPA + "asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2",
              "downloads/sense-voice-int8-2025-09-09.tar.bz2", 158, "", "models/asr"),
        Asset(GH_SHERPA + "asr-models/silero_vad.onnx", "models/asr/silero_vad.onnx", 1),
    ],
    "soullink": [Asset(SOULLINK_RAW + f, f"web/vendor/soullink3d/{f}", 0.01) for f in SOULLINK_FILES],
    "avatar": [  # 许可见 README "角色模型"
        Asset("https://raw.githubusercontent.com/pixiv/ChatVRM/main/public/AvatarSample_B.vrm",
              "models/vrm/AvatarSample_B.vrm", 20.1,
              "ffbd8c92a9e67c0a948f69c7a2eec91e5c282c9ae70e9184309fc164d74cbc27"),
        Asset("https://raw.githubusercontent.com/madjin/vrm-samples/master/vroid/beta/Sendagaya_Shino.vrm",
              "models/vrm/Sendagaya_Shino.vrm", 14.4,
              "1e177c1a7b14f783a9c48395831db8616260d3bddd4154cb2784b779adca49b5"),
    ],
    "web-libs": [  # 前端改为本地加载, 不再依赖 CDN
        Asset(JSDELIVR + "three@0.180.0/build/three.module.js", "web/vendor/three/build/three.module.js", 0.58),
        Asset(JSDELIVR + "three@0.180.0/build/three.core.js", "web/vendor/three/build/three.core.js", 1.34),
        Asset(JSDELIVR + "three@0.180.0/examples/jsm/loaders/GLTFLoader.js",
              "web/vendor/three/examples/jsm/loaders/GLTFLoader.js", 0.11),
        Asset(JSDELIVR + "three@0.180.0/examples/jsm/utils/BufferGeometryUtils.js",
              "web/vendor/three/examples/jsm/utils/BufferGeometryUtils.js", 0.03),
        Asset(JSDELIVR + "three@0.180.0/LICENSE", "web/vendor/three/LICENSE", 0.01),
        Asset(JSDELIVR + "@pixiv/three-vrm@3.5.5/lib/three-vrm.module.min.js",
              "web/vendor/three-vrm/three-vrm.module.min.js", 0.15),
        Asset(JSDELIVR + "@pixiv/three-vrm@3.5.5/LICENSE", "web/vendor/three-vrm/LICENSE", 0.01),
    ],
    "hast-sec": [  # 身体运动层 (非商业研究), 装到 third_party/hast-sec
        Asset(f"https://codeload.github.com/sola1039/HAST-SEC/zip/{HAST_SHA}", "downloads/hast-sec.zip", 27,
              "", "downloads/hast-sec-src"),
    ],
    "tts": [
        Asset(GH_SHERPA + "tts-models/matcha-icefall-zh-baker.tar.bz2", "downloads/matcha-icefall-zh-baker.tar.bz2",
              71, "", "models/tts"),
        Asset(GH_SHERPA + "vocoder-models/vocos-22khz-univ.onnx", "models/tts/vocos-22khz-univ.onnx", 51),
    ],
    "mocap": [  # 100STYLE 的成员不在这里, 由 fetch_zip_members 按范围下载
        Asset("http://mocap.cs.cmu.edu/subjects/69/69.asf", "models/mocap/cmu/69/69.asf", 0.01),
        *(Asset(f"http://mocap.cs.cmu.edu/subjects/69/69_{t:02d}.amc", f"models/mocap/cmu/69/69_{t:02d}.amc", mb)
          for t, mb in CMU_69.items()),
        *(a for s, trials in CMU_ACTIONS.items() for a in (
            Asset(f"http://mocap.cs.cmu.edu/subjects/{s}/{s}.asf", f"models/mocap/cmu/{s}/{s}.asf", 0.01),
            *(Asset(f"http://mocap.cs.cmu.edu/subjects/{s}/{s}_{t:02d}.amc", f"models/mocap/cmu/{s}/{s}_{t:02d}.amc", mb)
              for t, mb in trials.items()))),
    ],
}
# 动捕补充 (2026-10-24 用户确认, 见 docs/third_party/mocap/SOURCE.md): CMU 其他受试者 36 个文件 35.2 MB, 按用途分组.
# 值为 MB (HEAD 请求查到的). 骨架文件 .asf 每个约 7 KB.
CMU_EXTRA = {
    143: {19: 0.67, 29: 0.86, 25: 0.52},                                       # 坐凳子再起身; 来回踱步; 挥手
    141: {17: 0.45, 29: 0.93, 30: 0.58, 21: 0.2, 26: 0.32, 16: 0.24, 20: 0.61, 13: 0.29},  # 坐凳子; 随意走; 耸肩/屈膝礼/挥手/等待/伸懒腰
    18: {12: 0.99, 8: 1.67, 10: 1.44},                                          # 坐下聊天; 边说边比划; 吵架手势
    19: {12: 1.0, 8: 1.67, 10: 1.44},
    104: {27: 1.94, 14: 0.57, 19: 0.73, 32: 1.25, 39: 2.56, 11: 0.43, 12: 0.34},  # 普通/严厉/慢走 + 直角转弯; 开心起步; 随意走; 开心/兴奋走
    91: {20: 2.22, 13: 2.37, 14: 2.22, 10: 2.55},                               # 害羞/难过/沮丧/慢走
    105: {20: 2.22},                                                            # 害羞走
    13: {14: 1.94},                                                             # 笑
}
COMPONENTS["mocap-extra"] = [a for s, trials in CMU_EXTRA.items() for a in (
    Asset(f"http://mocap.cs.cmu.edu/subjects/{s}/{s}.asf", f"models/mocap/cmu/{s}/{s}.asf", 0.01),
    *(Asset(f"http://mocap.cs.cmu.edu/subjects/{s}/{s}_{t:02d}.amc", f"models/mocap/cmu/{s}/{s}_{t:02d}.amc", mb)
      for t, mb in trials.items()))]
# Audio2Face-3D v2.3 (Mark): 语音 -> 整张脸的动作 (NVIDIA Open Model License, 可商用). 锁定提交, 大文件带 sha256.
A2F_SHA = "5451728e07378df93b04523279e134a9993ae71b"
A2F_URL = f"https://huggingface.co/nvidia/Audio2Face-3D-v2.3-Mark/resolve/{A2F_SHA}/"
A2F_FILES = {
    "network.onnx": (75.75, "dcd16d3b1affb090d4da1ba88791faa6bcd755f97c853b3b667abad9be15cb4b"),
    "model_data.npz": (203.8, "03790f24942ec26796ce101c2e238eb7ff5c1bb94a6c14a840ffcd530d3a301e"),
    "bs_skin.npz": (39.18, "95fbe12499271a9c1ca0d718262fad76d6218f026a450d07006c6b70bc001cc5"),
    "bs_tongue.npz": (1.15, "ae9f0cdade93c0e0037f9e61b125a02709b8eb133eb857d348065a847e5f0d47"),
    "implicit_emo_db.npz": (1.32, "ec07bdf65ea52b6c348e637021a5933b663fae52acf5ff45c11726c1057d5130"),
    **{f: (0.01, "") for f in ("README.md", "a2f_ms_config.json", "bs_skin_config.json", "bs_tongue_config.json",
                                "model.json", "model_config.json", "network_info.json", "trt_info.json")},
}
COMPONENTS["a2f"] = [Asset(A2F_URL + f, f"models/a2f/v2.3-mark/{f}", mb, sha) for f, (mb, sha) in A2F_FILES.items()]
# Kokoro v1.1 多语言 (中文 100+ 音色), sherpa-onnx 打包 (Apache-2.0)
COMPONENTS["tts-kokoro"] = [Asset(GH_SHERPA + "tts-models/kokoro-multi-lang-v1_1.tar.bz2",
                                  "downloads/kokoro-multi-lang-v1_1.tar.bz2", 364, "", "models/tts")]
# 升级复核模型: 小模型 (0.8B) 意图置信度低时请它复核. 放在单独的目录, 不让 detect_config 把它当成主模型.
QWEN2B_SHA = "f6d5376be1edb4d416d56da11e5397a961aca8ae"
COMPONENTS["llm-escalate"] = [Asset(f"https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/{QWEN2B_SHA}/Qwen3.5-2B-Q4_K_M.gguf",
                                    "models/llm-escalate/Qwen3.5-2B-Q4_K_M.gguf", 1281,
                                    "aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223")]
# 记忆的语义检索 (wm/embed.py, 两个都可以装, 配置 memory.embed.engine 选用):
#   embed-bge   BAAI/bge-small-zh-v1.5 (MIT, 512 维), 用已装的 torch/transformers 在 CPU 上跑
#   embed-qwen  Qwen3-Embedding-0.6B Q8_0 (Apache-2.0, 1024 维), 用 llama.cpp 另起一个嵌入服务 (start_llm.py --which embed)
BGE_SHA = "7999e1d3359715c523056ef9478215996d62a620"
BGE_FILES = {"model.safetensors": (91.4, "354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026"),
             **{f: (0.45 if f == "tokenizer.json" else 0.11 if f == "vocab.txt" else 0.03 if f == "README.md" else 0.01, "")
                for f in ("config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.txt",
                          "modules.json", "1_Pooling/config.json", "config_sentence_transformers.json",
                          "sentence_bert_config.json", "README.md")}}
COMPONENTS["embed-bge"] = [Asset(f"https://huggingface.co/BAAI/bge-small-zh-v1.5/resolve/{BGE_SHA}/{f}",
                                 f"models/embed/bge-small-zh-v1.5/{f}", mb, sha) for f, (mb, sha) in BGE_FILES.items()]
QWEN_EMB_SHA = "370f27d7550e0def9b39c1f16d3fbaa13aa67728"
QWEN_EMB_URL = f"https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/{QWEN_EMB_SHA}/"
COMPONENTS["embed-qwen"] = [
    Asset(QWEN_EMB_URL + "Qwen3-Embedding-0.6B-Q8_0.gguf", "models/embed/Qwen3-Embedding-0.6B-Q8_0.gguf", 609.5,
          "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439"),
    Asset(QWEN_EMB_URL + "README.md", "models/embed/Qwen3-Embedding-0.6B-README.md", 0.01)]
# StayStill 待机动作 (MIT, 50 人约 6 小时): 整包 369 MB, 只解压 LaFAN1 骨架的日常待机和几种待机小动作 (BVH 文本解开有几 GB)
STAYSTILL_URL = "https://zenodo.org/api/records/18741736/files/staystill.zip/content"
STAYSTILL_MD5 = "5e07aef93361ea61edcfef66abf82006"
STAYSTILL_ACTIONS = ("wei_lr", "wei_rl", "l_aro", "t_fac", "sc_hea", "sc_arm", "l_wat", "st_arm")
COMPONENTS["staystill"] = [Asset(STAYSTILL_URL, "downloads/staystill.zip", 369),
                           Asset("https://zenodo.org/api/records/18741736/files/LICENSE.txt/content", "models/mocap/staystill/LICENSE.txt", 0.01),
                           Asset("https://zenodo.org/api/records/18741736/files/README.md/content", "models/mocap/staystill/README.md", 0.01)]
# 角色模型转换 (PMX / .blend -> VRM, 只在本机离线跑, 见 tools/convert_vrm.py): Blender 便携版 (GPL, 只当工具用,
# 不影响导出的模型) + mmd_tools (GPL-3, 导入 PMX) + VRM Add-on for Blender (MIT, 导出 VRM). sha256 取自官方发布页.
BLENDER_VERSION = "4.5.15"
GH_ADDON = "https://github.com/{}/releases/download/{}/{}"
COMPONENTS["blender"] = [
    Asset(f"https://download.blender.org/release/Blender4.5/blender-{BLENDER_VERSION}-windows-x64.zip",
          f"downloads/blender-{BLENDER_VERSION}-windows-x64.zip", 399,
          "52dd02fd74877fa08e74b67d7854cf32468a232c02aaa19b6844c91285574f8d", "runtime/blender"),
    Asset(GH_ADDON.format("MMD-Blender/blender_mmd_tools", "v4.5.14", "mmd_tools-v4.5.14-bl4.2.zip"),
          "downloads/blender-addons/mmd_tools-v4.5.14-bl4.2.zip", 0.8,
          "ed3b78184ae9862be0df04e2e147803d011ad067edd8c31029c92fb719e19a6f"),
    Asset(GH_ADDON.format("saturday06/VRM-Addon-for-Blender", "v4.10.2", "VRM_Addon_for_Blender-Extension-4_10_2.zip"),
          "downloads/blender-addons/VRM_Addon_for_Blender-Extension-4_10_2.zip", 1.7,
          "228e622d158fbbf070b1b49547708f360a223873f00fa81a42170794492e6db9"),
]
# 人体姿态估计 (看用户): MediaPipe Pose Landmarker full + tasks-vision 运行库 (都是 Apache-2.0), 浏览器里本地运行.
# 只取 ES 模块和带 SIMD 的 wasm; 运行库的 sha256 取自 jsDelivr, 模型用固定版本号的地址, md5 取自谷歌存储 (见 extract 后的校验)
MP_VISION = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.1.1/"
MP_POSE_MD5 = "83879689d373d143be094c972355e48e"
COMPONENTS["hpe"] = [
    Asset(MP_VISION + "vision_bundle.mjs", "web/vendor/mediapipe/vision_bundle.mjs", 0.16,
          "a84921e02321e582484c1f8126d344b5f33b791fa09c640a52e20ee8632068f1"),
    Asset(MP_VISION + "wasm/vision_wasm_internal.js", "web/vendor/mediapipe/wasm/vision_wasm_internal.js", 0.33,
          "6522286aa32dd93eff17c3da002b0649516f87abfec7a80b5bfe5c9625caa5f8"),
    Asset(MP_VISION + "wasm/vision_wasm_internal.wasm", "web/vendor/mediapipe/wasm/vision_wasm_internal.wasm", 13.5,
          "0e101a010e262ef8e78f22e3c95b60a9705fd06962753c9acd6356d9fb56b7bd"),
    Asset(MP_VISION + "package.json", "web/vendor/mediapipe/package.json", 0.01,
          "ed8a115ed6a703014465111bfe5f7321a94769dc19f6eb7936ece7ae6a8c8c1e"),
    Asset("https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task",
          "models/hpe/pose_landmarker_full.task", 9.4),
]
# LoRA 微调 (tools/train_lora.py, 装进单独的虚拟环境 runtime/train-venv, 不动主环境):
#   train-base     Qwen3.5-0.8B 原始权重 (bf16 safetensors, Apache-2.0), GGUF 不能训练
#   train-convert  llama.cpp b11429 (和 llama-server 同一版本, MIT) 的转换脚本, 把训练出的 LoRA 转成 GGUF
# 大文件按 LFS 的 sha256 校验, 小文件和转换脚本按 git 的 blob sha1 校验 ("git:" 前缀).
QWEN_BASE_SHA = "2fc06364715b967f1860aea9cf38778875588b17"
QWEN_BASE_FILES = {
    "model.safetensors-00001-of-00001.safetensors": (1746.9, "04b1c301231dd422b8860db31311ab2721511346a32cb1e079c4c4e5f1fe4696"),
    "tokenizer.json": (12.8, "5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42"),
    "vocab.json": (6.7, "git:0aa0ce0658d60ac4a5d609f4eadb0e8e43514176"),
    "merges.txt": (3.4, "git:a494e019ca1502219fd0128658b979e5f05ae8e8"),
    "model.safetensors.index.json": (0.05, "git:f691cefdb79d73270895ebd6d9594ddcecfc1838"),
    "README.md": (0.06, "git:5824f1761b2b3a55a2141a9a1172a7f92c7c2ad9"),
    "tokenizer_config.json": (0.02, "git:fae3ce993e07c092ad024dde45e592379fde91bb"),
    "LICENSE": (0.01, "git:f938136e3adacfd92be087f6e113b5d6d97f678f"),
    "chat_template.jinja": (0.01, "git:0ef09f214eaa6d9bca297988afc1454b5827b2c7"),
    "config.json": (0.01, "git:715f0448b9d38103211f0ad88bbb4d6e4f4be8c9"),
    "preprocessor_config.json": (0.01, "git:2ea84a437d448ff71b08df68fdd949d5cc4ebb64"),
    "video_preprocessor_config.json": (0.01, "git:3ba673a5ad7d4d13f54155ecd38b2a94a6dac8fe"),
}
COMPONENTS["train-base"] = [Asset(f"https://huggingface.co/Qwen/Qwen3.5-0.8B/resolve/{QWEN_BASE_SHA}/{f}",
                                  f"models/train/Qwen3.5-0.8B/{f}", mb, sha) for f, (mb, sha) in QWEN_BASE_FILES.items()]
_CONVERT = json.loads((ROOT / "tools" / "llama_convert_files.json").read_text(encoding="utf-8"))
COMPONENTS["train-convert"] = [
    Asset(f"https://raw.githubusercontent.com/{_CONVERT['repo']}/{_CONVERT['tag']}/{f['path']}",
          f"runtime/llama-convert/{f['path']}", round(f["size"] / 1e6, 3), "git:" + f["git_sha1"]) for f in _CONVERT["files"]]
# 用显卡跑 Audio2Face: DirectML 版 onnxruntime (MIT), 和 CPU 版同名, 先卸掉 CPU 版. sherpa-onnx 自带运行库, 不受影响.
PIP_DML = ["onnxruntime-directml==1.23.0"]

ZIP_MEMBERS = {  # 组件 -> (远程 zip, 总大小, 成员列表, 解到哪里, 约多少 MB)
    "mocap": (STYLE100_ZIP, STYLE100_SIZE,
              ["100STYLE/Frame_Cuts.csv"] + [f"100STYLE/{s}/{s}_{k}.bvh" for s in STYLE100_STYLES for k in STYLE100_KINDS],
              "models/mocap", sum(STYLE100_STYLES.values())),
    # 补充 (2026-10-24 用户确认): StartStop 风格的 8 段, 起步、停步的过渡补进中性库
    "mocap-extra": (STYLE100_ZIP, STYLE100_SIZE, [f"100STYLE/StartStop/StartStop_{k}.bvh" for k in STYLE100_KINDS],
                    "models/mocap", 26.6),
}
PIP = ["sherpa-onnx==" + SHERPA_VERSION]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def digest(path: Path, want: str) -> str:
    """按期望值的形式算: "git:<sha1>" 是 git 的 blob 哈希 (GitHub / Hugging Face 的小文件只给这个), 否则 sha256."""
    if want.startswith("git:"):
        data = path.read_bytes()
        return "git:" + hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    return sha256(path)


def download(asset: Asset, hf_mirror: bool) -> Path:
    path = ROOT / asset.dest
    if path.exists() and (not asset.sha256 or digest(path, asset.sha256) == asset.sha256):
        print(f"  已存在, 跳过: {asset.dest}")
        return path
    url = asset.url.replace("https://huggingface.co", "https://hf-mirror.com") if hf_mirror else asset.url
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + ".part")
    print(f"  下载 {url}\n    -> {asset.dest} (约 {asset.size_mb} MB)")
    req = urllib.request.Request(url, headers={"User-Agent": "WorldModel-v1-setup"})
    with urllib.request.urlopen(req, timeout=60) as r, part.open("wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r    {done / total:6.1%}  {done >> 20}/{total >> 20} MB", end="", flush=True)
    print()
    if asset.sha256:
        got = digest(part, asset.sha256)
        if got != asset.sha256:
            part.unlink()
            raise SystemExit(f"校验失败: {asset.dest}\n  期望 {asset.sha256}\n  实际 {got}")
    part.replace(path)
    return path


def fetch_zip_members(url: str, total: int, members: list[str], dest: str) -> None:
    """只下载远程 zip 里的指定成员: 先按范围读中央目录, 再逐个读成员数据并解压, 用包内 CRC32 校验."""
    import struct
    import zlib

    def ranged(a: int, b: int) -> bytes:
        req = urllib.request.Request(url, headers={"Range": f"bytes={a}-{b}", "User-Agent": "WorldModel-v1-setup"})
        with urllib.request.urlopen(req, timeout=120) as r:
            if r.status != 206:
                raise SystemExit(f"服务器不支持范围请求: {url}")
            return r.read()

    base = (ROOT / dest).resolve()
    todo = [m for m in members if not (base / m).exists()]
    if not todo:
        print(f"  已存在, 跳过: {len(members)} 个 zip 成员")
        return
    tail = ranged(total - 65536, total - 1)
    eocd = tail.rfind(b"PK\x05\x06")
    cd_size, cd_off = struct.unpack("<II", tail[eocd + 12:eocd + 20])
    cd, p, index = ranged(cd_off, cd_off + cd_size - 1), 0, {}
    while cd[p:p + 4] == b"PK\x01\x02":
        method, = struct.unpack("<H", cd[p + 10:p + 12])
        crc, comp, size, ln, le, lc = struct.unpack("<IIIHHH", cd[p + 16:p + 34])
        off, = struct.unpack("<I", cd[p + 42:p + 46])
        index[cd[p + 46:p + 46 + ln].decode("utf-8")] = (method, crc, comp, size, off)
        p += 46 + ln + le + lc
    for name in todo:
        if name not in index or not _inside(base, name):
            raise SystemExit(f"zip 里没有这个成员或路径非法: {name}")
        method, crc, comp, size, off = index[name]
        head = ranged(off, off + 29)
        ln, le = struct.unpack("<HH", head[26:30])
        raw = ranged(off + 30 + ln + le, off + 30 + ln + le + comp - 1)
        data = zlib.decompress(raw, -15) if method == 8 else raw
        if len(data) != size or zlib.crc32(data) != crc:
            raise SystemExit(f"校验失败: {name}")
        out = base / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        print(f"  {name}  {comp / 1e6:.2f} MB")


def _inside(base: Path, name: str) -> bool:
    target = (base / name).resolve()
    return target == base or base in target.parents


def extract(archive: Path, dest: str) -> None:
    out = (ROOT / dest).resolve()
    out.mkdir(parents=True, exist_ok=True)
    print(f"  解压 {archive.name} -> {dest}")
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                if not _inside(out, name):
                    raise SystemExit(f"压缩包里有越界路径, 已停止: {name}")
            z.extractall(out)
    else:
        with tarfile.open(archive) as t:
            members = []
            for m in t.getmembers():
                if not _inside(out, m.name) or m.issym() or m.islnk() or m.isdev():
                    raise SystemExit(f"压缩包里有不安全的条目, 已停止: {m.name}")
                members.append(m)
            t.extractall(out, members=members)


def _first(paths, prefer: str = "") -> Path | None:
    paths = sorted(paths)
    for p in paths:
        if prefer and prefer in p.name:
            return p
    return paths[0] if paths else None


def detect_config() -> dict:
    """在 runtime/ 和 models/ 里找已经就位的文件, 生成要合并进 local.json 的配置."""
    rel = lambda p: p.relative_to(ROOT).as_posix()  # noqa: E731
    cfg: dict = {}
    exe = _first((ROOT / "runtime").rglob("llama-server.exe")) if (ROOT / "runtime").exists() else None
    gguf = _first((ROOT / "models" / "llm").glob("*.gguf")) if (ROOT / "models" / "llm").exists() else None
    if exe and gguf:
        cuda = any(exe.parent.glob("ggml-cuda*.dll"))
        cfg["llm"] = {"engine": "llamacpp", "server": {"exe": rel(exe), "model": rel(gguf),
                                                      "device": "CUDA0" if cuda else "CPU"}}
    asr_root = ROOT / "models" / "asr"
    # 流式和整句识别的模型目录里都有 tokens.txt, 按目录名区分.
    tokens = _first(p for p in asr_root.glob("*streaming*/tokens.txt")) if asr_root.exists() else None
    if tokens:
        model = _first(tokens.parent.glob("*.onnx"), prefer="int8")
        if model:
            cfg["asr"] = {"engine": "sherpa", "sherpa": {"model_type": "zipformer2_ctc",
                                                         "tokens": rel(tokens), "model": rel(model)}}
    sv_tokens = _first(p for p in asr_root.glob("*sense-voice*/tokens.txt")) if asr_root.exists() else None
    vad = asr_root / "silero_vad.onnx"
    if sv_tokens and vad.exists():
        model = _first(sv_tokens.parent.glob("*.onnx"), prefer="int8")
        if model:
            # 装了 SenseVoice 就作为默认 ASR (端点更快更稳, 识别更准); 流式 ASR 留作 --asr sherpa 备选.
            asr = cfg.setdefault("asr", {})
            asr["engine"] = "vad_sensevoice"
            asr["vad_sensevoice"] = {"model": rel(model), "tokens": rel(sv_tokens), "vad_model": rel(vad)}
    tts_root = ROOT / "models" / "tts"
    lexicon = _first(tts_root.rglob("lexicon.txt")) if tts_root.exists() else None
    vocoder = _first(tts_root.glob("vocos*.onnx")) if tts_root.exists() else None
    if lexicon and vocoder:
        d = lexicon.parent
        acoustic = _first(d.glob("model*.onnx"))
        fsts = ",".join(rel(p) for p in sorted(d.glob("*.fst")))
        if acoustic:
            cfg["tts"] = {"engine": "sherpa", "sherpa": {
                "model_type": "matcha", "acoustic_model": rel(acoustic), "vocoder": rel(vocoder),
                "lexicon": rel(lexicon), "tokens": rel(d / "tokens.txt"), "rule_fsts": fsts}}
    vrm_dir = ROOT / "models" / "vrm"
    vrms = sorted(vrm_dir.glob("*.vrm")) if vrm_dir.exists() else []
    if vrms:
        preferred = next((p for p in vrms if p.name == "AvatarSample_B.vrm"), vrms[0])
        cfg["avatar"] = {"default": preferred.name}
    return cfg


def merge_local(update: dict) -> None:
    sys.path.insert(0, str(ROOT))
    from wm.config import deep_merge
    path = ROOT / "config" / "local.json"
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    path.write_text(json.dumps(deep_merge(current, update), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已更新 {path.relative_to(ROOT)}:")
    print(json.dumps(update, ensure_ascii=False, indent=2))


def extract_staystill() -> None:
    """校验 md5, 只解出 LaFAN1 骨架的 Idle 和选定的 Actions (BVH)."""
    import hashlib
    import zipfile
    src = ROOT / "downloads" / "staystill.zip"
    h = hashlib.md5()
    with src.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    if h.hexdigest() != STAYSTILL_MD5:
        raise SystemExit(f"staystill.zip md5 不对: {h.hexdigest()}")
    dst = ROOT / "models" / "mocap" / "staystill"
    n = 0
    with zipfile.ZipFile(src) as z:
        for info in z.infolist():
            parts = info.filename.replace("\\", "/").split("/")
            low = [x.lower() for x in parts]
            if not info.filename.lower().endswith(".bvh") or "lafan" not in low:
                continue
            name = parts[-1]
            if "idle" in low or ("actions" in low and name.startswith(STAYSTILL_ACTIONS)):
                sub = "Idle" if "idle" in low else "Actions"
                out = dst / sub / name
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as fi, out.open("wb") as fo:
                    fo.write(fi.read())
                n += 1
    print(f"  解出 {n} 个 BVH -> {dst.relative_to(ROOT)}")


def blender_exe() -> Path:
    return ROOT / "runtime" / "blender" / f"blender-{BLENDER_VERSION}-windows-x64" / "blender.exe"


def install_blender_addons() -> None:
    """Blender 用便携模式 (配置和插件放在它自己的目录里, 不碰用户的 AppData), 装上 mmd_tools 和 VRM 插件."""
    exe = blender_exe()
    (exe.parent / "portable").mkdir(exist_ok=True)
    for a in COMPONENTS["blender"][1:]:
        subprocess.check_call([str(exe), "--background", "--factory-startup", "--command", "extension",
                               "install-file", "-r", "user_default", "--enable", str(ROOT / a.dest)])
    print(f"  Blender 插件已装到 {(exe.parent / 'portable').relative_to(ROOT)}")


def install_hast() -> None:
    """从解压的源码包里只取运动层需要的目录, 原样复制 (不修改), 再放入来源与修改说明."""
    import shutil
    src = ROOT / "downloads" / "hast-sec-src" / f"HAST-SEC-{HAST_SHA}"
    dst = ROOT / "third_party" / "hast-sec"
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for name in HAST_KEEP:
        (shutil.copytree if (src / name).is_dir() else shutil.copy2)(src / name, dst / name)
    for note in (ROOT / "docs" / "third_party" / "hast-sec").iterdir():
        shutil.copy2(note, dst / note.name)
    print(f"  HAST-SEC 已安装到 {dst.relative_to(ROOT)} (仅限非商业研究, 见其 LICENSE)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("components", nargs="*", help=" ".join(list(COMPONENTS) + ["pip", "ort-dml"]))
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--hf-mirror", action="store_true")
    ap.add_argument("--detect-only", action="store_true", help="不下载, 只根据已有文件更新 local.json")
    ap.add_argument("--no-config", action="store_true",
                    help="下载完不改 local.json (自动检测会把手动设的模型路径、默认形象等改回检测到的值)")
    args = ap.parse_args()

    if args.list or not (args.components or args.detect_only):
        for name, assets in COMPONENTS.items():
            zipped = ZIP_MEMBERS.get(name)
            extra = zipped[4] if zipped else 0
            print(f"{name}  (共约 {sum(a.size_mb for a in assets) + extra:.1f} MB)")
            for a in assets:
                print(f"  {a.size_mb:>6.2f} MB  {a.url}")
            if zipped:
                print(f"  {extra:>6.2f} MB  {len(zipped[2])} 个成员, 按范围取自 {zipped[0]}")
        print(f"pip  pip install {' '.join(PIP)}")
        print(f"ort-dml  pip uninstall onnxruntime && pip install {' '.join(PIP_DML)}  (约 25 MB)")
        return

    for name in args.components:
        if name == "pip":
            subprocess.check_call([sys.executable, "-m", "pip", "install", *PIP])
            continue
        if name == "ort-dml":
            subprocess.call([sys.executable, "-m", "pip", "uninstall", "-y", "onnxruntime"])
            subprocess.check_call([sys.executable, "-m", "pip", "install", *PIP_DML])
            continue
        if name not in COMPONENTS:
            raise SystemExit(f"未知组件: {name}")
        print(f"[{name}]")
        for asset in COMPONENTS[name]:
            path = download(asset, args.hf_mirror)
            if asset.extract_to:
                extract(path, asset.extract_to)
        if name in ZIP_MEMBERS:
            url, total, members, dest, _ = ZIP_MEMBERS[name]
            fetch_zip_members(url, total, members, dest)
        if name == "hast-sec":
            install_hast()
        if name == "staystill":
            extract_staystill()
        if name == "blender":
            install_blender_addons()
        if name == "hpe":  # 模型没有公布 sha256, 用谷歌存储给的 md5 校验
            got = hashlib.md5((ROOT / "models/hpe/pose_landmarker_full.task").read_bytes()).hexdigest()
            if got != MP_POSE_MD5:
                (ROOT / "models/hpe/pose_landmarker_full.task").unlink()
                raise SystemExit(f"pose_landmarker_full.task md5 不对: {got}")
            print("  姿态模型 md5 校验通过")
    if args.no_config:
        print("--no-config: local.json 未改动.")
        return
    update = detect_config()
    if update:
        merge_local(update)
    else:
        print("没有检测到可用的模型文件, local.json 未改动.")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    main()
