from __future__ import annotations

from pathlib import Path

import pytest

from danbooru_multi_search.integrations.eagle import EagleBridge
from danbooru_multi_search.platform import (
    NOT_IMPLEMENTED,
    UNSUPPORTED_PLATFORM,
    CredentialStore,
    ExternalOpenService,
    MediaCapabilityService,
    PlatformPaths,
    QtPlatformPaths,
    SystemInfoService,
    WallpaperService,
    create_platform_adapter,
    resolve_application_paths,
)


def test_override_paths_are_isolated_and_created(tmp_path):
    paths = resolve_application_paths(override_root=tmp_path)
    paths.ensure_directories()

    assert paths.config_dir == tmp_path / "config"
    assert paths.data_dir == tmp_path / "data"
    assert paths.cache_dir == tmp_path / "cache"
    assert paths.log_dir == tmp_path / "logs"
    assert paths.temp_dir == tmp_path / "temp"
    assert paths.download_dir == tmp_path / "downloads"
    assert paths.config_dir.is_dir()
    assert paths.data_dir.is_dir()


@pytest.mark.parametrize(
    ("platform_name", "expected_os_name"),
    [("win32", "windows"), ("linux", "linux")],
)
def test_platform_adapters_provide_the_complete_service_container(
    tmp_path,
    platform_name,
    expected_os_name,
):
    path_service = QtPlatformPaths(tmp_path / expected_os_name)
    adapter = create_platform_adapter(
        platform_name,
        path_service=path_service,
    )
    services = adapter.services

    assert adapter.capabilities.os_name == expected_os_name
    assert services.paths is path_service
    assert isinstance(services.paths, PlatformPaths)
    assert isinstance(services.wallpaper, WallpaperService)
    assert isinstance(services.media, MediaCapabilityService)
    assert isinstance(services.eagle, EagleBridge)
    assert isinstance(services.external_open, ExternalOpenService)
    assert isinstance(services.credentials, CredentialStore)
    assert isinstance(services.system_info, SystemInfoService)


def test_linux_eagle_bridge_has_an_explicit_unsupported_state(tmp_path):
    adapter = create_platform_adapter("linux", override_root=tmp_path)
    eagle = adapter.services.eagle

    assert eagle.is_supported is False
    assert eagle.availability.available is False
    assert eagle.availability.code == UNSUPPORTED_PLATFORM
    assert eagle.unavailable_reason is not None
    assert "official Linux desktop client" in eagle.unavailable_reason
    assert eagle.test_connection().code == UNSUPPORTED_PLATFORM
    assert eagle.get_current_library().code == UNSUPPORTED_PLATFORM


@pytest.mark.parametrize("platform_name", ["win32", "linux"])
def test_unavailable_services_return_results_without_system_side_effects(
    tmp_path,
    platform_name,
):
    isolated_root = tmp_path / platform_name
    adapter = create_platform_adapter(platform_name, override_root=isolated_root)
    services = adapter.services
    absent_file = isolated_root / "input.png"
    absent_directory = isolated_root / "folder"

    results = [
        services.wallpaper.set_wallpaper(absent_file),
        services.wallpaper.restore_previous(),
        services.media.can_play("webm"),
        services.external_open.open_file(absent_file),
        services.external_open.open_directory(absent_directory),
        services.external_open.open_url("https://example.invalid/"),
        services.credentials.read("danbooru-api-key"),
        services.credentials.write("danbooru-api-key", "secret"),
        services.credentials.delete("danbooru-api-key"),
        services.system_info.get_info(),
        services.eagle.test_connection(),
        services.eagle.get_current_library(),
        services.eagle.import_file(
            absent_file,
            folder_identifier="folder-id",
            metadata={"source": "test"},
        ),
    ]

    assert all(result.succeeded is False for result in results)
    assert all(result.message for result in results)
    assert all(result.value is None for result in results if hasattr(result, "value"))
    assert not isolated_root.exists()

    expected_eagle_code = (
        NOT_IMPLEMENTED if platform_name == "win32" else UNSUPPORTED_PLATFORM
    )
    assert results[-1].code == expected_eagle_code
    assert all(result.code == NOT_IMPLEMENTED for result in results[:-3])


def test_path_service_resolution_does_not_create_directories(tmp_path):
    isolated_root = tmp_path / "not-created"
    path_service: PlatformPaths = QtPlatformPaths(isolated_root)

    resolved = path_service.resolve()

    assert resolved.config_dir == isolated_root / "config"
    assert not isolated_root.exists()
