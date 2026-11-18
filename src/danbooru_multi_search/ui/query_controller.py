"""Assemble the default Qt gallery model for the M2 query coordinator."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import cast

from PySide6.QtCore import QObject

from danbooru_multi_search.danbooru.query_controller import M2QueryCoordinator
from danbooru_multi_search.danbooru.query_execution import DanbooruMultiBranchExecutor
from danbooru_multi_search.danbooru.tag_translation import TagTranslationStore
from danbooru_multi_search.ui.gallery import DanbooruPostListModel


class DanbooruM2QueryController(M2QueryCoordinator):
    """Keep the public UI constructor and its concrete gallery model."""

    def __init__(
        self,
        base_url: str,
        *,
        executor: DanbooruMultiBranchExecutor | None = None,
        tag_store: TagTranslationStore | None = None,
        posts_model: DanbooruPostListModel | None = None,
        clock: Callable[[], datetime] | None = None,
        parent: QObject | None = None,
    ) -> None:
        model = posts_model if posts_model is not None else DanbooruPostListModel()
        super().__init__(
            base_url,
            executor=executor,
            tag_store=tag_store,
            posts_model=model,
            clock=clock,
            parent=parent,
        )
        if posts_model is None:
            model.setParent(self)

    @property
    def posts_model(self) -> DanbooruPostListModel:
        return cast(DanbooruPostListModel, super().posts_model)
