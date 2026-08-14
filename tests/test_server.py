import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("about_server", ROOT / "app" / "server.py")
about_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(about_server)


class MonitorTests(unittest.TestCase):
    def test_load_toml_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "[sub2api]\nbase_url = 'http://sub2api.internal'\n"
                "main_account_id = '123'\n",
                encoding="utf-8",
            )
            config = about_server._load_config(path)
            self.assertEqual(config["sub2api"]["base_url"], "http://sub2api.internal")
            self.assertEqual(config["sub2api"]["main_account_id"], "123")

    def test_source_url_uses_base_url(self):
        old = about_server.SUB2API_BASE_URL
        try:
            about_server.SUB2API_BASE_URL = "https://sub2api.example.test"
            self.assertEqual(
                about_server.source_url("MISSING_URL", "/api/usage"),
                "https://sub2api.example.test/api/usage",
            )
        finally:
            about_server.SUB2API_BASE_URL = old

    def test_amount_rate_ignores_non_positive_changes(self):
        rows = [
            {"generated_at": "2026-01-01T00:00:00Z", "main": {"amount": 10}},
            {"generated_at": "2026-01-01T00:01:00Z", "main": {"amount": 12}},
            {"generated_at": "2026-01-01T00:02:00Z", "main": {"amount": 11}},
        ]
        rates = about_server._amount_rate_samples(rows)
        self.assertTrue(rates)
        self.assertTrue(all(rate > 0 for rate in rates))

    def test_bark_change_is_deduplicated(self):
        old_endpoint = about_server._bark_endpoint
        old_sender = about_server._send_bark
        try:
            sent = []
            about_server._bark_endpoint = lambda: "https://example.test/device"
            about_server._send_bark = lambda title, body: sent.append((title, body)) or True
            about_server.bark_previous_fingerprints = None
            about_server.bark_pending_notifications = {}
            cycle = {
                "label": "额度周期 1",
                "started_at": "2026-01-01T00:00:00Z",
                "change_markers": [],
            }
            about_server.notify_slope_changes([cycle])
            cycle["change_markers"] = [
                {
                    "generated_at": "2026-01-01T01:00:00Z",
                    "percent": 20,
                    "amount": 100,
                    "relative_change": 0.2,
                    "slope_before": 10,
                    "slope_after": 12,
                }
            ]
            about_server.notify_slope_changes([cycle])
            about_server.notify_slope_changes([cycle])
            self.assertEqual(len(sent), 1)
        finally:
            about_server._bark_endpoint = old_endpoint
            about_server._send_bark = old_sender


if __name__ == "__main__":
    unittest.main()
