"""文件整理：重命名模板 + 按扩展名归类子目录。

纯逻辑，不依赖任何第三方库，便于单测。pipeline 在下载前调用 ``organize`` 得到
最终上传用的 (filename, target_dir)。

模板变量（``str.format_map``，缺失变量渲染为空串而非报错）：
  {filename}  原始文件名（含扩展名）
  {name}      去扩展名的主名
  {ext}       扩展名（含点，如 .mp4；无扩展名则为空）
  {date}      YYYYMMDD
  {time}      HHMMSS
  {datetime}  YYYYMMDD_HHMMSS
  {size}      文件字节数

按扩展名归类（classify_by_ext=true）：在 target_dir 下追加分类子目录，
如 /tg115bot/videos、/tg115bot/audio …
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

# 扩展名 -> 分类目录名（小写匹配）
EXT_CATEGORIES = {
    "videos": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
               ".m4v", ".ts", ".rmvb", ".rm", ".mpg", ".mpeg", ".3gp"},
    "audio": {".mp3", ".flac", ".aac", ".ogg", ".wav", ".m4a", ".wma", ".opus", ".ape"},
    "images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".heic", ".svg"},
    "documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                  ".txt", ".epub", ".mobi", ".azw3", ".djvu"},
    "archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"},
}

OTHER_CATEGORY = "others"

# 文件名非法字符（与 core/workspace 一致，避免 115/本地路径出错）
_ILLEGAL = set('\\/:*?"<>|') | {chr(0), chr(9), chr(10), chr(13)}


def classify_subdir(filename: str) -> str:
    """按扩展名返回分类子目录名（videos/audio/.../others）。"""
    ext = Path(filename).suffix.lower()
    for cat, exts in EXT_CATEGORIES.items():
        if ext in exts:
            return cat
    return OTHER_CATEGORY


def _safe_format(template: str, mapping: dict) -> str:
    """``str.format_map``，缺失键渲染为空串，非法格式原样返回。"""
    class _Default(dict):
        def __missing__(self, k: str) -> str:  # type: ignore[override]
            return ""
    try:
        return template.format_map(_Default(mapping))
    except (IndexError, ValueError, KeyError):
        return template


def render_name(template: str, filename: str, *, size: int = 0,
                now: Optional[datetime] = None) -> str:
    """按模板渲染新文件名。``now`` 为 None 时取当前时间。"""
    now = now or datetime.now()
    p = Path(filename)
    mapping = {
        "filename": filename,
        "name": p.stem,
        "ext": p.suffix,
        "date": now.strftime("%Y%m%d"),
        "time": now.strftime("%H%M%S"),
        "datetime": now.strftime("%Y%m%d_%H%M%S"),
        "size": str(size),
    }
    return _safe_format(template or "{filename}", mapping)


def _sanitize(name: str) -> str:
    safe = "".join("_" if c in _ILLEGAL else c for c in (name or "")).strip()
    return safe or "file"


def organize(
    filename: str,
    target_dir: str,
    *,
    rename_template: str = "{filename}",
    classify_by_ext: bool = False,
    size: int = 0,
    now: Optional[datetime] = None,
) -> Tuple[str, str]:
    """返回 (最终文件名, 最终目标目录)。

    - 渲染重命名模板并做文件名净化（保证非空、无非法字符、保留扩展名）；
    - classify_by_ext=True 时在 target_dir 末尾追加分类子目录。
    """
    new_name = render_name(rename_template, filename, size=size, now=now)
    # 模板可能丢掉扩展名 -> 若新名无扩展名而原名有，则补回，避免 115 端识别错误
    new_name = _ensure_ext(new_name, filename)
    new_name = _sanitize(new_name)

    base = (target_dir or "").rstrip("/")
    if classify_by_ext:
        sub = classify_subdir(filename)
        new_dir = f"{base}/{sub}" if base else sub
    else:
        new_dir = base or "/"
    return new_name, new_dir


def _ensure_ext(new_name: str, original: str) -> str:
    new_ext = Path(new_name).suffix.lower()
    orig_ext = Path(original).suffix.lower()
    if orig_ext and not new_ext:
        return f"{new_name}{orig_ext}"
    return new_name
