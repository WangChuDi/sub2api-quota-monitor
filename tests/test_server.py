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

    def test_validate_accepts_legacy_endpoint_urls(self):
        old_base = about_server.SUB2API_BASE_URL
        old_sources = about_server.SOURCES
        old_key_reader = about_server.read_sub2api_admin_key
        try:
            about_server.SUB2API_BASE_URL = ""
            about_server.SOURCES = {name: "https://sub2api.example.test/" + name for name in old_sources}
            about_server.read_sub2api_admin_key = lambda: "configured"
            about_server.validate_runtime_config()
        finally:
            about_server.SUB2API_BASE_URL = old_base
            about_server.SOURCES = old_sources
            about_server.read_sub2api_admin_key = old_key_reader

    def test_amount_rate_ignores_non_positive_changes(self):
        rows = [
            {"generated_at": "2026-01-01T00:00:00Z", "main": {"amount": 10}},
            {"generated_at": "2026-01-01T00:01:00Z", "main": {"amount": 12}},
            {"generated_at": "2026-01-01T00:02:00Z", "main": {"amount": 11}},
        ]
        rates = about_server._amount_rate_samples(rows)
        self.assertTrue(rates)
        self.assertTrue(all(rate > 0 for rate in rates))

    def test_smoothing_keeps_boundary_values(self):
        series = [
            {"x": 0, "y": 1},
            {"x": 1, "y": 3},
            {"x": 2, "y": 5},
            {"x": 3, "y": 7},
        ]
        smoothed = about_server._smooth_regression_series(series)
        self.assertEqual(smoothed[0]["y"], 1)
        self.assertEqual(smoothed[-1]["y"], 7)

    def test_latest_platform_summary_uses_last_two_buckets(self):
        summary = about_server._latest_platform_summary(
            [
                {"x": 86, "y": 700, "amount_min": 699, "amount_max": 701, "generated_at": "2026-01-01T00:00:00Z"},
                {"x": 88, "y": 720, "amount_min": 718, "amount_max": 722, "generated_at": "2026-01-01T00:05:00Z"},
                {"x": 89, "y": 735, "amount_min": 733, "amount_max": 737, "generated_at": "2026-01-01T00:10:00Z"},
            ]
        )
        self.assertEqual([item["percent"] for item in summary["latest_platforms"]], [88, 89])
        self.assertEqual(summary["latest_platforms"][0]["amount_min"], 718)
        self.assertEqual(summary["latest_platforms"][1]["amount_max"], 737)
        self.assertEqual(summary["latest_platforms"][0]["amount_used"], 4)
        self.assertEqual(summary["latest_platforms"][1]["amount_used"], 4)
        self.assertEqual(summary["latest_platform_delta_amount"], 15)
        self.assertEqual(summary["latest_platform_delta_percent"], 1)
        self.assertEqual(summary["latest_platform_amount_per_percent"], 15)

    def test_regression_series_keeps_platform_range(self):
        series = about_server._regression_series(
            [
                {"generated_at": "2026-01-01T00:00:00Z", "main": {"quota_percent": 88, "amount": 801}},
                {"generated_at": "2026-01-01T00:01:00Z", "main": {"quota_percent": 88, "amount": 803}},
            ]
        )
        self.assertEqual(series[0]["amount_min"], 801)
        self.assertEqual(series[0]["amount_max"], 803)
        self.assertEqual(series[0]["sample_count"], 2)

    def test_coalesce_change_points_merges_small_local_slope_change(self):
        series = [
            {"x": index, "y": (index if index < 5 else 5) + (index - 5) * (1 if index < 10 else 1.1)}
            for index in range(15)
        ]
        changes = [
            {"index": 5, "split": 5, "relative_change": 1.0, "p_value": 0.01},
            {"index": 10, "split": 10, "relative_change": 0.1, "p_value": 0.02},
        ]
        merged = about_server._coalesce_change_points(series, changes)
        self.assertEqual([item["index"] for item in merged], [5])

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
                },
                {
                    "generated_at": "2026-01-01T02:00:00Z",
                    "percent": 35,
                    "amount": 140,
                    "relative_change": 0.3,
                    "slope_before": 12,
                    "slope_after": 8,
                },
            ]
            cycle["change_marker"] = cycle["change_markers"][-1]
            about_server.notify_slope_changes([cycle])
            about_server.notify_slope_changes([cycle])
            self.assertEqual(len(sent), 1)
            self.assertIn("已用 35%", sent[0][1])
            self.assertNotIn("已用 20%", sent[0][1])
        finally:
            about_server._bark_endpoint = old_endpoint
            about_server._send_bark = old_sender


if __name__ == "__main__":
    unittest.main()
