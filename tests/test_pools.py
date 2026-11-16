from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import subprocess
import sys

import pytest

from danbooru_multi_search.core.pools import (
    DanbooruPool, PoolCategory, PoolError, PoolMembershipPage, PoolPageReference,
    PoolRequestIdentity, PoolRequestKind, PoolRequestOutcome, PoolRequestState,
)


def test_pool_pages_keep_missing_and_repeated_post_positions():
    pool = DanbooruPool(123, "原始 名称", PoolCategory.SERIES, (30, 10, 30, 20))
    assert pool.pages == tuple(
        PoolPageReference(123, page, identifier)
        for page, identifier in enumerate((30, 10, 30, 20), 1)
    )
    # A future loader knowing only posts 30/20 must still retain page 2 for 10.
    known = {30: "available", 20: "available"}
    assert [known.get(page.post_id, "missing") for page in pool.pages] == [
        "available", "missing", "available", "available",
    ]
    assert pool.page_at(3) == pool.pages[2]
    assert pool.page_at(5) is None
    with pytest.raises(FrozenInstanceError):
        pool.post_ids = ()


def test_empty_and_maximum_pool_are_valid_without_media():
    empty = DanbooruPool(1, "empty", PoolCategory.COLLECTION, ())
    assert empty.pages == () and empty.post_count == 0
    maximum = replace(empty, post_ids=(1,) * 25000)
    assert maximum.post_count == 25000
    assert maximum.page_at(25000).page_number == 25000


@pytest.mark.parametrize("changes", [
    {"id": True}, {"id": 0}, {"name": " "}, {"category": "series"},
    {"post_ids": [1]}, {"post_ids": (False,)}, {"post_ids": (1,) * 25001},
    {"description": "x" * 20001}, {"is_deleted": 0},
])
def test_pool_rejects_invalid_values(changes):
    with pytest.raises(PoolError):
        replace(DanbooruPool(1, "pool", PoolCategory.SERIES, (3,)), **changes)


@pytest.mark.parametrize("page,limit", [(True, 1), (0, 1), (1001, 1), (1, 0), (1, 101)])
def test_membership_page_is_bounded(page, limit):
    with pytest.raises(PoolError):
        PoolMembershipPage(3, page, limit, ())


def test_membership_page_requires_filtered_target_and_limit():
    pool = DanbooruPool(1, "pool", PoolCategory.SERIES, (3,))
    assert PoolMembershipPage(3, 2, 1, (pool,)).pools == (pool,)
    for values in ((replace(pool, post_ids=(4,)),), (replace(pool, is_deleted=True),), (pool, pool)):
        with pytest.raises(PoolError):
            PoolMembershipPage(3, 2, 1, values)


@pytest.mark.parametrize("state,error_code", [
    ("success", "error"), ("failed", "error"),
    (PoolRequestState.SUCCESS, []), (PoolRequestState.FAILED, None),
])
def test_outcome_rejects_string_state_and_nontext_error_code(state, error_code):
    identity = PoolRequestIdentity("a" * 32, PoolRequestKind.POOL, 1)
    with pytest.raises(PoolError):
        PoolRequestOutcome(identity, state, error_code=error_code)


def test_outcome_checks_request_identity_and_matching_shape():
    identity = PoolRequestIdentity("a" * 32, PoolRequestKind.POOL, 1)
    pool = DanbooruPool(1, "pool", PoolCategory.SERIES, (3,))
    assert PoolRequestOutcome(identity, PoolRequestState.SUCCESS, pool=pool).succeeded
    with pytest.raises(PoolError):
        PoolRequestOutcome(identity, PoolRequestState.SUCCESS, pool=replace(pool, id=2))
    membership = PoolRequestIdentity("b" * 32, PoolRequestKind.MEMBERSHIPS, 3, 2, 1)
    with pytest.raises(PoolError):
        PoolRequestOutcome(membership, PoolRequestState.SUCCESS,
                           memberships=PoolMembershipPage(3, 1, 1, (pool,)))


def test_pool_public_types_and_parsers_load_without_qt_or_storage():
    code = (
        "import sys; from danbooru_multi_search.danbooru import DanbooruPool, "
        "PoolCategory, parse_pool_response; "
        "assert DanbooruPool(1, 'pool', PoolCategory.SERIES, ()).post_count == 0; "
        "assert not any(n.startswith(('PySide6', 'sqlite3', 'danbooru_multi_search.storage')) for n in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
    from danbooru_multi_search.danbooru import DanbooruPool as PublicPool
    assert PublicPool is DanbooruPool
