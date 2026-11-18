"""Private staged media for one Eagle request and its subsequent recovery."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from uuid import uuid4

from danbooru_multi_search.danbooru.download import download_destination
from danbooru_multi_search.danbooru.models import DanbooruPost


def _file_stamp(path: Path) -> str:
    stat = path.stat()
    return f"{path.name} {stat.st_dev} {stat.st_ino} {stat.st_size} {stat.st_mtime_ns}"


@dataclass(slots=True)
class EagleMediaFile:
    path: Path
    retry_path: Path
    recovered: bool
    _submitted_stamp: str = ""

    @classmethod
    def prepare(cls, root: Path, post: DanbooruPost) -> "EagleMediaFile":
        """Claim a previous download atomically, or prepare a private destination."""
        source_key = sha256(post.download_url.encode("utf-8")).hexdigest()
        source_dir = root / source_key
        retry_path = download_destination(post, source_dir)
        work_dir = source_dir / uuid4().hex
        work_dir.mkdir(parents=True)
        path = work_dir / retry_path.name
        try:
            retry_path.replace(path)
        except FileNotFoundError:
            return cls(path, retry_path, False)
        except OSError:
            work_dir.rmdir()
            raise
        return cls(path, retry_path, True)

    def capture_for_import(self) -> None:
        self._submitted_stamp = _file_stamp(self.path)

    def finish(self, *, retain: bool) -> Path | None:
        """Return the retry path only if this request retained its own file."""
        saved = None
        if self._submitted_stamp and self.path.is_file() and _file_stamp(self.path) != self._submitted_stamp:
            raise OSError("暂存文件在提交后发生变化，已保留原路径。")
        if retain and self.path.is_file():
            saved = self.path.replace(self.retry_path)
        else:
            self.path.unlink(missing_ok=True)
        self.path.parent.rmdir()
        # Other sessions may own files under the same source URL.
        try:
            self.retry_path.parent.rmdir()
        except OSError:
            pass
        return saved

    def mark_stopped(self) -> None:
        """Keep the submitted path intact while its Eagle result is unknown."""
        if not self._submitted_stamp or _file_stamp(self.path) != self._submitted_stamp:
            return
        with (self.path.parent / ".stopped").open("x", encoding="utf-8") as marker:
            marker.write(self._submitted_stamp)

    @staticmethod
    def release_recovered(root: Path, path: Path) -> None:
        """Release an old file only after the worker stopped and recovery confirmed it."""
        resolved_root = root.resolve()
        try:
            relative = path.relative_to(root)
        except ValueError:
            if not path.is_relative_to(resolved_root):
                return
            relative = path.relative_to(resolved_root)
        if (len(relative.parts) != 3 or not re.fullmatch("[0-9a-f]{64}", relative.parts[0])
                or not re.fullmatch("[0-9a-f]{32}", relative.parts[1])
                or not re.fullmatch(r"[0-9]+\.[a-z0-9]{1,10}", relative.name)):
            return
        marker = path.parent / ".stopped"
        try:
            if path.resolve(strict=True) != resolved_root / relative or marker.is_symlink():
                return
            with marker.open("rb") as stream:
                if stream.read(512) != _file_stamp(path).encode("utf-8"):
                    return
            path.unlink()
            marker.unlink()
            path.parent.rmdir()
        except FileNotFoundError:
            # A live request has no marker, and a second recovery may finish first.
            return
        except RuntimeError:
            # Python 3.11 may report a symlink loop while resolving a stored path.
            return
        try:
            path.parent.parent.rmdir()
        except OSError:
            pass
