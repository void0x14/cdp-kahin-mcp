"""Unit tests for kahin._obscura — asset naming, discovery, install cascade."""

from pathlib import Path

import pytest

import kahin._obscura as obscura_mod

# --------------------------------------------------------------------------- #
# asset_name()
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("system", "machine", "stealth", "expected"),
    [
        ("linux", "x86_64", False, "obscura-x86_64-linux.tar.gz"),
        ("linux", "x86_64", True, "obscura-x86_64-linux-stealth.tar.gz"),
        ("linux", "amd64", False, "obscura-x86_64-linux.tar.gz"),
        ("linux", "aarch64", False, "obscura-aarch64-linux.tar.gz"),
        ("linux", "arm64", False, "obscura-aarch64-linux.tar.gz"),
        ("darwin", "x86_64", False, "obscura-x86_64-macos.tar.gz"),
        ("darwin", "aarch64", True, "obscura-aarch64-macos-stealth.tar.gz"),
        ("windows", "x86_64", False, "obscura-x86_64-windows.zip"),
        ("windows", "x86_64", True, "obscura-x86_64-windows-stealth.zip"),
    ],
)
def test_asset_name(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    stealth: bool,
    expected: str,
) -> None:
    monkeypatch.setattr(obscura_mod, "SYSTEM", system)
    monkeypatch.setattr("platform.machine", lambda: machine)
    assert obscura_mod.asset_name(stealth=stealth) == expected


@pytest.mark.parametrize(
    ("system", "machine"),
    [
        ("windows", "aarch64"),  # no Windows build for arm
        ("plan9", "x86_64"),     # unsupported platform
    ],
)
def test_asset_name_unsupported(
    monkeypatch: pytest.MonkeyPatch, system: str, machine: str
) -> None:
    monkeypatch.setattr(obscura_mod, "SYSTEM", system)
    monkeypatch.setattr("platform.machine", lambda: machine)
    with pytest.raises(RuntimeError):
        obscura_mod.asset_name()


def test_asset_name_unknown_architecture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(obscura_mod, "SYSTEM", "linux")
    monkeypatch.setattr("platform.machine", lambda: "mips")
    with pytest.raises(RuntimeError, match="Unsupported architecture"):
        obscura_mod.asset_name()


# --------------------------------------------------------------------------- #
# find_obscura()
# --------------------------------------------------------------------------- #

def test_find_obscura_managed_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OBSCURA_PATH", raising=False)
    version_dir = tmp_path / "v0.1.11"
    version_dir.mkdir(parents=True)
    binary = version_dir / "obscura"
    worker = version_dir / "obscura-worker"
    binary.write_bytes(b"#!/bin/sh\necho fake\n")
    worker.write_bytes(b"#!/bin/sh\necho fake\n")
    binary.chmod(0o755)
    worker.chmod(0o755)
    monkeypatch.setenv("KAHIN_OBSCURA_DIR", str(tmp_path))

    found = obscura_mod.find_obscura()
    assert found == str(binary)


def test_find_obscura_empty_dir_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OBSCURA_PATH", raising=False)
    monkeypatch.setenv("KAHIN_OBSCURA_DIR", str(tmp_path))
    assert obscura_mod.find_obscura() is None


def test_find_obscura_non_executable_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OBSCURA_PATH", raising=False)
    version_dir = tmp_path / "v0.1.11"
    version_dir.mkdir(parents=True)
    (version_dir / "obscura").write_bytes(b"not executable")
    monkeypatch.setenv("KAHIN_OBSCURA_DIR", str(tmp_path))
    assert obscura_mod.find_obscura() is None


# --------------------------------------------------------------------------- #
# install_obscura() cascade
# --------------------------------------------------------------------------- #

def test_install_cascade_falls_through_to_github(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(obscura_mod, "SYSTEM", "linux")
    monkeypatch.setattr(obscura_mod, "_native_pm_install", lambda timeout: None)
    monkeypatch.setattr(obscura_mod, "_aur_install", lambda timeout: None)
    monkeypatch.setattr(obscura_mod, "_flatpak_install", lambda timeout: None)
    monkeypatch.setattr(obscura_mod, "_appimage_install", lambda: None)
    github_binary = Path("/fake/github/obscura")
    monkeypatch.setattr(
        obscura_mod,
        "_install_from_github",
        lambda timeout, stealth=False: github_binary,
    )

    assert obscura_mod.install_obscura() == str(github_binary)


def test_install_cascade_native_pm_wins_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(obscura_mod, "SYSTEM", "linux")
    native_binary = Path("/fake/native/obscura")
    monkeypatch.setattr(obscura_mod, "_native_pm_install", lambda timeout: native_binary)
    monkeypatch.setattr(obscura_mod, "_aur_install", lambda timeout: None)
    monkeypatch.setattr(obscura_mod, "_flatpak_install", lambda timeout: None)
    monkeypatch.setattr(obscura_mod, "_appimage_install", lambda: None)
    monkeypatch.setattr(
        obscura_mod,
        "_install_from_github",
        lambda timeout, stealth=False: Path("/fake/github/obscura"),
    )

    assert obscura_mod.install_obscura() == str(native_binary)


def test_install_cascade_total_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(obscura_mod, "SYSTEM", "linux")
    monkeypatch.setattr(obscura_mod, "_native_pm_install", lambda timeout: None)
    monkeypatch.setattr(obscura_mod, "_aur_install", lambda timeout: None)
    monkeypatch.setattr(obscura_mod, "_flatpak_install", lambda timeout: None)
    monkeypatch.setattr(obscura_mod, "_appimage_install", lambda: None)
    monkeypatch.setattr(obscura_mod, "_install_from_github", lambda timeout, stealth=False: None)

    with pytest.raises(
        RuntimeError, match="Could not find or install the Obscura browser binary"
    ):
        obscura_mod.install_obscura()


def test_install_cascade_step_exception_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(obscura_mod, "SYSTEM", "linux")
    github_binary = Path("/fake/github/obscura")

    def exploding(timeout: float) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(obscura_mod, "_native_pm_install", exploding)
    monkeypatch.setattr(obscura_mod, "_aur_install", lambda timeout: None)
    monkeypatch.setattr(obscura_mod, "_flatpak_install", lambda timeout: None)
    monkeypatch.setattr(obscura_mod, "_appimage_install", lambda: None)
    monkeypatch.setattr(
        obscura_mod,
        "_install_from_github",
        lambda timeout, stealth=False: github_binary,
    )

    # Exception in one step must not abort the cascade.
    assert obscura_mod.install_obscura() == str(github_binary)
