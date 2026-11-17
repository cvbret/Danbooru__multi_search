from danbooru_multi_search.app.write_access import SharedWriteAccess

import pytest


def test_maintenance_is_visible_before_notification_and_cleared_before_release():
    access = SharedWriteAccess()
    facts = []
    access.changed.connect(
        lambda: facts.append((access.external_owner, access.maintenance_owner, access.busy))
    )
    assert access.begin_external_write("cleanup", maintenance=True)
    assert facts == [("cleanup", "cleanup", True)]
    access.finish_external_write("cleanup")
    assert facts[-1] == (None, None, False)


def test_failed_acquisition_and_wrong_release_leave_the_existing_owner_untouched():
    access = SharedWriteAccess()
    assert access.begin_external_write("import")
    assert not access.begin_external_write("cleanup", maintenance=True)
    access.finish_external_write("cleanup")
    assert access.external_owner == "import"
    assert access.maintenance_owner is None and access.busy


def test_close_preserves_held_maintenance_without_emitting_or_blocking_matching_release():
    access = SharedWriteAccess()
    events = []
    access.changed.connect(lambda: events.append(access.external_owner))
    assert access.begin_external_write("cleanup", maintenance=True)
    access.close()
    assert access.closed and access.busy
    assert access.external_owner == access.maintenance_owner == "cleanup"
    assert events == ["cleanup"]
    assert not access.begin_external_write("new")
    access.finish_external_write("cleanup")
    assert access.external_owner is access.maintenance_owner is None
    assert events == ["cleanup"]


@pytest.mark.parametrize("kind", ["local", "external"])
def test_close_during_acquisition_rolls_back_only_the_failed_acquisition(kind):
    access = SharedWriteAccess()
    access.changed.connect(access.close)
    if kind == "local":
        assert not access.begin_local_write()
    else:
        assert not access.begin_external_write("pending", maintenance=True)
    assert access.closed
    assert not access.local_write_active
    assert access.external_owner is access.maintenance_owner is None


def test_reentrant_replacement_is_not_released_by_the_old_failed_acquisition():
    access = SharedWriteAccess()
    replaced = False

    def replace():
        nonlocal replaced
        if replaced or access.external_owner != "old":
            return
        replaced = True
        access.finish_external_write("old")
        assert access.begin_external_write("replacement", maintenance=True)

    access.changed.connect(replace)
    assert not access.begin_external_write("old")
    assert access.external_owner == access.maintenance_owner == "replacement"


def test_local_write_excludes_all_other_writers_until_release():
    access = SharedWriteAccess()
    assert access.begin_local_write()
    assert access.local_write_active and access.busy
    assert not access.begin_local_write()
    assert not access.begin_external_write("import")
    access.end_local_write()
    assert not access.busy and not access.local_write_active
