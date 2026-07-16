import unittest

from src.server import validation_payload


class ServerTests(unittest.TestCase):
    def test_validation_payload_contains_error(self) -> None:
        self.assertEqual(validation_payload("Invalid branch")["status"], "error")


if __name__ == "__main__":
    unittest.main()
