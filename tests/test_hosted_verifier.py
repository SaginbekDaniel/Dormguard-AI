"""Hosted-verifier guard tests. HTTP/provider doubles are controlled, not live evidence."""

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from scripts.verify_hosted import (
    IDENTITIES,
    Blocked,
    application_checks,
    catalog,
    configuration,
    data_api_denial,
    pinned_database_url,
)

REFERENCE = "sgpeplsengohysoiomyl"
URL = f"https://{REFERENCE}.supabase.co"


def controlled_environment():
    environment = {
        "DORMGUARD_HOSTED_TEST_IDENTITIES_APPROVED": "1",
        "DORMGUARD_HOSTED_PROJECT_NAME": "dormguard AI",
        "DORMGUARD_HOSTED_PROJECT_REF": REFERENCE,
        "SUPABASE_URL": URL,
        "SUPABASE_ANON_KEY": "sb_publishable_controlled_test_double",
    }
    for index, label in enumerate(IDENTITIES, 1):
        prefix = "DORMGUARD_HOSTED_TEST_" + label
        environment[prefix + "_ID"] = f"00000000-0000-0000-0000-{index:012d}"
        environment[prefix + "_EMAIL"] = "dormguard-verify-" + label.lower() + "@example.invalid"
        environment[prefix + "_PASSWORD"] = "controlled-test-password"
    return environment


class HostedVerifierTests(unittest.TestCase):
    def test_pooler_target_pinning_and_required_tls_cannot_be_overridden(self):
        from urllib.parse import parse_qs, urlsplit

        with tempfile.TemporaryDirectory(prefix="dormguard-public-ca-guard-") as directory:
            certificate = Path(directory) / "controlled-public-ca.crt"
            certificate.write_text("Controlled path fixture; no real connection")
            base = f"postgresql://postgres.{REFERENCE}:CONTROLLED@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
            verified = pinned_database_url(base, REFERENCE, str(certificate))
            self.assertEqual(parse_qs(urlsplit(verified).query)["sslmode"], ["verify-full"])
            for invalid in (
                base.replace(REFERENCE, "b" * 20),
                base + "?sslmode=disable",
                base + "?host=unapproved.invalid",
                base.replace(":5432", ":6543"),
            ):
                with self.assertRaises(Blocked):
                    pinned_database_url(invalid, REFERENCE, str(certificate))

    def test_bootstrap_retains_private_intent_on_failure_and_never_invites(self):
        from scripts.create_hosted_test_users import create

        environment = {
            **controlled_environment(),
            "SUPABASE_SERVICE_ROLE_KEY": "sb_secret_CONTROLLED_NOT_REAL",
        }
        calls = []

        def controlled_transport(method, path, **arguments):
            self.assertEqual((method, path), ("POST", "admin/users"))
            self.assertTrue(arguments["body"]["email_confirm"])
            calls.append(arguments["body"])
            if len(calls) == 2:
                raise RuntimeError("CONTROLLED failure, no external operation")
            return {"id": "00000000-0000-0000-0000-000000000001", "email": arguments["body"]["email"]}

        with tempfile.TemporaryDirectory(prefix="dormguard-hosted-bootstrap-test-") as directory:
            output = Path(directory) / "private.json"
            with (
                patch("dormguard.supabase_auth.SupabaseAuth._call", side_effect=controlled_transport),
                self.assertRaises(RuntimeError),
            ):
                create(environment, REFERENCE, True, output)
            retained = json.loads(output.read_text())
            self.assertIn("DORMGUARD_HOSTED_TEST_DIRECTOR_ID", retained)
            self.assertIn("DORMGUARD_HOSTED_TEST_DEPUTY_PASSWORD", retained)
            self.assertNotIn("DORMGUARD_HOSTED_TEST_DEPUTY_ID", retained)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(environment["SUPABASE_SERVICE_ROLE_KEY"], output.read_text())
            with (
                patch("dormguard.supabase_auth.SupabaseAuth._call") as transport,
                self.assertRaises(FileExistsError),
            ):
                create(environment, REFERENCE, True, output)
            transport.assert_not_called()

    def test_bootstrap_requires_private_admin_key_before_network(self):
        from scripts.create_hosted_test_users import create

        with tempfile.TemporaryDirectory(prefix="dormguard-hosted-bootstrap-test-") as directory:
            output = Path(directory) / "private.json"
            with patch("dormguard.supabase_auth.SupabaseAuth._call") as transport, self.assertRaises(Blocked):
                create(controlled_environment(), REFERENCE, True, output)
            transport.assert_not_called()
            self.assertFalse(output.exists())

    def test_preflight_refuses_missing_consent_mismatch_and_secret_keys_before_network(self):
        environment = controlled_environment()
        _, _, identities = configuration(environment, REFERENCE, True)
        self.assertEqual(len(identities), 5)
        cases = (
            ({}, True),
            (environment, False),
            ({**environment, "DORMGUARD_HOSTED_PROJECT_NAME": "unapproved-project"}, True),
            ({**environment, "DORMGUARD_HOSTED_PROJECT_REF": "b" * 20}, True),
            ({**environment, "SUPABASE_URL": "https://unapproved.example.invalid"}, True),
            ({**environment, "SUPABASE_URL": URL + "@unapproved.example.invalid"}, True),
            ({**environment, "SUPABASE_ANON_KEY": "sb_secret_controlled_rejected"}, True),
            ({**environment, "DORMGUARD_HOSTED_TEST_DIRECTOR_EMAIL": "real-person@example.invalid"}, True),
        )
        with patch("scripts.verify_hosted.build_opener") as network:
            for values, consent in cases:
                with self.subTest(consent=consent), self.assertRaises(Blocked):
                    configuration(values, REFERENCE, consent)
            network.assert_not_called()

    def test_legacy_service_or_other_project_key_and_duplicate_identity_are_rejected(self):
        environment = controlled_environment()
        for claims in ({"role": "service_role", "ref": REFERENCE}, {"role": "anon", "ref": "b" * 20}):
            token = (
                "header."
                + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
                + ".signature"
            )
            with self.assertRaises(Blocked):
                configuration({**environment, "SUPABASE_ANON_KEY": token}, REFERENCE, True)
        environment["DORMGUARD_HOSTED_TEST_UNASSIGNED_ID"] = environment["DORMGUARD_HOSTED_TEST_DIRECTOR_ID"]
        with self.assertRaises(Blocked):
            configuration(environment, REFERENCE, True)

    def test_missing_table_bad_key_and_outage_cannot_be_reported_as_rls_pass(self):
        for status, body in ((404, {"code": "PGRST205"}), (401, {"message": "Invalid API key"}), (503, {})):
            error = HTTPError(URL, status, "controlled", {}, io.BytesIO(json.dumps(body).encode()))
            with patch("scripts.verify_hosted.build_opener") as opener:
                opener.return_value.open.side_effect = error
                self.assertEqual(data_api_denial(URL, "controlled-public-key", "users"), "BLOCKED")

    def test_exact_permission_denial_is_required_for_anonymous_and_user_probes(self):
        for token, status in ((None, 401), (None, 403), ("controlled-user-token", 403)):
            error = HTTPError(URL, status, "controlled", {}, io.BytesIO(b'{"code":"42501"}'))
            with patch("scripts.verify_hosted.build_opener") as opener:
                opener.return_value.open.side_effect = error
                self.assertEqual(data_api_denial(URL, "controlled-public-key", "users", token), "PASS")
        with patch("scripts.verify_hosted.build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            self.assertEqual(data_api_denial(URL, "controlled-public-key", "users"), "FAIL")
            response.read.assert_not_called()  # No accidental private row extraction.

    def test_sql_target_overrides_are_refused_without_connection(self):
        for url in (
            "postgresql://test:controlled@unapproved.example.invalid/postgres",
            f"postgresql://test:controlled@db.{REFERENCE}.supabase.co/postgres?host=unapproved.example.invalid",
            f"postgresql://test:controlled@db.{REFERENCE}.supabase.co:9999/postgres",
        ):
            with patch("psycopg.connect") as connect, self.assertRaises(Blocked):
                catalog(url, REFERENCE)
            connect.assert_not_called()

    def test_application_matrix_uses_real_disposable_sql_and_controlled_provider_only(self):
        from dormguard.config import Settings

        _, _, identities = configuration(controlled_environment(), REFERENCE, True)

        class ControlledProvider:
            """Explicit local contract double; no Supabase operation."""

            configured = True

            def login(self, email, password):
                identity = next(item for item in identities if item["email"] == email)
                return {
                    "user": {"id": identity["id"], "user_metadata": {"role": "super_admin"}},
                    "access_token": identity["id"],
                    "expires_in": 3600,
                }

            def validate(self, token):
                return {"id": token}

            def logout(self, token):
                pass

        with tempfile.TemporaryDirectory(prefix="dormguard-hosted-guard-test-") as directory:
            settings = Settings(Path(directory), public_url="http://testserver")
            self.assertEqual(application_checks(settings, identities, ControlledProvider()), "PASS")
