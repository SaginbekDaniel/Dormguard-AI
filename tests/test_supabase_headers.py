"""Provider header regressions use a labeled transport double; no hosted success claim."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dormguard.config import Settings
from dormguard.supabase_auth import SupabaseAuth


class SupabaseHeaderTests(unittest.TestCase):
    def test_new_publishable_key_is_api_key_only_until_user_jwt_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = SupabaseAuth(
                Settings(
                    Path(directory),
                    supabase_url="https://controlled.example.invalid",
                    supabase_anon_key="sb_publishable_CONTROLLED",
                )
            )
            with patch(
                "dormguard.supabase_auth.request_json",
                return_value={"access_token": "CONTROLLED_USER_JWT", "user": {"id": "CONTROLLED"}},
            ) as transport:
                provider.login("controlled@example.invalid", "controlled-password")
                self.assertEqual(transport.call_args.args[2], {"apikey": "sb_publishable_CONTROLLED"})
                transport.return_value = {"id": "CONTROLLED"}
                provider.validate("CONTROLLED_USER_JWT")
                self.assertEqual(
                    transport.call_args.args[2],
                    {"apikey": "sb_publishable_CONTROLLED", "Authorization": "Bearer CONTROLLED_USER_JWT"},
                )
                provider.logout("CONTROLLED_USER_JWT")
                self.assertEqual(transport.call_args.args[2]["Authorization"], "Bearer CONTROLLED_USER_JWT")

    def test_legacy_jwt_key_retains_header_compatibility_and_modern_admin_key_is_not_bearer(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = SupabaseAuth(
                Settings(
                    Path(directory),
                    supabase_url="https://controlled.example.invalid",
                    supabase_anon_key="CONTROLLED_LEGACY_ANON_JWT",
                )
            )
            with patch(
                "dormguard.supabase_auth.request_json",
                return_value={"access_token": "CONTROLLED_USER_JWT", "user": {"id": "CONTROLLED"}},
            ) as transport:
                provider.login("controlled@example.invalid", "controlled-password")
                self.assertEqual(
                    transport.call_args.args[2]["Authorization"], "Bearer CONTROLLED_LEGACY_ANON_JWT"
                )
                provider.invite("controlled@example.invalid", "sb_secret_CONTROLLED_NOT_REAL")
                self.assertEqual(transport.call_args.args[2], {"apikey": "sb_secret_CONTROLLED_NOT_REAL"})
