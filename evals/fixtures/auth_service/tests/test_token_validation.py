import unittest

from src.token_validation import login_user, validate_token


class TokenValidationTests(unittest.TestCase):
    def test_invalid_token_is_denied(self) -> None:
        self.assertFalse(validate_token({"active": True, "expired": True}))

    def test_active_token_can_login(self) -> None:
        self.assertTrue(login_user("alex", {"active": True, "expired": False}))


if __name__ == "__main__":
    unittest.main()
