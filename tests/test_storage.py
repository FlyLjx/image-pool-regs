from __future__ import annotations

import json

from app.storage import DEFAULT_SETTINGS, JsonStore


def test_json_store_creates_and_updates_atomically(tmp_path):
    store = JsonStore(tmp_path)
    assert store.read("items.json", []) == []

    result = store.update("items.json", [], lambda items: [*items, {"id": 1}])

    assert result == [{"id": 1}]
    assert json.loads((tmp_path / "items.json").read_text(encoding="utf-8")) == [{"id": 1}]
    assert not list(tmp_path.glob("*.tmp"))


def test_settings_deep_merge_defaults(tmp_path):
    store = JsonStore(tmp_path)
    store.write("settings.json", {"registration": {"count": 7}})

    settings = store.settings()

    assert settings["registration"]["count"] == 7
    assert settings["registration"]["concurrency"] == DEFAULT_SETTINGS["registration"]["concurrency"]
    assert settings["mail"]["api_base"] == DEFAULT_SETTINGS["mail"]["api_base"]
    assert "codex_agent" not in settings
    assert "sub2api" not in settings


def test_settings_removes_legacy_codex_and_sub2api_sections(tmp_path):
    store = JsonStore(tmp_path)
    store.write("settings.json", {
        "codex_agent": {"enabled": True},
        "sub2api": {"enabled": True, "admin_api_key": "legacy-secret"},
    })

    settings = store.settings()

    assert "codex_agent" not in settings
    assert "sub2api" not in settings
    persisted = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert "codex_agent" not in persisted
    assert "sub2api" not in persisted
