"""Atomic result-file output for packaged application checks."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Mapping


class CheckOutputError(RuntimeError):
    """Base error raised when a check result cannot be published safely."""


class CheckOutputExistsError(CheckOutputError):
    """Raised when the requested result path already exists."""


def write_check_output(
    output_path: str | os.PathLike[str],
    payload: Mapping[str, object],
) -> Path:
    """Write one UTF-8 JSON result without replacing an existing path.

    The complete document is flushed and synchronized in a unique temporary
    file beside the target. A hard-link publication then creates the target
    atomically and fails if another process won the same name in the meantime.
    This gives Windows and Linux the same no-overwrite behavior.
    """

    target = Path(output_path)
    parent = target.parent
    if not parent.is_dir():
        raise CheckOutputError(
            f"Check output parent directory does not exist: {parent}"
        )
    if os.path.lexists(target):
        raise CheckOutputExistsError(
            f"Check output path already exists: {target}"
        )

    descriptor: int | None = None
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(
            descriptor,
            mode="w",
            encoding="utf-8",
            newline="\n",
        ) as stream:
            descriptor = None
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        try:
            os.link(temporary_path, target)
        except FileExistsError as exc:
            raise CheckOutputExistsError(
                f"Check output path already exists: {target}"
            ) from exc
        except OSError as exc:
            raise CheckOutputError(
                f"Unable to publish check output {target}: {exc}"
            ) from exc
    except CheckOutputError:
        raise
    except (OSError, TypeError, ValueError, RecursionError) as exc:
        raise CheckOutputError(
            f"Unable to write check output {target}: {exc}"
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    return target
