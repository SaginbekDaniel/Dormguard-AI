"""Server-side trust, passwords and private source resolution."""

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

OFFICIAL_ROLES = frozenset({"director", "deputy_director", "super_admin"})
CAMERA_MANAGER_ROLES = frozenset({"director", "super_admin"})
ROLE_ALIASES = {"dorm_director": "director", "education_deputy": "deputy_director"}
SOURCE_REF = re.compile(r"DORMGUARD_CAMERA_[A-Z0-9_]{1,64}\Z")


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password: str) -> str:
    if not 14 <= len(password) <= 256:
        raise ValueError("Password must contain 14 to 256 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    # Missing-user work uses the same parameters; neither role nor identity comes from the browser.
    try:
        scheme, salt, digest = (encoded or "scrypt$" + "00" * 16 + "$" + "00" * 64).split("$")
        if scheme != "scrypt" or len(password) > 256:
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return bool(encoded) and hmac.compare_digest(actual.hex(), digest)
    except (ValueError, TypeError):
        return False


def resolve_source(settings, camera) -> str:
    ref = camera.get("source_ref", "")
    if not SOURCE_REF.fullmatch(ref):
        raise ValueError("Camera source reference is invalid")
    source = os.environ.get(ref, "")
    if not source:
        raise ValueError("Camera source is not configured on this server")
    if camera["kind"] == "test":
        root = (settings.data_dir / "test_sources").resolve()
        candidate = Path(source).expanduser().resolve()
        if (
            not candidate.is_relative_to(root)
            or not candidate.is_file()
            or candidate.suffix.lower() not in {".mp4", ".avi", ".mov"}
        ):
            raise ValueError("Test source must be a video file inside the private test_sources directory")
        return str(candidate)
    try:
        parsed = urlsplit(source)
        if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname or parsed.fragment:
            raise ValueError
        address = ipaddress.ip_address(parsed.hostname)
        networks = settings.camera_networks
        if isinstance(networks, str):
            networks = networks.split(",")
        permitted = [ipaddress.ip_network(item.strip(), strict=False) for item in networks if item.strip()]
        if (
            address.is_multicast
            or address.is_unspecified
            or address.is_link_local
            or not any(address in net for net in permitted)
        ):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("RTSP source must use a literal IP inside the configured camera networks") from None
    return source


def private_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Private media path is outside the data volume")
    return path
