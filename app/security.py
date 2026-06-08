import hashlib
import secrets

PBKDF2_ALGORITHM = "sha256"
PBKDF2_ITERATIONS = 600_000
PBKDF2_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Hash a password with a deliberately slow, salted PBKDF2 derivation."""
    salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(PBKDF2_ALGORITHM, password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_{PBKDF2_ALGORITHM}${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verify current PBKDF2 hashes and legacy salted SHA-256 hashes."""
    if password_hash.startswith("pbkdf2_sha256$"):
        try:
            _, iterations_text, salt_hex, digest_hex = password_hash.split("$", 3)
            iterations = int(iterations_text)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(digest_hex)
        except (ValueError, TypeError):
            return False
        if iterations <= 0 or not salt or not expected:
            return False
        actual = hashlib.pbkdf2_hmac(PBKDF2_ALGORITHM, password.encode("utf-8"), salt, iterations)
        return secrets.compare_digest(actual, expected)

    # Backward compatibility: successful logins are upgraded by the caller.
    try:
        salt, digest = password_hash.split("$", 1)
    except (ValueError, AttributeError):
        return False
    legacy_digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return secrets.compare_digest(legacy_digest, digest)


def password_hash_needs_upgrade(password_hash: str) -> bool:
    try:
        scheme, iterations_text, _, _ = password_hash.split("$", 3)
        return scheme != "pbkdf2_sha256" or int(iterations_text) < PBKDF2_ITERATIONS
    except (ValueError, TypeError, AttributeError):
        return True
