"""Owned partial files and case-insensitive target allocation without Qt/SQL."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from danbooru_multi_search.core.downloads import DownloadConflictPolicy, DownloadError, DownloadItemSnapshot, filename_key


@dataclass(frozen=True, slots=True)
class DownloadDestination:
    path: Path
    skipped: bool = False
    blocked: bool = False


def target_key(path: Path) -> str:
    return filename_key(str(path.resolve()))


def resolve_destination(item: DownloadItemSnapshot, occupied: set[str]) -> DownloadDestination:
    """Keep a persisted name unless an actual conflict requires another one."""
    directory = item.directory
    try:
        directory.mkdir(parents=True, exist_ok=True)
        entries: dict[str, list[Path]] = {}
        for entry in directory.iterdir():
            entries.setdefault(filename_key(entry.name), []).append(entry)
        preferred = item.resolved_name or item.request.filename
        path = directory / preferred
        matches = entries.get(filename_key(preferred), [])
        if item.policy is not DownloadConflictPolicy.AUTO_NUMBER:
            if target_key(path) in occupied:
                return DownloadDestination(path, blocked=True)
            if len(matches) > 1:
                raise DownloadError("file_error", "下载目录包含多个仅大小写不同的同名文件。")
            if matches:
                if not matches[0].is_file():
                    raise DownloadError("file_error", "下载目标被同名目录或无效文件占用。")
                return DownloadDestination(matches[0], skipped=item.policy is DownloadConflictPolicy.SKIP)
            return DownloadDestination(path)
        if not matches and target_key(path) not in occupied:
            return DownloadDestination(path)
        base = Path(item.request.filename)
        for number in range(1, 100001):
            path = directory / f"{base.stem} ({number}){base.suffix}"
            if filename_key(path.name) not in entries and target_key(path) not in occupied:
                return DownloadDestination(path)
        raise DownloadError("file_error", "下载文件自动编号已达到上限。")
    except OSError as exc:
        raise DownloadError("file_error", f"无法准备下载目标：{exc}") from exc


def partial_path(item: DownloadItemSnapshot) -> Path:
    return item.directory / f".download-{item.item_id}-{item.generation}.part"


def discard_owned_partial(item: DownloadItemSnapshot) -> None:
    """Only unlink a basename explicitly owned by this persistent task."""
    if not item.partial_name:
        return
    prefix = f".download-{item.item_id}-"
    name = item.partial_name
    suffix = name.removeprefix(prefix).removesuffix(".part")
    if (not name.startswith(prefix) or not name.endswith(".part") or not suffix.isascii()
            or not suffix.isdecimal() or len(suffix) > 19 or str(int(suffix)) != suffix
            or not 1 <= int(suffix) <= item.generation):
        raise DownloadError("invalid_partial", "临时文件归属无法确认，保留原文件。")
    try:
        (item.directory / name).unlink(missing_ok=True)
    except OSError as exc:
        raise DownloadError("file_error", f"无法清理下载临时文件 {name}：{exc}") from exc


def commit_file(partial: Path, destination: Path, policy: DownloadConflictPolicy) -> None:
    for entry in destination.parent.iterdir():
        if filename_key(entry.name) == filename_key(destination.name):
            if policy is not DownloadConflictPolicy.OVERWRITE or entry.name != destination.name:
                raise FileExistsError(f"A case-insensitive download target appeared: {entry.name}")
    if policy is DownloadConflictPolicy.OVERWRITE:
        os.replace(partial, destination)
    else:
        # Linking within the destination directory atomically refuses a late
        # collision. Unsupported filesystems fail explicitly instead of clobbering.
        os.link(partial, destination)
        partial.unlink()
