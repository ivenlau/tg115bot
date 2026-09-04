"""本地临时工作区：唯一临时路径、预分配、磁盘水位、清理、副本保留。"""
from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# 文件名非法字符（含 Windows/Unix 保留符与控制字符）
_ILLEGAL = set('\\/:*?"<>|') | {"'", chr(0), chr(9), chr(10), chr(13)}


class Workspace:
    def __init__(self, work_dir: Path, min_free_gb: int, keep_local: bool = False):
        self.root = Path(work_dir)
        self.min_free_bytes = min_free_gb * 1024 ** 3
        self.keep_local = keep_local
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, name: str) -> Path:
        safe = "".join("_" if c in _ILLEGAL else c for c in (name or "download")).strip("._") or "download"
        return self.root / f"{safe}.{uuid.uuid4().hex[:8]}.part"

    def keep_copy(self, tmp: Path, final_name: str) -> Optional[Path]:
        """把上传完成的临时文件转存为副本（去掉 uuid 后缀，还原原名）。

        存到 <work_dir>/copies/；同名冲突加 (1)/(2) 序号；失败返回 None
        （调用方负责清理原临时文件，行为与未开启开关时一致）。
        """
        try:
            copies = self.root / "copies"
            copies.mkdir(parents=True, exist_ok=True)
            safe = "".join("_" if c in _ILLEGAL else c for c in (final_name or tmp.name)).strip("._")
            dst = copies / (safe or tmp.name)
            if dst.exists():
                stem, suffix = dst.stem, dst.suffix
                for i in range(1, 10000):
                    cand = copies / f"{stem} ({i}){suffix}"
                    if not cand.exists():
                        dst = cand
                        break
            shutil.move(str(tmp), dst)
            log.info("已保留本地副本: %s", dst)
            return dst
        except OSError as e:
            log.warning("保留副本失败 %s: %r", tmp, e)
            return None

    def finalize(self, tmp: Path, final_name: str, succeeded: bool) -> None:
        """任务收尾本地临时文件（替代已移除的 upload.delete_after_upload）。

        keep_local=true：成功→转存原名副本（转存失败才清理，云端已有不丢数据）；
                          失败→原样保留 .part 现场供排查/续用。
        keep_local=false：一律清理。
        """
        if self.keep_local:
            if succeeded:
                if self.keep_copy(tmp, final_name) is None:
                    self.cleanup(tmp)
            return
        self.cleanup(tmp)

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
