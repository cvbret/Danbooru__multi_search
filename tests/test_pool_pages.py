from dataclasses import replace

import pytest

from danbooru_multi_search.core.pool_pages import PoolPageBatch, PoolPageSlot, PoolPageState, PoolPostMetadata
from danbooru_multi_search.core.pools import PoolError, PoolPageReference
from tests.test_favorite_controller import post


def test_page_identity_keeps_duplicate_posts_at_distinct_original_positions():
    metadata = PoolPostMetadata(9, PoolPageState.AVAILABLE, post(9))
    slots = tuple(PoolPageSlot(PoolPageReference(7, page, 9), metadata) for page in (1, 3))
    assert [slot.key for slot in PoolPageBatch(slots).slots] == ["7:1", "7:3"]


@pytest.mark.parametrize("change", [
    {"state": "available"}, {"post_id": True}, {"post_id": 2},
    {"state": PoolPageState.DELETED}, {"message": None},
])
def test_page_metadata_rejects_inconsistent_values(change):
    with pytest.raises(PoolError):
        replace(PoolPostMetadata(1, PoolPageState.AVAILABLE, post(1)), **change)


def test_batch_rejects_duplicate_positions_and_more_than_one_window():
    slot = PoolPageSlot(PoolPageReference(1, 1, 3), PoolPostMetadata(3, PoolPageState.LOADING))
    for slots in ((), (slot, slot), (slot,) * 41):
        with pytest.raises(PoolError):
            PoolPageBatch(slots)
