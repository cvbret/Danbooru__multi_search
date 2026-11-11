from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import danbooru_multi_search.danbooru.details as details_module

from danbooru_multi_search.danbooru import (
    DanbooruDetailController,
    DanbooruPost,
    DanbooruPostListModel,
    DetailMediaState,
    MediaQuality,
    TagIndexSnapshot,
    TagSuggestion,
    TagTranslationStore,
    build_media_candidates,
    build_post_page_url,
    format_file_size_bytes,
    format_file_size_readable,
    format_local_created_at,
)


def _post(post_id: int = 10, **changes) -> DanbooruPost:
    values = {
        "id": post_id,
        "preview_url": "https://cdn.invalid/preview.jpg",
        "large_file_url": "https://cdn.invalid/large.jpg",
        "file_url": "https://cdn.invalid/original.jpg",
        "score": -2,
        "fav_count": 0,
        "rating": "g",
        "width": 1600,
        "height": 1200,
        "file_ext": "jpg",
        "created_at": "2024-01-02T03:04:05.123456Z",
        "file_size": 1_572_864,
        "source": " file://fictional/source ",
        "artist_tags": ("sample_artist",),
        "character_tags": ("sample_character",),
        "copyright_tags": ("sample_series",),
        "general_tags": ("blue_sky",),
        "meta_tags": ("highres",),
    }
    values.update(changes)
    return DanbooruPost(**values)


def _controller(*posts: DanbooruPost, store: TagTranslationStore | None = None):
    model = DanbooruPostListModel()
    model.set_posts(tuple(posts))
    translation_store = store or TagTranslationStore()
    controller = DanbooruDetailController(
        "https://example.invalid/deploy?ignored=1#fragment",
        translation_store,
        local_timezone=timezone(timedelta(hours=8)),
    )
    return controller, model, translation_store


@pytest.mark.parametrize(
    ("value", "exact", "readable"),
    [
        (None, "暂无数据", "暂无数据"),
        (0, "0 字节", "0 B"),
        (1023, "1023 字节", "1023 B"),
        (1024, "1024 字节", "1.00 KiB"),
        (1536, "1536 字节", "1.50 KiB"),
        (1024**2, f"{1024**2} 字节", "1.00 MiB"),
        (1024**3, f"{1024**3} 字节", "1.00 GiB"),
        (5 * 1024**3 + 6 * 1024**2, f"{5 * 1024**3 + 6 * 1024**2} 字节", "5.01 GiB"),
        (
            10**36,
            f"{10**36} 字节",
            f"{10**36 // 1024**3}.00 GiB",
        ),
        (
            10**100,
            f"{10**100} 字节",
            f"{10**100 // 1024**3}.00 GiB",
        ),
    ],
)
def test_file_size_formatting_uses_fixed_binary_thresholds(value, exact, readable):
    assert format_file_size_bytes(value) == exact
    assert format_file_size_readable(value) == readable


def test_created_time_and_post_page_url_have_fixed_formats():
    assert format_local_created_at(
        "2024-01-02T03:04:05.123456Z",
        local_timezone=timezone(timedelta(hours=8)),
    ) == "2024-01-02 11:04:05 +08:00"
    assert format_local_created_at(None) == "暂无数据"
    assert build_post_page_url(
        "https://example.invalid/deploy/?query=1#fragment",
        42,
    ) == "https://example.invalid/deploy/posts/42"
    assert build_post_page_url(
        "https://example.invalid/a%2Fb/?q=1#f",
        5,
    ) == "https://example.invalid/a%2Fb/posts/5"


@pytest.mark.parametrize(
    ("value", "local_timezone", "expected"),
    [
        (
            "0001-01-01T00:00:00.000000Z",
            timezone.utc,
            "0001-01-01 00:00:00 +00:00",
        ),
        (
            "0001-01-01T00:00:00.000000Z",
            timezone(-timedelta(hours=1)),
            "0001-01-01 00:00:00 +00:00",
        ),
        (
            "9999-12-31T23:59:59.999999Z",
            timezone.utc,
            "9999-12-31 23:59:59 +00:00",
        ),
        (
            "9999-12-31T23:59:59.999999Z",
            timezone(timedelta(hours=1)),
            "9999-12-31 23:59:59 +00:00",
        ),
    ],
)
def test_created_time_boundaries_fall_back_to_explicit_utc(
    value,
    local_timezone,
    expected,
):
    assert format_local_created_at(
        value,
        local_timezone=local_timezone,
    ) == expected


@pytest.mark.parametrize("failure", [OverflowError, OSError])
def test_created_time_system_timezone_failure_falls_back_to_utc(
    monkeypatch,
    failure,
):
    class FailingSystemDateTime(datetime):
        def astimezone(self, tz=None):
            assert tz is None
            raise failure("system timezone conversion failed")

    monkeypatch.setattr(details_module, "datetime", FailingSystemDateTime)

    assert format_local_created_at(
        "9999-12-31T23:59:59.999999Z"
    ) == "9999-12-31 23:59:59 +00:00"


@pytest.mark.parametrize(
    ("quality", "expected"),
    [
        ("preview", ["preview", "large", "original"]),
        ("large", ["large", "original", "preview"]),
        ("original", ["original", "large", "preview"]),
    ],
)
def test_media_candidate_order_is_fixed(quality, expected):
    candidates = build_media_candidates(_post(), quality)

    assert [candidate.quality.value for candidate in candidates] == expected
    assert [candidate.source_field for candidate in candidates] == [
        f"{value}_file_url" if value != "original" else "file_url"
        for value in expected
    ]


def test_media_candidates_deduplicate_and_preserve_first_field_identity():
    same_url = "https://cdn.invalid/original.jpg"
    post = _post(large_file_url=same_url, file_url=same_url)

    large = build_media_candidates(post, MediaQuality.LARGE)
    original = build_media_candidates(post, MediaQuality.ORIGINAL)

    assert [candidate.quality.value for candidate in large] == ["large", "preview"]
    assert large[0].same_as_original is True
    assert [candidate.quality.value for candidate in original] == [
        "original",
        "preview",
    ]
    assert original[0].same_as_original is False


def test_gif_and_restricted_formats_choose_safe_components():
    gif = build_media_candidates(_post(file_ext="gif"), "large")
    webm = build_media_candidates(_post(file_ext="webm"), "original")
    unknown = build_media_candidates(_post(file_ext="mystery"), "large")

    assert [candidate.component for candidate in gif] == [
        "animated",
        "animated",
        "image",
    ]
    assert [(item.quality.value, item.component) for item in webm] == [
        ("preview", "image")
    ]
    assert [(item.quality.value, item.component) for item in unknown] == [
        ("preview", "image")
    ]


def test_detail_holds_original_post_identity_and_invalid_posts_do_nothing():
    post = _post()
    controller, model, _store = _controller(post)

    assert controller.open_post(model.post_at(0)) is True
    assert controller.current_post is model.posts[0]
    assert controller.postIdText == "10"
    assert controller.scoreText == "-2"
    assert controller.favoriteText == "0"
    assert controller.createdAtText == "2024-01-02 11:04:05 +08:00"
    assert controller.sourceText == " file://fictional/source "
    assert controller.requestedQuality == "large"
    assert controller.mediaUrl.endswith("large.jpg")

    generation = controller.mediaGeneration
    assert controller.open_post(None) is False
    assert controller.open_post(object()) is False
    assert controller.current_post is post
    assert controller.mediaGeneration == generation


@pytest.mark.parametrize(
    ("score", "favorites", "score_text", "favorite_text"),
    [
        (None, None, "暂无数据", "暂无数据"),
        (0, 0, "0", "0"),
        (-7, 0, "-7", "0"),
    ],
)
def test_detail_preserves_missing_zero_and_negative_popularity(
    score,
    favorites,
    score_text,
    favorite_text,
):
    controller, model, _store = _controller(
        _post(score=score, fav_count=favorites)
    )

    assert controller.open_post(model.post_at(0))
    assert controller.scoreText == score_text
    assert controller.favoriteText == favorite_text


def test_gallery_model_revision_and_identity_accessors_are_read_only():
    first = _post(10)
    second = _post(20)
    model = DanbooruPostListModel()

    assert model.revision == 0
    assert model.post_at(0) is None
    model.set_posts((first, second))
    assert model.revision == 1
    assert model.post_at(0) is first
    assert model.row_for_post_id(20) == 1
    assert model.rowForPostId(999) == -1
    model.set_posts((first, second))
    assert model.revision == 2


def test_translation_snapshot_refreshes_all_names_without_replacing_post():
    first_snapshot = TagIndexSnapshot.build(
        (TagSuggestion("sample_artist", ("示例画师", "别名"), 1, 1),)
    )
    store = TagTranslationStore(first_snapshot)
    post = _post()
    controller, model, _store = _controller(post, store=store)
    controller.open_post(model.post_at(0))

    groups = controller.tagsModel.groups
    assert len(groups) == 5
    assert [group.label for group in groups] == [
        "画师",
        "角色",
        "作品系列",
        "普通标签",
        "元数据",
    ]
    assert groups[0].entries[0].chinese_text == "示例画师、别名"
    original_post = controller.current_post

    store.replace_snapshot(
        TagIndexSnapshot.build(
            (TagSuggestion("sample_artist", ("新名称",), 2, 1),)
        )
    )

    assert controller.tagsModel.groups[0].entries[0].chinese_text == "新名称"
    assert controller.current_post is original_post
    assert controller.requestedQuality == "large"


def test_media_fallback_requires_generation_attempt_and_exact_url():
    controller, model, _store = _controller(_post())
    controller.open_post(model.post_at(0))
    generation = controller.mediaGeneration
    first_url = controller.mediaUrl

    assert controller.reportMediaFailure(generation + 1, 1, first_url) is False
    assert controller.reportMediaFailure(generation, 2, first_url) is False
    assert controller.reportMediaFailure(generation, 1, first_url + "?late") is False
    assert controller.reportMediaFailure(generation, 1, first_url) is True
    assert controller.mediaState == DetailMediaState.FALLBACK.value
    assert controller.candidateAttempt == 2
    assert controller.mediaUrl.endswith("original.jpg")
    assert controller.actualQualityText == "暂无数据"
    assert controller.reportMediaSuccess(generation, 1, first_url) is False
    assert controller.reportMediaSuccess(
        generation,
        2,
        controller.mediaUrl,
    ) is True
    assert controller.mediaState == DetailMediaState.SUCCESS.value
    assert controller.actualQualityText == "原图"


def test_media_attempt_has_one_terminal_result():
    controller, model, _store = _controller(_post())
    controller.open_post(model.post_at(0))
    generation = controller.mediaGeneration
    attempt = controller.candidateAttempt
    url = controller.mediaUrl

    assert controller.reportMediaSuccess(generation, attempt, url) is True
    assert controller.reportMediaFailure(generation, attempt, url) is False
    assert controller.mediaState == DetailMediaState.SUCCESS.value

    controller.requestQuality("preview")
    while controller.mediaState != DetailMediaState.ERROR.value:
        last_generation = controller.mediaGeneration
        last_attempt = controller.candidateAttempt
        last_url = controller.mediaUrl
        assert controller.reportMediaFailure(
            last_generation,
            last_attempt,
            last_url,
        )
    assert controller.reportMediaSuccess(
        last_generation,
        last_attempt,
        last_url,
    ) is False
    assert controller.mediaState == DetailMediaState.ERROR.value


def test_cache_clear_stops_current_candidate_without_starting_fallback():
    controller, model, _store = _controller(_post())
    controller.open_post(model.post_at(0))
    generation = controller.mediaGeneration
    attempt = controller.candidateAttempt
    url = controller.mediaUrl

    assert controller.reportMediaStopped(generation, attempt, url) is True
    assert controller.mediaState == DetailMediaState.ERROR.value
    assert controller.canRetryMedia is True
    assert controller.candidateAttempt == attempt
    assert controller.mediaUrl == url
    assert "缓存已清理" in controller.mediaMessage
    assert controller.reportMediaFailure(generation, attempt, url) is False


def test_quality_change_same_quality_retry_and_close_generation_rules():
    controller, model, _store = _controller(_post())
    controller.open_post(model.post_at(0))
    first_generation = controller.mediaGeneration

    assert controller.requestQuality("large") is False
    assert controller.mediaGeneration == first_generation
    assert controller.requestQuality("preview") is True
    assert controller.mediaGeneration == first_generation + 1
    assert controller.mediaUrl.endswith("preview.jpg")

    while controller.mediaState != DetailMediaState.ERROR.value:
        assert controller.reportMediaFailure(
            controller.mediaGeneration,
            controller.candidateAttempt,
            controller.mediaUrl,
        )
    failed_generation = controller.mediaGeneration
    assert controller.retryMedia() is True
    assert controller.mediaGeneration == failed_generation + 1
    retry_url = controller.mediaUrl
    assert controller.closeDetail() is True
    assert controller.mediaGeneration == failed_generation + 2
    assert controller.mediaUrl == ""
    assert controller.reportMediaSuccess(
        failed_generation + 1,
        1,
        retry_url,
    ) is False


def test_restricted_media_never_enters_original_fallback_chain():
    controller, model, _store = _controller(_post(file_ext="webm"))
    controller.open_post(model.post_at(0))

    assert controller.restrictedFormat is True
    assert controller.requestedQuality == "large"
    assert controller.mediaUrl.endswith("preview.jpg")
    assert controller.reportMediaFailure(
        controller.mediaGeneration,
        controller.candidateAttempt,
        controller.mediaUrl,
    )
    assert controller.mediaState == DetailMediaState.ERROR.value
    assert controller.canRetryMedia is True


@pytest.mark.parametrize("extension", ["webm", "mp4", "zip", "swf", "mystery"])
def test_every_restricted_or_unknown_extension_uses_preview_only(extension):
    controller, model, _store = _controller(_post(file_ext=extension))

    assert controller.open_post(model.post_at(0))
    assert controller.restrictedFormat is True
    assert controller.formatState == (
        "unknown" if extension == "mystery" else "restricted"
    )
    assert controller.formatStatusText.startswith(
        "未知格式" if extension == "mystery" else "受限格式"
    )
    assert controller.mediaUrl.endswith("preview.jpg")
    assert controller.requestQuality("original") is True
    assert controller.mediaUrl.endswith("preview.jpg")


def test_restricted_format_without_preview_has_no_media_candidate():
    controller, model, _store = _controller(
        _post(file_ext="mp4", preview_url=None)
    )

    assert controller.open_post(model.post_at(0))
    assert controller.mediaState == DetailMediaState.ERROR.value
    assert controller.mediaUrl == ""
    assert "没有可用预览图" in controller.mediaMessage


@pytest.mark.parametrize("action", ["tag", "shutdown"])
def test_tag_search_and_shutdown_reject_late_media_results(action):
    controller, model, _store = _controller(_post())
    assert controller.open_post(model.post_at(0))
    generation = controller.mediaGeneration
    attempt = controller.candidateAttempt
    url = controller.mediaUrl

    if action == "tag":
        assert controller.activateTag("sample_artist") is True
    else:
        controller.shutdown()

    assert controller.mediaGeneration == generation + 1
    assert controller.mediaUrl == ""
    assert controller.reportMediaSuccess(generation, attempt, url) is False
    assert controller.reportMediaFailure(generation, attempt, url) is False
    assert controller.mediaState == DetailMediaState.IDLE.value


@pytest.mark.parametrize(
    "signal_name",
    ["mediaChanged", "openChanged", "detailChanged"],
)
def test_open_post_stops_after_synchronous_close_from_any_signal(signal_name):
    controller, model, _store = _controller(_post())
    getattr(controller, signal_name).connect(controller.closeDetail)

    assert controller.open_post(model.post_at(0)) is False
    assert controller.isOpen is False
    assert controller.mediaUrl == ""
    assert controller.mediaState == DetailMediaState.IDLE.value


def test_open_post_stops_after_synchronous_shutdown_without_rebuilding_media():
    controller, model, _store = _controller(_post())
    controller.mediaChanged.connect(controller.shutdown)

    assert controller.open_post(model.post_at(0)) is False
    assert controller.isOpen is False
    assert controller.mediaUrl == ""
    assert controller.mediaState == DetailMediaState.IDLE.value


def test_open_post_stops_when_tag_model_reset_synchronously_shuts_down():
    controller, model, _store = _controller(_post())
    controller.tagsModel.modelReset.connect(controller.shutdown)

    assert controller.open_post(model.post_at(0)) is False
    assert controller.isOpen is False
    assert controller.mediaUrl == ""


def test_tag_activation_stops_after_synchronous_shutdown():
    controller, model, _store = _controller(_post())
    requested: list[str] = []
    controller.tagSearchRequested.connect(requested.append)
    assert controller.open_post(model.post_at(0))
    controller.mediaChanged.connect(controller.shutdown)

    assert controller.activateTag("sample_artist") is False
    assert requested == []
    assert controller.mediaUrl == ""
