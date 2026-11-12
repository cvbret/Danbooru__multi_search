from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtGui import QGuiApplication

from danbooru_multi_search.core.favorites import FavoriteCandidate, FavoriteError
from danbooru_multi_search.danbooru.models import DanbooruPost
from danbooru_multi_search.storage.database import Database
from danbooru_multi_search.storage.favorites import FavoriteRepository
from danbooru_multi_search.ui.favorites import FavoriteController, post_identity


APPLICATION = QGuiApplication.instance() or QGuiApplication([])
IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def post(identifier=1, *, url=IMAGE):
    return DanbooruPost(
        id=identifier, preview_url=url, large_file_url=url, file_url=url,
        score=-2, fav_count=17, rating="g", width=100, height=100, file_ext="png",
        artist_tags=("sample_artist",), character_tags=("sample_character",),
        copyright_tags=("sample_series",), general_tags=("cat",), meta_tags=("highres",),
    )


def build(tmp_path):
    database = Database(tmp_path / "favorites.sqlite3")
    database.initialize()
    repository = FavoriteRepository(database)
    return repository, FavoriteController(repository)


def test_shared_state_is_published_after_commit_and_cancel_is_idempotent(tmp_path):
    repository, controller = build(tmp_path)
    work = post()
    identity = post_identity("1")
    controller.ensure((identity,))
    seen = []
    controller.stateChanged.connect(lambda: seen.append(repository.get_by_identity(identity)))
    assert controller.set_post(work, True, guard=lambda: True)
    first = repository.get_by_identity(identity)
    assert controller.state(identity) == {"known": True, "saved": True}
    assert seen[-1] == first
    assert controller.set_post(work, True, guard=lambda: True)
    assert repository.get_by_identity(identity) == first
    assert repository.capture_collection().revision == 1
    assert work.fav_count == 17
    assert controller.set_post(work, False, guard=lambda: True)
    assert controller.set_post(work, False, guard=lambda: True)
    assert controller.state(identity) == {"known": True, "saved": False}
    assert repository.capture_collection().revision == 2


def test_read_and_write_failures_keep_confirmed_facts(tmp_path, monkeypatch):
    repository, controller = build(tmp_path)
    work = post()
    identity = post_identity("1")
    original_read = repository.get_by_identity

    def fail(*args, **kwargs):
        raise FavoriteError("injected", "injected failure")

    monkeypatch.setattr(repository, "get_by_identity", fail)
    controller.ensure((identity,))
    assert controller.state(identity) == {"known": False, "saved": False}
    assert "读取失败" in controller.noticeText
    monkeypatch.setattr(repository, "get_by_identity", original_read)
    controller.retryStates()
    assert controller.state(identity) == {"known": True, "saved": False}
    original_add = repository.add_favorites
    monkeypatch.setattr(repository, "add_favorites", fail)
    assert not controller.set_post(work, True, guard=lambda: True)
    assert controller.state(identity) == {"known": True, "saved": False}
    monkeypatch.setattr(repository, "add_favorites", original_add)
    assert controller.set_post(work, True, guard=lambda: True)
    monkeypatch.setattr(repository, "delete_by_identity", fail)
    assert not controller.set_post(work, False, guard=lambda: True)
    assert controller.state(identity) == {"known": True, "saved": True}
    assert repository.get_by_identity(identity) is not None


def test_cancel_only_needs_identity_when_current_post_has_no_media(tmp_path):
    from dataclasses import replace

    repository, controller = build(tmp_path)
    work = post()
    assert controller.set_post(work, True, guard=lambda: True)
    restricted = replace(work, preview_url=None, large_file_url=None, file_url=None)
    assert not controller.set_post(restricted, True, guard=lambda: True)
    assert controller.state(post_identity("1"))["saved"]
    assert controller.set_post(restricted, False, guard=lambda: True)
    assert repository.get_by_identity(post_identity("1")) is None


@pytest.mark.parametrize("action", ["invalidate", "close", "reenter"])
def test_busy_callback_is_checked_before_any_transaction(tmp_path, action):
    repository, controller = build(tmp_path)
    valid = True
    nested = []

    def callback():
        nonlocal valid
        if not controller.busy:
            return
        if action == "close":
            controller.shutdown()
        elif action == "invalidate":
            valid = False
        else:
            nested.append(controller.set_post(post(2), True, guard=lambda: True))

    controller.busyChanged.connect(callback)
    result = controller.set_post(post(), True, guard=lambda: valid)
    assert result is (action == "reenter")
    assert repository.capture_collection().revision == (1 if result else 0)
    assert repository.get_by_identity(post_identity("2")) is None
    if action == "reenter":
        assert nested == [False]
