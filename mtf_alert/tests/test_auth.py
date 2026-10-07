import unittest

from mtf_alert.auth import parse_request_token


class AuthenticationTests(unittest.TestCase):
    def test_extracts_token_from_redirect_url(self):
        url = "https://example.test/callback?request_token=abc123&status=success"
        self.assertEqual(parse_request_token(url), "abc123")

    def test_accepts_raw_token(self):
        self.assertEqual(parse_request_token("  abc123  "), "abc123")

    def test_empty_input(self):
        self.assertEqual(parse_request_token("  "), "")


if __name__ == "__main__":
    unittest.main()
