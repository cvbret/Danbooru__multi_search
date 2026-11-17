import json

import pytest

from danbooru_multi_search.core.pool_pages import PoolPageState
from danbooru_multi_search.danbooru.parsing import DanbooruParseError, parse_complete_posts_response
from danbooru_multi_search.danbooru.pool_post_parsing import parse_pool_posts_response
from danbooru_multi_search.danbooru.pool_parsing import PoolParseError
from tests.test_danbooru_parsing import _post_mapping


def raw_post(identifier=1, **changes):
    raw = _post_mapping()
    raw.update(id=identifier, is_deleted=False,
               tag_string_artist="artist", tag_string_character="character",
               tag_string_copyright="series", tag_string_general="cat", tag_string_meta="highres")
    raw.update(changes)
    return raw


def test_batch_classifies_individual_rows_and_keeps_requested_id_order():
    raw = [raw_post(5, image_width=0), raw_post(3, is_deleted=True), raw_post(1),
           raw_post(2, preview_file_url=None, large_file_url=None, file_url=None)]
    values = parse_pool_posts_response(json.dumps(raw), (1, 2, 3, 4, 5))
    assert [value.state for value in values] == [
        PoolPageState.AVAILABLE, PoolPageState.UNAVAILABLE, PoolPageState.DELETED,
        PoolPageState.NOT_RETURNED, PoolPageState.FAILED,
    ]
    assert values[0].post == parse_complete_posts_response(json.dumps([raw_post(1)]).encode())[0]
    assert "未返回" in values[3].message


@pytest.mark.parametrize("rows", [
    [raw_post(2)], [raw_post(1), raw_post(1)], [{}], [None], [{"id": True}],
])
def test_ambiguous_or_unrequested_id_rejects_batch(rows):
    with pytest.raises(PoolParseError):
        parse_pool_posts_response(json.dumps(rows), (1,))


@pytest.mark.parametrize("changes", [
    {"is_deleted": "false"}, {"preview_file_url": 1},
    {"tag_string_artist": None}, {"created_at": "yesterday"},
])
def test_known_bad_row_fails_only_that_post(changes):
    values = parse_pool_posts_response(json.dumps([raw_post(1, **changes), raw_post(2)]), (1, 2))
    assert values[0].state is PoolPageState.FAILED
    assert values[1].state is PoolPageState.AVAILABLE


def test_old_parser_remains_strict_for_unavailable_posts():
    raw = raw_post(1, preview_file_url=None, large_file_url=None, file_url=None)
    with pytest.raises(DanbooruParseError):
        parse_complete_posts_response(json.dumps([raw]).encode())
