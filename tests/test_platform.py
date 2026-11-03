from __future__ import annotations

from pathlib import Path

import pytest

from danbooru_multi_search.integrations.eagle import (
    EagleBridge,
    EagleWebApiBridge,
    UnavailableEagleBridge,
)
from danbooru_multi_search.platform import (
    NOT_IMPLEMENTED,
    UNSUPPORTED_PLATFORM,
    CredentialStore,
    ExternalOpenService,
    MediaCapabilityService,
    PlatformPaths,
    QtPlatformPaths,
    ServiceAvailability,
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
    assert eagle.list_folders().code == UNSUPPORTED_PLATFORM


def test_windows_adapter_uses_the_real_eagle_bridge_without_connecting(tmp_path):
    isolated_root = tmp_path / "not-created"
    adapter = create_platform_adapter("win32", override_root=isolated_root)

    assert isinstance(adapter.services.eagle, EagleWebApiBridge)
    assert adapter.services.eagle.is_supported is True
    assert adapter.services.eagle.availability.available is True
    assert adapter.capabilities.local_eagle is True
    assert not isolated_root.exists()


@pytest.mark.parametrize("platform_name", ["win32", "linux"])
def test_unavailable_services_return_results_without_system_side_effects(
    tmp_path,
    platform_name,
):
    isolated_root = tmp_path / platform_name
    eagle_bridge = None
    if platform_name == "win32":
        eagle_bridge = UnavailableEagleBridge(
            availability=ServiceAvailability.unavailable(
                NOT_IMPLEMENTED,
                "Eagle test bridge is intentionally unavailable.",
            ),
            is_supported=True,
        )
    adapter = create_platform_adapter(
        platform_name,
        override_root=isolated_root,
        eagle_bridge=eagle_bridge,
    )
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
    if platform_name == "win32" and services.wallpaper.availability.available:
        assert results[0].code == "file_not_found"
        assert results[1].code == "no_restore_point"
    elif platform_name == "win32":
        assert results[0].code in {
            UNSUPPORTED_PLATFORM,
            "windows_api_unavailable",
        }
        assert results[1].code == results[0].code
    else:
        assert results[0].code == NOT_IMPLEMENTED
        assert results[1].code == NOT_IMPLEMENTED
    assert all(result.code == NOT_IMPLEMENTED for result in results[2:-3])


def test_path_service_resolution_does_not_create_directories(tmp_path):
    isolated_root = tmp_path / "not-created"
    path_service: PlatformPaths = QtPlatformPaths(isolated_root)

    resolved = path_service.resolve()

    assert resolved.config_dir == isolated_root / "config"
    assert not isolated_root.exists()


@pytest.mark.parametrize(
    ("platform_name", "expected_code"),
    [("linux", NOT_IMPLEMENTED), ("darwin", UNSUPPORTED_PLATFORM)],
)
def test_non_windows_wallpaper_service_is_side_effect_free(
    tmp_path,
    platform_name,
    expected_code,
):
    isolated_root = tmp_path / platform_name
    wallpaper = create_platform_adapter(
        platform_name,
        override_root=isolated_root,
    ).services.wallpaper

    results = [
        wallpaper.get_current(),
        wallpaper.set_wallpaper(isolated_root / "image.png"),
        wallpaper.restore_previous(),
    ]

    assert all(result.succeeded is False for result in results)
    assert all(result.code == expected_code for result in results)
    assert not isolated_root.exists()


@pytest.mark.parametrize(
    ("platform_name", "expected_code"),
    [("linux", UNSUPPORTED_PLATFORM), ("darwin", UNSUPPORTED_PLATFORM)],
)
def test_non_windows_eagle_bridge_never_connects_or_reads_files(
    tmp_path,
    platform_name,
    expected_code,
):
    isolated_root = tmp_path / f"eagle-{platform_name}"
    eagle = create_platform_adapter(
        platform_name,
        override_root=isolated_root,
    ).services.eagle

    results = [
        eagle.test_connection(),
        eagle.get_current_library(),
        eagle.list_folders(),
        eagle.import_file(isolated_root / "missing.png"),
    ]

    assert all(result.succeeded is False for result in results)
    assert all(result.code == expected_code for result in results)
    assert not isolated_root.exists()
