"""Exercise favorite values independently of storage and serialization."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _post(**changes):
    from danbooru_multi_search.danbooru.models import DanbooruPost

    post = DanbooruPost(
        id=42,
        preview_url="local-image:preview",
        large_file_url=None,
        file_url=None,
        score=-1,
        fav_count=0,
        rating="g",
        width=100,
        height=100,
        file_ext="jpg",
        general_tags=("cat",),
    )
    return replace(post, **changes)


def _probe_without_persistence():
    from importlib.abc import MetaPathFinder

    blocked = ("danbooru_multi_search.storage", "json", "sqlite3", "PySide6")

    class RejectPersistence(MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
                raise AssertionError(f"favorite values imported {fullname}")
            return None

    assert not any(name in sys.modules for name in blocked)
    sys.meta_path.insert(0, RejectPersistence())
    from danbooru_multi_search.core.favorites import (
        FavoriteCandidate,
        FavoriteError,
        FavoriteFilter,
        FavoriteFolderDraft,
        FavoriteItemDraft,
        FavoriteTagCategory,
        candidate_from_favorite_item,
        favorite_folder_key,
    )

    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    candidate = FavoriteCandidate.for_post(_post())
    assert candidate.post.preview_url == "local-image:preview"
    assert FavoriteCandidate.for_tag("CAT", FavoriteTagCategory.GENERAL).identity.object_key == "cat"
    assert FavoriteCandidate.for_pool(3, "Pool").identity.object_key == "3"
    assert FavoriteFolderDraft(" Folder ", now, now).name == " Folder "
    assert favorite_folder_key(" Folder ") == "folder"
    assert FavoriteItemDraft(candidate, None, now, now).candidate is candidate
    assert FavoriteFilter(exact_tag="cat").exact_tag == "cat"
    assert callable(candidate_from_favorite_item)
    for invalid in (True, "42", 2**63):
        try:
            FavoriteCandidate.for_pool(invalid, "Pool")
        except FavoriteError as exc:
            assert exc.code == "invalid_identifier"
        else:
            raise AssertionError("invalid identifier accepted")
    assert not any(
        name == prefix or name.startswith(prefix + ".")
        for name in sys.modules for prefix in blocked
    )


def test_favorite_values_work_when_persistence_and_qt_imports_are_blocked():
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "probe"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_original_favorite_imports_keep_the_same_objects():
    from danbooru_multi_search.core import favorites as models
    from danbooru_multi_search import storage
    from danbooru_multi_search.storage import favorite_codec, favorites

    model_names = (
        "FavoriteError", "FavoriteObjectType", "FavoriteTagCategory",
        "FavoritePoolCategory", "FavoriteFolderScope", "FavoriteSortField",
        "FavoriteSortDirection", "FolderDeleteMode", "FavoriteIdentity",
        "FavoriteCandidate", "FavoriteFolder", "FavoriteItem", "FavoriteFolderDraft",
        "FavoriteItemDraft", "FavoriteCollectionSnapshot", "FavoriteFilter",
        "FavoriteSort", "favorite_folder_key", "candidate_from_favorite_item",
    )
    codec_names = (
        "FAVORITE_SUMMARY_VERSION", "encode_favorite_summary",
        "decode_favorite_summary", "encode_favorite_timestamp",
        "decode_favorite_timestamp",
    )
    for owner, names in ((models, model_names), (favorite_codec, codec_names)):
        for name in names:
            assert getattr(favorites, name) is getattr(owner, name)
            if name in storage.__all__:
                assert getattr(storage, name) is getattr(owner, name)


def test_post_dimensions_keep_their_existing_range_and_media_text():
    from danbooru_multi_search.core.favorites import FavoriteCandidate

    candidate = FavoriteCandidate.for_post(
        _post(width=10**60, height=10**60, file_size=10**60)
    )
    assert candidate.post.width == 10**60
    assert candidate.post.file_size == 10**60
    assert candidate.post.preview_url == "local-image:preview"


def test_post_value_errors_map_to_the_existing_favorite_error_code():
    import pytest
    from danbooru_multi_search.core.favorites import FavoriteCandidate, FavoriteError

    for post in (
        _post(width=True),
        _post(preview_url=None),
        _post(artist_tags=("cat",)),
        _post(created_at="2026-11-10T00:00:00Z"),
        _post(score=2**63),
        _post(fav_count=2**63),
    ):
        with pytest.raises(FavoriteError) as error:
            FavoriteCandidate.for_post(post)
        assert error.value.code == "invalid_summary"


def test_post_value_validation_keeps_python_integer_text_limit():
    import pytest
    from danbooru_multi_search.core.favorites import FavoriteCandidate, FavoriteError
    from danbooru_multi_search.danbooru.post_validation import (
        PostValidationError,
        validate_post_values,
    )

    limit = sys.get_int_max_str_digits()
    if limit == 0:
        pytest.skip("the interpreter has disabled its integer text limit")
    for field in ("width", "height", "file_size"):
        post = _post(**{field: 10**limit})
        with pytest.raises(PostValidationError):
            validate_post_values(post)
        with pytest.raises(FavoriteError) as error:
            FavoriteCandidate.for_post(post)
        assert error.value.code == "invalid_summary"


if __name__ == "__main__":
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    _probe_without_persistence()
