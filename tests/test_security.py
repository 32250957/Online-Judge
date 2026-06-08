import hashlib
import unittest

from app.security import PBKDF2_ITERATIONS, hash_password, password_hash_needs_upgrade, verify_password


class PasswordSecurityTests(unittest.TestCase):
    def test_pbkdf2_hash_round_trip_and_unique_salts(self):
        first = hash_password("correct horse battery staple")
        second = hash_password("correct horse battery staple")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith(f"pbkdf2_sha256${PBKDF2_ITERATIONS}$"))
        self.assertTrue(verify_password("correct horse battery staple", first))
        self.assertFalse(verify_password("wrong", first))
        self.assertFalse(password_hash_needs_upgrade(first))

    def test_legacy_hash_verifies_and_requests_upgrade(self):
        salt = "0123456789abcdef"
        password = "legacy password"
        legacy = f"{salt}${hashlib.sha256((salt + password).encode()).hexdigest()}"
        self.assertTrue(verify_password(password, legacy))
        self.assertTrue(password_hash_needs_upgrade(legacy))

    def test_malformed_hashes_fail_closed(self):
        for malformed in ("", "invalid", "pbkdf2_sha256$bad$salt$digest", "pbkdf2_sha256$0$00$00"):
            self.assertFalse(verify_password("password", malformed))


if __name__ == "__main__":
    unittest.main()
