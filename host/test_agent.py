import unittest

from agent import _normalize_server_url, _server_websocket_url


class ServerUrlTests(unittest.TestCase):
    def test_normalizes_remote_https_url(self):
        self.assertEqual(
            _normalize_server_url("  https://remote.example/  "),
            "https://remote.example",
        )
        self.assertEqual(
            _server_websocket_url("https://remote.example", "/ws/host"),
            "wss://remote.example/ws/host",
        )

    def test_allows_plain_http_only_for_local_development(self):
        self.assertEqual(
            _normalize_server_url("http://127.0.0.1:8000"),
            "http://127.0.0.1:8000",
        )
        with self.assertRaises(ValueError):
            _normalize_server_url("http://remote.example")

    def test_rejects_urls_that_are_not_server_origins(self):
        for value in (
            "file:///tmp/server",
            "https://user:password@remote.example",
            "https://remote.example/path",
            "https://remote.example/?token=secret",
            "https://remote.example/#section",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _normalize_server_url(value)


if __name__ == "__main__":
    unittest.main()
