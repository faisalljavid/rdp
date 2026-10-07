"""
Configuration and security helpers for Wayland Remote Desktop.
"""

import os
import sys
import ssl
import json
import secrets
import getpass
import hashlib
import argparse
import datetime
from pathlib import Path

# Base configuration directory in user home
CONFIG_DIR = Path.home() / ".config" / "rdp"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

RESTORE_TOKEN_FILE = CONFIG_DIR / "restore_token"
CERT_FILE = CONFIG_DIR / "cert.pem"
KEY_FILE = CONFIG_DIR / "key.pem"
TOKEN_FILE = CONFIG_DIR / "token"
AUTH_FILE = CONFIG_DIR / "auth.json"


def get_saved_restore_token() -> str | None:
    """Read saved portal restore token if it exists."""
    if RESTORE_TOKEN_FILE.exists():
        try:
            token = RESTORE_TOKEN_FILE.read_text().strip()
            if token:
                return token
        except Exception:
            pass
    return None


def save_restore_token(token: str) -> None:
    """Save portal restore token to disk."""
    try:
        RESTORE_TOKEN_FILE.write_text(token.strip())
    except Exception as e:
        print(f"[config] Warning: Failed to save restore token: {e}", file=sys.stderr)


def get_or_create_auth_token(cli_token: str | None = None) -> str:
    """Get or generate connection auth token."""
    if cli_token:
        TOKEN_FILE.write_text(cli_token.strip())
        return cli_token.strip()

    if "RDP_TOKEN" in os.environ and os.environ["RDP_TOKEN"].strip():
        token = os.environ["RDP_TOKEN"].strip()
        TOKEN_FILE.write_text(token)
        return token

    if TOKEN_FILE.exists():
        try:
            token = TOKEN_FILE.read_text().strip()
            if token:
                return token
        except Exception:
            pass

    # Generate a new random token
    new_token = secrets.token_urlsafe(16)
    try:
        TOKEN_FILE.write_text(new_token)
    except Exception:
        pass
    return new_token


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """Hashes a password with scrypt and a cryptographic salt."""
    if salt is None:
        salt = secrets.token_bytes(16)
    pw_hash = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
    return salt.hex(), pw_hash.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """Verifies a password against the stored scrypt salt and hash in constant time."""
    try:
        salt = bytes.fromhex(salt_hex)
        pw_hash = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
        return secrets.compare_digest(pw_hash.hex(), hash_hex)
    except Exception:
        return False


def save_credentials(username: str, password: str) -> dict:
    """Saves user credentials with salted scrypt hash to auth.json."""
    salt_hex, hash_hex = hash_password(password)
    creds = {
        "username": username.strip(),
        "salt": salt_hex,
        "hash": hash_hex,
    }
    AUTH_FILE.write_text(json.dumps(creds, indent=2))
    AUTH_FILE.chmod(0o600)
    return creds


def load_credentials() -> dict | None:
    """Loads stored credentials from auth.json if present and valid."""
    if AUTH_FILE.exists():
        try:
            data = json.loads(AUTH_FILE.read_text())
            if "username" in data and "salt" in data and "hash" in data:
                return data
        except Exception:
            pass
    return None


def get_or_create_credentials(
    cli_username: str | None = None,
    cli_password: str | None = None,
) -> tuple[dict, bool]:
    """
    Returns (credentials_dict, is_newly_generated).
    If custom username or password provided via CLI, updates auth.json.
    If no auth.json exists, creates default credentials.
    """
    if cli_username and cli_password:
        creds = save_credentials(cli_username, cli_password)
        return creds, False

    if cli_password and not cli_username:
        existing = load_credentials()
        user = existing["username"] if existing else getpass.getuser()
        creds = save_credentials(user, cli_password)
        return creds, False

    existing = load_credentials()
    if existing:
        return existing, False

    # First-time setup: generate clean default credentials
    default_user = getpass.getuser()
    generated_pw = secrets.token_urlsafe(8)
    creds = save_credentials(default_user, generated_pw)
    creds["initial_password"] = generated_pw
    return creds, True


def ensure_self_signed_cert() -> tuple[Path, Path]:
    """Generate a self-signed TLS certificate if not already present."""
    if CERT_FILE.exists() and KEY_FILE.exists():
        return CERT_FILE, KEY_FILE

    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "fedora-rdp")
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )

    KEY_FILE.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    # Restrict key permissions
    KEY_FILE.chmod(0o600)
    return CERT_FILE, KEY_FILE


def create_ssl_context(no_ssl: bool = False) -> ssl.SSLContext | None:
    """Create SSL context for HTTPS/WSS server."""
    if no_ssl:
        return None

    cert_path, key_path = ensure_self_signed_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx


def parse_args():
    parser = argparse.ArgumentParser(
        description="Minimal Wayland Remote Desktop for Fedora GNOME"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host address to bind to (default: 0.0.0.0 / all LAN interfaces)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8443,
        help="Port to listen on (default: 8443)",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="Custom authentication token or password (default: saved or random token)",
    )
    parser.add_argument(
        "--no-ssl",
        action="store_true",
        help="Disable HTTPS/WSS (run plain HTTP/WS, ideal for Tailscale)",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=30,
        help="Target framerate (default: 30 fps)",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=70,
        help="JPEG compression quality (1-100, default: 70)",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="Video scale factor (e.g. 0.75, default: 1.0 = native logical size)",
    )
    parser.add_argument(
        "--reset-portal",
        action="store_true",
        help="Clear saved portal restore token to force re-selection of screen",
    )
    parser.add_argument(
        "--username",
        type=str,
        default=None,
        help="Set or update web login username",
    )
    parser.add_argument(
        "--password",
        type=str,
        default=None,
        help="Set or update web login password",
    )
    parser.add_argument(
        "--set-password",
        action="store_true",
        help="Interactively set web login username and password",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run in mock sandbox mode (videotestsrc test pattern, simulated portal, no real desktop capture/input)",
    )
    return parser.parse_args()
