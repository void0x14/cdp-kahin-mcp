from pathlib import Path

import pytest

from kahin.the_twins.mirage import _profile_directory


def test_default_profile_is_stable_under_xdg_data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("KAHIN_PROFILE_DIR", raising=False)

    path, persistent = _profile_directory(True, None)

    assert persistent is True
    assert path == tmp_path / "kahin" / "profile"


def test_ephemeral_profile_is_not_stable():
    path, persistent = _profile_directory(False, None)

    assert persistent is False
    assert path is None


def test_configured_profile_must_be_absolute():
    with pytest.raises(ValueError, match="absolute"):
        _profile_directory(True, "relative-profile")


def test_configured_profile_is_expanded(tmp_path):
    path, persistent = _profile_directory(True, str(tmp_path / "kahin-profile"))

    assert persistent is True
    assert path == tmp_path / "kahin-profile"


def test_default_profile_override_is_used(tmp_path, monkeypatch):
    configured = tmp_path / "override"
    monkeypatch.setenv("KAHIN_PROFILE_DIR", str(configured))

    path, persistent = _profile_directory(True, None)

    assert persistent is True
    assert path == configured
