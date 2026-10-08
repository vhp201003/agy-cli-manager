from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from agy_cli_manager.proxy.credential_provider import (
    PosixFileCredentialProvider,
    get_credential_provider,
)


class CredentialProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_posix_provider_write_read_atomic(self):
        store = PosixFileCredentialProvider(manager_root=self.root)
        account_name = "test_acc"
        token_payload = {
            "token": {
                "access_token": "ya29.test_token_posix",
                "refresh_token": "1//test_refresh",
                "expiry_timestamp": 1234567890,
            },
            "email": "test@example.com",
            "name": "Test User",
        }

        # Write account
        ok = store.write_account(account_name, token_payload)
        self.assertTrue(ok)

        # File exists at expected POSIX path
        expected_path = self.root / "accounts" / account_name / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        self.assertTrue(expected_path.is_file())

        # Read account
        loaded = store.read_account(account_name)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["token"]["access_token"], "ya29.test_token_posix")
        self.assertEqual(loaded["email"], "test@example.com")

    def test_posix_provider_sync_active(self):
        store = PosixFileCredentialProvider(manager_root=self.root)
        token_payload = {
            "token": {"access_token": "ya29.active_slot"},
            "email": "active@example.com",
        }
        # Point live_token_path to temporary home
        store.live_token_path = self.root / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"

        ok = store.sync_active("active_acc", token_payload)
        self.assertTrue(ok)
        self.assertTrue(store.live_token_path.is_file())

        loaded = json.loads(store.live_token_path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["token"]["access_token"], "ya29.active_slot")


if __name__ == "__main__":
    unittest.main()
