"""HAST-SEC 运动层的文件服务 (非商业研究许可, 见 third_party/hast-sec/LICENSE).

上游前端模块里写死了一批绝对路径 (/configs/..., /v15-data/..., /v15-api/bootstrap 等), 这里按上游 Node 服务
(service/public_routes.mjs, public_api.mjs, v15_library.mjs) 相同的规则提供, 这样上游文件一行都不用改.
模块本身挂在 /hast/ 下, 它们之间的相对 import 照常工作.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from aiohttp import web

from .config import resolve_path

MODULE_DIRS = ("web", "motion", "face", "hands", "retarget", "training")
# 上游 public_routes.mjs: URL 前缀 -> models/ 下的子目录
MODEL_PREFIXES = {
    "baseline-v9-filtered-data": "body",
    "feasible-history": "history",
    "v15-data": "control",
    "v14-data": "timing",
    "v14-policy": "policy",
}
TYPES = {".mjs": "text/javascript", ".js": "text/javascript", ".json": "application/json",
         ".bin": "application/octet-stream"}
_NAME = re.compile(r"^[\w.\-]+$", re.ASCII)


def hast_dir(cfg: dict) -> Path:
    return resolve_path(cfg.get("motion", {}).get("hast_dir", "third_party/hast-sec"))


def available(cfg: dict) -> bool:
    root = hast_dir(cfg)
    return cfg.get("motion", {}).get("engine", "hast") == "hast" and (root / "motion" / "v9" / "controller.mjs").is_file()


def build_library(root: Path) -> dict:
    """等价于上游 publicApi 的 library + extraLibrary: 基础技能 + 第十五版新增的挥手/抬腿."""
    read = lambda p: json.loads((root / p).read_text(encoding="utf-8"))  # noqa: E731
    manifest = read("models/skills/manifest.json")
    classes = [read(f"models/skills/{c['file']}") for c in manifest["classes"]]
    sources = [s for i in range(4) for s in read(f"models/skills/sources-{i}.json")]
    extra = read("models/control/skills.json")
    if extra["registry_sha256"] != manifest["registry_sha256"] or extra["bones"] != manifest["bones"]:
        raise ValueError("HAST-SEC 新增技能的素材审批或骨架不一致")
    classes += [read(f"models/control/{c['file']}") for c in extra["classes"]]
    return {**manifest, "classes": classes, "sources": sources, "version": 15}


def _file(path: Path) -> web.FileResponse:
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Content-Type": TYPES.get(path.suffix, "application/octet-stream"),
                                           "Cache-Control": "no-store"})


def _inside(base: Path, rel: str) -> Path:
    parts = rel.split("/")
    if not rel or any(not _NAME.match(p) for p in parts):
        raise web.HTTPNotFound()
    path = (base / Path(*parts)).resolve()
    if base.resolve() not in path.parents:
        raise web.HTTPNotFound()
    return path


def register(app: web.Application, cfg: dict) -> bool:
    if not available(cfg):
        return False
    root = hast_dir(cfg)
    library = build_library(root)
    sources = {s["id"]: s["file"] for s in library["sources"]}

    async def module(request: web.Request):
        rel = request.match_info["tail"]
        if rel.split("/")[0] not in MODULE_DIRS or Path(rel).suffix not in (".mjs", ".js", ".json"):
            raise web.HTTPNotFound()
        return _file(_inside(root, rel))

    async def configs(request: web.Request):
        name = request.match_info["name"]
        if not name.endswith(".json"):
            raise web.HTTPNotFound()
        return _file(_inside(root / "configs", name))

    def model_route(folder: str):
        async def handler(request: web.Request):
            return _file(_inside(root / "models" / folder, request.match_info["name"]))
        return handler

    async def trajectory(request: web.Request):
        file = sources.get(request.match_info["id"])
        if not file:
            raise web.HTTPNotFound()
        return _file(_inside(root / "models" / "trajectories", file))

    async def bootstrap(request: web.Request):
        return web.json_response(library, headers={"Cache-Control": "no-store"})

    async def source_filter(request: web.Request):
        return _file(root / "models" / "body" / "source-filter.json")

    app.router.add_get("/hast/{tail:.+}", module)
    app.router.add_get("/configs/{name}", configs)
    for prefix, folder in MODEL_PREFIXES.items():
        app.router.add_get(f"/{prefix}/{{name}}", model_route(folder))
    app.router.add_get("/v13-sources/{id}.bin", trajectory)
    app.router.add_get("/v15-api/bootstrap", bootstrap)
    app.router.add_get("/v13-api/bootstrap", bootstrap)
    app.router.add_get("/semantic-api/source-filter", source_filter)
    return True
