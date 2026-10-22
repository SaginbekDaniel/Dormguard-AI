"""Trusted local provisioning and development commands; never auto-sends invitations."""

import argparse
import getpass
import json
import secrets
import sys

from .config import load_settings
from .security import OFFICIAL_ROLES, ROLE_ALIASES, hash_password
from .store import DomainError, Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize the selected private database")
    provision = commands.add_parser(
        "provision", help="Assign a trusted official; password prompted in local evaluation mode"
    )
    provision.add_argument("--role", choices=sorted(OFFICIAL_ROLES | ROLE_ALIASES.keys()), required=True)
    provision.add_argument("--email", required=True)
    provision.add_argument(
        "--display-name", help="Optional account display name; never used for authorization"
    )
    provision.add_argument(
        "--provider-id", help="Existing Supabase Auth user UUID; provision through Supabase admin first"
    )
    availability = commands.add_parser(
        "account", help="Enable/disable an existing account and revoke its sessions"
    )
    availability.add_argument("--user-id", required=True)
    availability.add_argument("--enabled", choices=("0", "1"), required=True)
    commands.add_parser("users", help="List trusted assignments without secrets")
    commands.add_parser("audit-verify")
    commands.add_parser("serve")
    fixture = commands.add_parser(
        "test-source", help="Generate a clearly labeled no-human transport fixture, not a fight detector demo"
    )
    fixture.add_argument("--seconds", type=int, default=20, choices=range(5, 61), metavar="5..60")
    args = parser.parse_args()
    settings = load_settings()
    if args.command == "serve":
        from .app import main as serve

        serve()
        return
    store = Store(settings.data_dir, settings.database_url)
    if args.command == "init":
        print("Database initialized. No accounts, cameras or incidents created.")
    elif args.command == "provision":
        password_hash = None
        if settings.auth_mode == "supabase":
            if not args.provider_id:
                parser.error(
                    "Supabase mode requires --provider-id from a trusted invitation/provisioning operation"
                )
            import uuid

            try:
                uuid.UUID(args.provider_id)
            except ValueError:
                parser.error("--provider-id must be a UUID")
        else:
            if args.provider_id:
                parser.error("--provider-id is only accepted in Supabase mode")
            password = getpass.getpass("New password (14–256 characters): ")
            if password != getpass.getpass("Repeat new password: "):
                parser.error("Passwords do not match")
            password_hash = hash_password(password)
        user = store.provision_user(args.email, args.role, password_hash, args.provider_id, args.display_name)
        print(json.dumps({key: user[key] for key in ("id", "email", "role")}, indent=2))
    elif args.command == "account":
        with store.db.transaction() as conn:
            if not conn.execute("SELECT id FROM users WHERE id=?", (args.user_id,)).fetchone():
                raise DomainError("Account not found", 404)
            conn.execute("UPDATE users SET enabled=? WHERE id=?", (int(args.enabled), args.user_id))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (args.user_id,))
            conn.execute("DELETE FROM stream_tickets WHERE user_id=?", (args.user_id,))
            store._audit(
                conn,
                None,
                "account.availability",
                args.user_id,
                {"enabled": args.enabled == "1", "source": "trusted_cli"},
            )
        print("Account availability updated; local sessions and preview tickets revoked.")
    elif args.command == "users":
        print(json.dumps(store.list_users(), indent=2))
    elif args.command == "audit-verify":
        result = store.verify_audit()
        print(json.dumps(result, indent=2))
        if not result["valid"]:
            raise SystemExit(1)
    elif args.command == "test-source":
        import cv2
        import numpy as np

        from .playback import encode_clip

        root = settings.data_dir / "test_sources"
        root.mkdir(exist_ok=True, mode=0o700)
        target = root / ("transport-test-" + secrets.token_hex(4) + ".mp4")
        frames = []
        fps = 4
        for index in range(args.seconds * fps):
            image = np.zeros((360, 640, 3), dtype=np.uint8)
            image[:] = (36, 31, 22)
            cv2.rectangle(image, (30 + index % 450, 165), (100 + index % 450, 235), (120, 170, 50), -1)
            cv2.putText(
                image,
                "SIMULATED CAMERA - NO PEOPLE",
                (32, 70),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (230, 230, 230),
                2,
            )
            cv2.putText(
                image,
                f"Transport fixture {index / fps:.2f}s",
                (32, 110),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (180, 180, 180),
                1,
            )
            frames.append(image)
        encode_clip(frames, fps, target)
        print("Generated no-human camera transport fixture:")
        print(target)
        print(
            "Set DORMGUARD_CAMERA_TEST to this private path; register kind=test and source_ref=DORMGUARD_CAMERA_TEST."
        )
        print("This fixture does not establish fight-detection quality or a positive incident demonstration.")


if __name__ == "__main__":
    try:
        main()
    except (DomainError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
