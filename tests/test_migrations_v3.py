"""Schema-v3 history conversion stays fixed as current query rules evolve."""

from __future__ import annotations

import json

import pytest

from danbooru_multi_search.storage import database as database_module
from danbooru_multi_search.storage.database import Database, DatabaseError
from danbooru_multi_search.storage.migrations_v3 import version_three_definition_json


CAT_DOG_DEFINITION = (
    '{"filter":{"blocked_tags":[],"file_categories":null,"maximum_age":null,'
    '"maximum_aspect_ratio":null,"minimum_age":null,"minimum_aspect_ratio":null,'
    '"minimum_fav_count":null,"minimum_height":null,"minimum_score":null,'
    '"minimum_width":null,"orientations":null,"published_from_utc":null,'
    '"published_until_utc":null},"limits":{"active_time_limit_seconds":60.0,'
    '"branch_page_limit":10,"candidate_limit":1000,"page_size":40},'
    '"mode":"standard","normalized_expression":"cat AND dog",'
    '"rating":"general","sort":{"direction":"original","field":"source_order"},'
    '"version":1}'
)


def _encoded(tags):
    return json.dumps(tags, ensure_ascii=True, separators=(",", ":"))


def _version_two_database(tmp_path, monkeypatch, encoded_tags):
    migrations = database_module.MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(database_module, "MIGRATIONS", migrations[:2])
        database = Database(tmp_path / "history.sqlite3")
        assert database.initialize() == 2
    with database.connect() as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO recent_queries(
                    id, display_text, normalized_tags, rating, page_size,
                    last_submitted_page, last_used_at
                ) VALUES (17, 'preserve this', ?, 'general', 40, 3, 'original time')
                """,
                (encoded_tags,),
            )
            connection.execute(
                "UPDATE browser_session SET active_query_id = 17 WHERE id = 1"
            )
    return database


def test_version_three_definition_uses_frozen_literal_json():
    assert version_three_definition_json('["cat","dog"]', "general", 40) == (
        CAT_DOG_DEFINITION
    )


def test_current_query_changes_do_not_change_historical_migration(
    tmp_path, monkeypatch
):
    from danbooru_multi_search.danbooru import (
        query_definition,
        query_language,
        query_types,
        result_processing,
        tag_syntax,
    )
    from danbooru_multi_search.storage import query_serialization

    database = _version_two_database(tmp_path, monkeypatch, '["cat","dog"]')

    def changed_current_rule(*args, **kwargs):
        raise AssertionError("Current query rules must not run during old migration")

    with monkeypatch.context() as patch:
        for module, name in (
            (query_definition, "GalleryQueryDefinition"),
            (query_language, "parse_query_expression"),
            (query_types, "QueryExecutionLimits"),
            (result_processing, "ResultFilterSpec"),
            (result_processing, "ResultSortSpec"),
            (tag_syntax, "normalize_tag_name"),
            (query_serialization, "encode_query_definition"),
            (query_serialization, "decode_normalized_tags"),
        ):
            patch.setattr(module, name, changed_current_rule)
        patch.setattr(query_serialization, "QUERY_DEFINITION_VERSION", 900)
        patch.setattr(query_types, "DEFAULT_CANDIDATE_LIMIT", 3)
        patch.setattr(tag_syntax, "METATAG_NAMES", frozenset({"cat", "dog"}))
        assert database.initialize() == 8
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recent_queries WHERE id = 17"
            ).fetchone()
            active = connection.execute(
                "SELECT active_query_id FROM browser_session WHERE id = 1"
            ).fetchone()["active_query_id"]
        assert row["definition_json"] == row["query_key"] == CAT_DOG_DEFINITION
        assert row["definition_version"] == 1
        assert row["display_text"] == "preserve this"
        assert row["last_submitted_page"] == 3
        assert row["last_used_at"] == "original time"
        assert active == 17


@pytest.mark.parametrize(
    "tag",
    (
        "fate/grand_order",
        "girls'_frontline",
        "honkai:_star_rail",
        "love_live!",
        "fate_(series)",
        "foo_(bar)_and_(baz)",
        'quote"and\\slash',
    ),
)
def test_version_two_tag_punctuation_is_preserved(tag):
    payload = json.loads(version_three_definition_json(_encoded([tag]), "all", 200))
    assert payload["normalized_expression"] == tag
    assert payload["limits"]["page_size"] == 200


@pytest.mark.parametrize(
    "encoded_tags",
    (
        "[]",
        "{}",
        '[true]',
        '["dog","cat"]',
        '["cat","cat"]',
        '[ "cat" ]',
        '["Cat"]',
        '[" cat"]',
        '["cat dog"]',
        '["猫"]',
        '["-cat"]',
        '["~cat"]',
        '["cat*"]',
        '["and"]',
        '["rating:g"]',
        '["series_pool_count:1"]',
        '["commentary:true"]',
        '["(cat)"]',
        '["cat_()"]',
        '["cat_((dog))"]',
        '["cat_(dog"]',
        '["cat)"]',
        '["cat\\u0000"]',
        '["\\u0063at"]',
        '[NaN]',
        '["cat",]',
    ),
)
def test_invalid_version_two_tags_roll_back_old_schema(
    tmp_path, monkeypatch, encoded_tags
):
    database = _version_two_database(tmp_path, monkeypatch, encoded_tags)
    with pytest.raises(DatabaseError, match="Unable to initialize"):
        database.initialize()
    assert database.current_version() == 2
    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM recent_queries WHERE id = 17"
        ).fetchone()
        active = connection.execute(
            "SELECT active_query_id FROM browser_session WHERE id = 1"
        ).fetchone()["active_query_id"]
        temporary = connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%_v3'"
        ).fetchall()
    assert "query_key" not in row.keys()
    assert row["normalized_tags"] == encoded_tags
    assert active == 17
    assert temporary == []


def test_version_three_expression_length_boundary_stays_2048():
    accepted = "a" * 2048
    result = json.loads(
        version_three_definition_json(_encoded([accepted]), "general", 40)
    )
    assert result["normalized_expression"] == accepted
    with pytest.raises(ValueError, match="2048"):
        version_three_definition_json(_encoded([accepted + "a"]), "general", 40)


def test_version_three_expression_node_boundary_includes_and_group():
    tags = [f"tag_{index:03}" for index in range(127)]
    result = json.loads(version_three_definition_json(_encoded(tags), "general", 40))
    assert result["normalized_expression"] == " AND ".join(tags)
    with pytest.raises(ValueError, match="128 nodes"):
        version_three_definition_json(_encoded(tags + ["tag_127"]), "general", 40)
