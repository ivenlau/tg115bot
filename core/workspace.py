"""本地临时工作区：唯一临时路径、预分配、磁盘水位、清理。"""
from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

# 文件名非法字符（含 Windows/Unix 保留符与控制字符）
_ILLEGAL = set('\\/:*?"<>|') | {"'", chr(0), chr(9), chr(10), chr(13)}


class Workspace:
    def __init__(self, work_dir: Path, min_free_gb: int):
        self.root = Path(work_dir)
        self.min_free_bytes = min_free_gb * 1024 ** 3
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, name: str) -> Path:
        safe = "".join("_" if c in _ILLEGAL else c for c in (name or "download")).strip("._") or "download"
        return self.root / f"{safe}.{uuid.uuid4().hex[:8]}.part"

    @staticmethod
    def preallocate(path: Path, size: int) -> None:
        if not size or size <= 0:
            return
        with open(path, "ab") as f:
            try:
                os.posix_fallocate(f.fileno(), 0, size)  # 避免碎片、加速写入
            except (AttributeError, OSError):
                f.truncate(size)

    @staticmethod
    def cleanup(path: Path) -> None:
        try:
            if path and path.exists():
                path.unlink()
        except OSError as e:
            log.warning("清理临时文件失败 %s: %r", path, e)

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free

    def has_enough_space(self, need_bytes: int = 0) -> bool:
        return self.free_bytes() - need_bytes >= self.min_free_bytes
