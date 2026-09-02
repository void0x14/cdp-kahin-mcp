import json
import zipfile
from pathlib import Path

import pytest

from kahin.extensions import stage_extension


def write_extension(root: Path, manifest: dict) -> Path:
    source = root / "source"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "content.js").write_text("document.documentElement.dataset.kahin = '1';", encoding="utf-8")
    return source


def test_prepare_manifest_v3_content_scripts(tmp_path):
    source = write_extension(
        tmp_path,
        {
            "manifest_version": 3,
            "name": "Reader",
            "version": "1",
            "content_scripts": [{"matches": ["<all_urls>"], "js": ["content.js"]}],
        },
    )

    result = stage_extension(str(source), tmp_path / "staged")

    assert result["compatible"] is True
    assert (Path(result["path"]) / "manifest.json").is_file()
    assert (Path(result["path"]) / "content.js").is_file()
    assert result["manifest"]["contentScriptCount"] == 1


def test_service_worker_is_explicitly_unsupported(tmp_path):
    source = write_extension(
        tmp_path,
        {
            "manifest_version": 3,
            "name": "Worker",
            "version": "1",
            "background": {"service_worker": "background.js"},
        },
    )

    result = stage_extension(str(source), tmp_path / "staged")

    assert result["compatible"] is False
    assert "background.service_worker" in result["unsupported"]


def test_zip_path_traversal_is_rejected(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps({"manifest_version": 3, "name": "Unsafe", "version": "1"}))
        bundle.writestr("../escape.js", "bad")

    with pytest.raises(ValueError, match="path traversal"):
        stage_extension(str(archive), tmp_path / "staged")


def test_missing_manifest_is_rejected(tmp_path):
    source = tmp_path / "empty"
    source.mkdir()

    with pytest.raises(ValueError, match="manifest.json"):
        stage_extension(str(source), tmp_path / "staged")
