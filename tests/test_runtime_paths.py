from pathlib import Path

import config_init
import db_operations
import server
import web_admin
from app_paths import get_app_root


def test_initialize_config_uses_repo_root_default_when_cwd_changes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BBS_CONFIG_PATH", raising=False)

    config = config_init.initialize_config()

    assert Path(config["config_file"]) == Path(get_app_root()) / "config.ini"


def test_get_database_path_uses_repo_root_default_when_cwd_changes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BBS_DB_PATH", raising=False)

    assert Path(db_operations.get_database_path()) == Path(get_app_root()) / "bulletins.db"


def test_relative_trigger_paths_resolve_under_repo_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BBS_PEER_RESYNC_TRIGGER_PATH", "tmp-peer.trigger")

    expected = Path(get_app_root()) / "tmp-peer.trigger"
    if expected.exists():
        expected.unlink()

    assert Path(server.get_peer_resync_trigger_path()) == expected
    assert Path(web_admin.get_peer_resync_trigger_path()) == expected

    web_admin.request_peer_resync_trigger("!0408b778")

    assert expected.exists()
    assert expected.read_text(encoding="utf-8") == "!0408b778"

    expected.unlink()