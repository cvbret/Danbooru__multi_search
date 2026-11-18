import pytest

from danbooru_multi_search.app.cache_protection import CacheProtectionService
from danbooru_multi_search.app.write_access import SharedWriteAccess
from danbooru_multi_search.storage.cache_protection import CacheProtectionError, CacheProtectionRepository
from danbooru_multi_search.storage.database import Database


@pytest.fixture
def protection(tmp_path):
    database = Database(tmp_path / "protection.sqlite3")
    database.initialize()
    service = CacheProtectionService(CacheProtectionRepository(database), SharedWriteAccess())
    service.reload()
    writer = CacheProtectionRepository(Database(database.path))
    return service, writer


def write_alias(writer):
    writer.remember_redirect("https://example.invalid/a", "https://cdn.invalid/b")


def test_lock_covers_key_conversion_and_the_complete_cache_consumer(protection):
    service, writer = protection
    observed = []

    def convert(snapshot):
        with pytest.raises(CacheProtectionError, match="locked"):
            write_alias(writer)
        observed.append("convert")
        return frozenset({b"protected"})

    with service.protection(convert) as keys:
        assert keys == frozenset({b"protected"})
        with pytest.raises(CacheProtectionError, match="locked"):
            write_alias(writer)
        observed.append("delete")
    write_alias(writer)
    assert observed == ["convert", "delete"]


def test_known_preparation_failure_releases_before_the_single_none_yield(protection):
    service, writer = protection
    messages = []
    service.report_status = messages.append

    def convert(snapshot):
        raise ValueError("conversion failed")

    with service.protection(convert) as keys:
        assert keys is None
        write_alias(writer)
    assert len(messages) == 1 and "conversion failed" in messages[0]
    assert not service.can_store()


@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
def test_unknown_preparation_error_releases_lock_even_while_traceback_is_retained(protection, error_type):
    service, writer = protection
    error = error_type("unexpected conversion error")

    def convert(snapshot):
        raise error

    with pytest.raises(error_type) as caught:
        with service.protection(convert):
            pytest.fail("unexpected preparation errors must propagate")
    retained = [error, caught.value, caught.value.__traceback__]
    assert caught.value is error
    write_alias(writer)
    assert retained[0] is error


def test_consumer_error_propagates_once_without_changing_snapshot_error(protection):
    service, writer = protection
    error = ValueError("consumer failed")
    messages = []
    service.report_status = messages.append
    with pytest.raises(ValueError) as caught:
        with service.protection(lambda snapshot: frozenset({b"protected"})) as keys:
            assert keys == frozenset({b"protected"})
            raise error
    assert caught.value is error
    assert messages == [] and service.error == ""
    write_alias(writer)
