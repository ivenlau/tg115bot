"""Workspace.finalize 收尾策略测试：keep_local × 成败 四象限（纯本地，零网络）。"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.workspace import Workspace  # noqa: E402


def _mk(keep_local: bool) -> tuple[Workspace, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="tg115_ws_"))
    return Workspace(tmp, min_free_gb=0, keep_local=keep_local), tmp


def _put(ws: Workspace, name: str = "a.mkv") -> Path:
    f = ws.path_for(name)
    f.write_bytes(b"x")
    return f


def test_finalize_default_cleans_all() -> None:
    """keep_local=false：成功/失败都清理（默认行为与旧 delete_after_upload=true 一致）。"""
    ws, root = _mk(keep_local=False)
    try:
        for ok in (True, False):
            f = _put(ws)
            ws.finalize(f, "a.mkv", succeeded=ok)
            assert not f.exists(), f"succeeded={ok} 应清理临时文件"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_finalize_keep_local_success_moves_copy() -> None:
    """keep_local=true + 成功：副本还原原名进 copies/，原临时路径消失。"""
    ws, root = _mk(keep_local=True)
    try:
        f = _put(ws, "movie.mkv")
        ws.finalize(f, "movie.mkv", succeeded=True)
        assert not f.exists(), "临时文件应已被转存"
        assert (root / "copies" / "movie.mkv").read_bytes() == b"x"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_finalize_keep_local_failure_keeps_part() -> None:
    """keep_local=true + 失败：原样保留 .part 现场（排查/续用）。"""
    ws, root = _mk(keep_local=True)
    try:
        f = _put(ws, "movie.mkv")
        ws.finalize(f, "movie.mkv", succeeded=False)
        assert f.exists(), "失败现场应保留"
        assert f.name.endswith(".part")
        assert not (root / "copies").exists() or not any((root / "copies").iterdir()), \
            "失败不应产生副本"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_finalize_copy_conflict_gets_index() -> None:
    """副本同名冲突自动加 (1) 序号（沿用 keep_copy 行为，不覆盖）。"""
    ws, root = _mk(keep_local=True)
    try:
        f1, f2 = _put(ws, "same.mkv"), _put(ws, "same.mkv")
        ws.finalize(f1, "same.mkv", succeeded=True)
        ws.finalize(f2, "same.mkv", succeeded=True)
        assert (root / "copies" / "same.mkv").exists()
        assert (root / "copies" / "same (1).mkv").exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)
