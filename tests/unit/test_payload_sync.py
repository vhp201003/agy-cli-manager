import json
from pathlib import Path

import pytest

from agy_cli_manager.manager import (
    build_paths,
    default_live_dir,
    delete_account,
    ensure_layout,
    export_account_payload,
    import_account_payload,
    load_state,
    save_state,
    validate_account_name,
)


def _write_mock_gemini_home(root: Path, email: str = "dev@example.com") -> Path:
    home = root / ".gemini" / "antigravity-cli"
    home.mkdir(parents=True, exist_ok=True)
    (home / "antigravity-oauth-token").write_text(
        json.dumps(
            {
                "email": email,
                "name": "Dev User",
                "token": {
                    "access_token": "ya29.test-access",
                    "refresh_token": "1//test-refresh",
                    "expiry_timestamp": 1893456000.0,
                },
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def paths(tmp_path: Path):
    paths = build_paths(tmp_path / "root")
    ensure_layout(paths)
    state = load_state(paths)
    live_dir = paths.root / "live" / ".gemini"
    state["live_dir"] = str(live_dir)
    save_state(paths, state)
    live_dir.mkdir(parents=True, exist_ok=True)
    return paths


def test_export_from_standard_gemini_home(paths, tmp_path):
    source = _write_mock_gemini_home(tmp_path / "home")

    payload = export_account_payload(paths, source_dir=source)

    assert payload["identity"]["email"] == "dev@example.com"
    assert payload["token_data"]["access_token"] == "ya29.test-access"
    assert payload["token_data"]["refresh_token"] == "1//test-refresh"
    assert payload["format_version"] == "1.0"


def test_export_missing_auth_files_raises(paths, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(ValueError):
        export_account_payload(paths, source_dir=empty)


def test_import_payload_creates_structure(paths):
    payload = {
        "format_version": "1.0",
        "identity": {"email": "dev@example.com", "display_name": "Dev User"},
        "token_data": {
            "access_token": "ya29.test-access",
            "refresh_token": "1//test-refresh",
            "expiry_timestamp": 1893456000.0,
        },
    }

    meta = import_account_payload(paths, "work-account", payload)

    token_file = paths.accounts_dir / "work-account" / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    assert token_file.is_file()
    stored = json.loads(token_file.read_text(encoding="utf-8"))
    assert stored["token"]["access_token"] == "ya29.test-access"
    assert meta["identity"]["email"] == "dev@example.com"

    state = load_state(paths)
    assert "work-account" in state["accounts"]


def test_import_existing_account_without_overwrite_fails(paths):
    payload = {
        "identity": {"email": "dev@example.com"},
        "token_data": {"access_token": "ya29.test-access", "refresh_token": "1//test-refresh"},
    }
    import_account_payload(paths, "work-account", payload)

    with pytest.raises(ValueError):
        import_account_payload(paths, "work-account", payload)

    import_account_payload(paths, "work-account", payload, overwrite=True)


def test_export_import_round_trip_preserves_tokens(paths, tmp_path):
    source = _write_mock_gemini_home(tmp_path / "home")
    payload = export_account_payload(paths, source_dir=source)

    import_account_payload(paths, "roundtrip", payload)

    token_file = paths.accounts_dir / "roundtrip" / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    stored = json.loads(token_file.read_text(encoding="utf-8"))
    assert stored["token"]["access_token"] == "ya29.test-access"
    assert stored["token"]["refresh_token"] == "1//test-refresh"


def test_delete_account_cleans_all_artifacts(paths):
    payload = {
        "identity": {"email": "dev@example.com"},
        "token_data": {"access_token": "ya29.test-access", "refresh_token": "1//test-refresh"},
    }
    import_account_payload(paths, "work-account", payload)

    result = delete_account(paths, "work-account")

    assert result["deleted"] == "work-account"
    assert not (paths.accounts_dir / "work-account").exists()
    assert "work-account" not in load_state(paths)["accounts"]


def test_delete_active_account_promotes_standby(paths):
    payload = {
        "identity": {"email": "dev@example.com"},
        "token_data": {"access_token": "ya29.test-access", "refresh_token": "1//test-refresh"},
    }
    import_account_payload(paths, "account-a", payload)
    import_account_payload(paths, "account-b", payload)

    result = delete_account(paths, "account-a")

    assert result["was_active"] is True
    assert result["promoted_to"] == "account-b"
    assert load_state(paths)["active"] == "account-b"


def test_delete_last_account_clears_the_live_credential(paths):
    payload = {
        "identity": {"email": "dev@example.com"},
        "token_data": {"access_token": "ya29.test-access", "refresh_token": "1//test-refresh"},
    }
    import_account_payload(paths, "only-account", payload)

    live_token = paths.root / "live" / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    assert live_token.is_file()

    delete_account(paths, "only-account")

    assert not live_token.exists()


def test_sandboxed_manager_never_touches_the_global_live_profile(paths, tmp_path):
    """A manager rooted outside the default installation must not write ~/.gemini."""
    global_token = default_live_dir() / "antigravity-cli" / "antigravity-oauth-token"
    before = global_token.read_bytes() if global_token.is_file() else None

    source = _write_mock_gemini_home(tmp_path / "home")
    payload = export_account_payload(paths, source_dir=source)
    import_account_payload(paths, "sandboxed", payload)
    delete_account(paths, "sandboxed")

    after = global_token.read_bytes() if global_token.is_file() else None
    assert after == before


def test_validate_account_name_rejects_traversal():
    for unsafe in ("../evil", "a/b", "a\\b", "", ".."):
        with pytest.raises(ValueError):
            validate_account_name(unsafe)

    assert validate_account_name("developer.alice_1") == "developer.alice_1"
