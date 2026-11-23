"""Server-owned scene selection and independent room experience stores."""
from __future__ import annotations

import threading

from . import layout, store
from .live import LiveRoom


class RoomRegistry:
    def __init__(self, cfg: dict, on_learn=None):
        self.cfg, self.on_learn = cfg, on_learn
        self.base = store.data_dir(cfg)
        self.generation = store.generation(self.base)
        self.lock = threading.RLock()
        self.rooms: dict[str, LiveRoom] = {}

    def get(self, scene_id=None) -> LiveRoom:
        selected = layout.scene_id(scene_id)
        with self.lock:
            if selected not in self.rooms:
                public = layout.load_scene(selected)
                folder = store.scene_dir(self.cfg, selected)
                rc = (self.cfg.get("room") or {}).get("cognition") or {}
                # Long-term summaries retain scene provenance rather than claiming
                # a location learned in another house. Old wording stays unchanged.
                callback = self.on_learn
                if callback and selected != layout.DEFAULT_SCENE_ID:
                    callback = lambda text, cb=callback, sid=selected: cb(f"[{sid}] {text}")
                self.rooms[selected] = LiveRoom(folder / "room-events.jsonl", carry_path=folder / "carry-events.jsonl",
                                               afford_path=folder / "afford-events.jsonl", layout=public,
                                               policy=rc.get("policy", "experience"), learn_after=int(rc.get("learn_after", 2)),
                                               on_learn=callback, generation_dir=self.base)
                self.rooms[selected].generation = max(self.rooms[selected].generation, self.generation)
            return self.rooms[selected]

    def set_generation(self, generation: int) -> None:
        with self.lock:
            self.generation = generation
            for room in self.rooms.values():
                with room.lock:
                    room.generation = generation

    def forget(self, generation: int | None = None) -> list[str]:
        with self.lock:
            if generation is not None:
                self.generation = generation
            gone = []
            for room in self.rooms.values():
                for name in room.forget(generation):
                    gone.append((room.path.parent / name).relative_to(self.base).as_posix())
            gone.extend(store.delete(self.base))  # Also clear known scenes not opened this run.
            return list(dict.fromkeys(gone))

    def status(self) -> dict:
        with self.lock:
            return {key: value.status() for key, value in self.rooms.items()}
