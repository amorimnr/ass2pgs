from __future__ import annotations

from jellyfin_ass2pgs.libass_renderer import _libass_candidates, _libass_install_hint


def test_linux_libass_candidates_honor_overrides_then_native_discovery() -> None:
    candidates = _libass_candidates(
        "/configured/libass.so",
        env_path="/environment/libass.so",
        discovered="libass.so.9",
        os_name="posix",
        platform_name="linux",
    )

    assert candidates == [
        "/configured/libass.so",
        "/environment/libass.so",
        "libass.so.9",
        "libass.so",
    ]


def test_windows_and_macos_do_not_receive_linux_fallback_names() -> None:
    windows = _libass_candidates(
        None,
        env_path=None,
        discovered=None,
        os_name="nt",
        platform_name="win32",
    )
    macos = _libass_candidates(
        None,
        env_path=None,
        discovered=None,
        os_name="posix",
        platform_name="darwin",
    )

    assert windows[-2:] == ["libass-9.dll", "libass.dll"]
    assert "libass.so" not in windows
    assert macos == ["libass.9.dylib", "libass.dylib"]


def test_missing_libass_hint_is_actionable_on_linux_and_windows() -> None:
    linux = _libass_install_hint("posix", "linux")
    windows = _libass_install_hint("nt", "win32")

    assert "sudo apt install libass9" in linux
    assert "LIBASS_PATH" in linux
    assert "mingw-w64-ucrt-x86_64-libass" in windows
    assert "libass-9.dll" in windows
