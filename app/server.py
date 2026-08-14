#!/usr/bin/env python3
import json
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit


if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    BUNDLE_DIR = Path(sys._MEIPASS)
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else BUNDLE_DIR
HTML_DIR = Path(os.getenv("HTML_DIR", str(BUNDLE_DIR / "html")))
DATA_DIR = Path(os.getenv("DATA_DIR", str(RUNTIME_DIR / "data")))
CACHE_FILE = DATA_DIR / "status.json"
HISTORY_DB = Path(os.getenv("HISTORY_DB", str(DATA_DIR / "history.db")))
HISTORY_RETENTION_DAYS = max(1, int(os.getenv("HISTORY_RETENTION_DAYS", "90")))
PORT = int(os.getenv("PORT", "80"))
SUB2API_BASE_URL = os.getenv("SUB2API_BASE_URL", "").rstrip("/")
MAIN_ACCOUNT_ID = os.getenv("MAIN_ACCOUNT_ID", "571")
SPARK_ACCOUNT_ID = os.getenv("SPARK_ACCOUNT_ID", "576")
SUB2API_ADMIN_KEY = os.getenv("SUB2API_ADMIN_KEY", "").strip()
FAST_INTERVAL_SECONDS = max(5, int(os.getenv("FAST_INTERVAL_SECONDS", "60")))
NORMAL_INTERVAL_SECONDS = max(
    5, int(os.getenv("NORMAL_INTERVAL_SECONDS", os.getenv("INTERVAL_SECONDS", "300")))
)
FAST_USAGE_AMOUNT_THRESHOLD = max(
    0.0, float(os.getenv("FAST_USAGE_AMOUNT_THRESHOLD", "5"))
)
FAST_USAGE_REQUEST_THRESHOLD = max(
    0, int(os.getenv("FAST_USAGE_REQUEST_THRESHOLD", "20"))
)
FAST_HOLD_SECONDS = max(
    NORMAL_INTERVAL_SECONDS, int(os.getenv("FAST_HOLD_SECONDS", "900"))
)
REQUEST_TIMEOUT = max(5, int(os.getenv("REQUEST_TIMEOUT", "45")))
BARK_ENABLED = os.getenv("BARK_ENABLED", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
BARK_URL = os.getenv("BARK_URL", "").strip()
BARK_URL_FILE = Path(os.getenv("BARK_URL_FILE", "/run/secrets/bark_url"))
BARK_REQUEST_TIMEOUT = max(3, int(os.getenv("BARK_REQUEST_TIMEOUT", "10")))
FRAME_ANCESTORS = os.getenv("FRAME_ANCESTORS", "'self'")
SUB2API_ADMIN_KEY_FILE = Path(
    os.getenv("SUB2API_ADMIN_KEY_FILE", "/run/secrets/sub2api_admin_key")
)


def source_url(env_name, suffix):
    configured = os.getenv(env_name, "").strip()
    return configured or (f"{SUB2API_BASE_URL}{suffix}" if SUB2API_BASE_URL else "")


SOURCES = {
    "quota": source_url(
        "QUOTA_URL",
        f"/api/v1/admin/openai/accounts/{MAIN_ACCOUNT_ID}/quota?timezone=Asia%2FShanghai",
    ),
    "usage": source_url(
        "USAGE_URL",
        f"/api/v1/admin/accounts/{MAIN_ACCOUNT_ID}/usage?source=active&force=true&timezone=Asia%2FShanghai",
    ),
    "spark_quota": source_url(
        "SPARK_QUOTA_URL",
        f"/api/v1/admin/openai/accounts/{SPARK_ACCOUNT_ID}/quota?timezone=Asia%2FShanghai",
    ),
    "spark_usage": source_url(
        "SPARK_USAGE_URL",
        f"/api/v1/admin/accounts/{SPARK_ACCOUNT_ID}/usage?source=active&force=true&timezone=Asia%2FShanghai",
    ),
}

state_lock = threading.Lock()
collect_lock = threading.Lock()
state = {}
adaptive_fast_until = 0.0
bark_previous_fingerprints = None
bark_pending_notifications = {}


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat().replace("+00:00", "Z")


def sampling_interval_seconds():
    return FAST_INTERVAL_SECONDS if time.monotonic() < adaptive_fast_until else NORMAL_INTERVAL_SECONDS


def sampling_policy():
    fast = time.monotonic() < adaptive_fast_until
    return {
        "mode": "fast" if fast else "normal",
        "current_interval_seconds": FAST_INTERVAL_SECONDS if fast else NORMAL_INTERVAL_SECONDS,
        "fast_interval_seconds": FAST_INTERVAL_SECONDS,
        "normal_interval_seconds": NORMAL_INTERVAL_SECONDS,
        "usage_amount_threshold": FAST_USAGE_AMOUNT_THRESHOLD,
        "usage_request_threshold": FAST_USAGE_REQUEST_THRESHOLD,
        "fast_hold_seconds": FAST_HOLD_SECONDS,
    }


def unwrap(payload):
    if not isinstance(payload, dict):
        return {}
    if payload.get("data") is not None:
        return payload["data"]
    if payload.get("result") is not None:
        return payload["result"]
    return payload


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def integer(value):
    value = number(value)
    return int(value) if value is not None else None


def parse_time(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 20_000_000_000:
            seconds /= 1000
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def normalize_stats(value):
    if not isinstance(value, dict):
        return {}
    stats = value.get("window_stats") or value.get("stats") or value.get("usage") or value
    if not isinstance(stats, dict):
        return {}
    aliases = {
        "requests": ("requests", "request_count", "requestCount"),
        "tokens": ("tokens", "total_tokens", "totalTokens", "token_count"),
        "cost": ("cost", "amount", "total_cost", "totalCost"),
        "standard_cost": ("standard_cost", "standardCost", "a_cost", "aCost"),
        "user_cost": ("user_cost", "userCost", "u_cost", "uCost"),
    }
    result = {}
    for output_key, candidates in aliases.items():
        for candidate in candidates:
            parsed = number(stats.get(candidate))
            if parsed is not None:
                result[output_key] = int(parsed) if output_key in ("requests", "tokens") else parsed
                break
    return result


def normalize_window(value, key, label):
    if not isinstance(value, dict):
        return None
    utilization = number(value.get("used_percent", value.get("utilization")))
    remaining = integer(value.get("reset_after_seconds", value.get("remaining_seconds")))
    resets_at = value.get("reset_at") or value.get("resets_at") or value.get("resetAt")
    reset_time = parse_time(resets_at)
    reset_is_future = bool(reset_time and reset_time > utc_now())
    has_active_limit = bool((remaining is not None and remaining > 0) or reset_is_future)
    stats = normalize_stats(value)
    if utilization is None and remaining is None and resets_at is None and not stats:
        return None
    return {
        "key": key,
        "label": label,
        "utilization": utilization,
        "remaining_seconds": max(0, remaining) if remaining is not None else None,
        "resets_at": resets_at,
        "has_active_limit": has_active_limit,
        "stats": stats,
    }


def normalize_usage(payload):
    root = unwrap(payload)
    if not isinstance(root, dict):
        return {"updated_at": None, "windows": []}
    labels = {
        "five_hour": "近 5 小时",
        "seven_day": "7 天",
        "seven_day_sonnet": "7 天 Sonnet",
        "seven_day_fable": "7 天 Fable",
    }
    windows = []
    for key, label in labels.items():
        normalized = normalize_window(root.get(key), key, label)
        if normalized:
            windows.append(normalized)
    return {
        "updated_at": root.get("updated_at") or root.get("fetched_at"),
        "source": root.get("source"),
        "windows": windows,
    }


def quota_window(value, key, label):
    normalized = normalize_window(value, key, label)
    if normalized:
        normalized["has_active_limit"] = True
    return normalized


def normalize_quota(payload):
    root = unwrap(payload)
    if not isinstance(root, dict):
        return {"fetched_at": None, "plan_type": None, "groups": [], "reset_credits": None}

    groups = []

    def add_group(name, rate_limit):
        if not isinstance(rate_limit, dict):
            return
        windows = []
        primary = quota_window(rate_limit.get("primary_window"), "primary_window", "短周期")
        secondary = quota_window(rate_limit.get("secondary_window"), "secondary_window", "长周期")
        if primary:
            windows.append(primary)
        if secondary:
            windows.append(secondary)
        if windows:
            groups.append(
                {
                    "name": name,
                    "allowed": rate_limit.get("allowed"),
                    "limit_reached": rate_limit.get("limit_reached"),
                    "windows": windows,
                }
            )

    add_group("主额度", root.get("rate_limit"))
    extras = root.get("additional_rate_limits")
    if isinstance(extras, list):
        for item in extras:
            if not isinstance(item, dict):
                continue
            name = item.get("limit_name") or item.get("metered_feature") or "附加额度"
            add_group(str(name)[:80], item.get("rate_limit"))

    credits = None
    reset_credits = root.get("rate_limit_reset_credits") or root.get("reset_credits")
    if isinstance(reset_credits, dict):
        credits = integer(reset_credits.get("available_count"))

    return {
        "fetched_at": root.get("fetched_at") or root.get("updated_at"),
        "plan_type": str(root.get("plan_type"))[:40] if root.get("plan_type") else None,
        "groups": groups,
        "reset_credits": credits,
    }


NORMALIZERS = {
    "quota": normalize_quota,
    "usage": normalize_usage,
    "spark_quota": normalize_quota,
    "spark_usage": normalize_usage,
}


def read_sub2api_admin_key():
    if SUB2API_ADMIN_KEY:
        return SUB2API_ADMIN_KEY
    try:
        key = SUB2API_ADMIN_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("sub2api admin key secret is unavailable") from exc
    if not key:
        raise RuntimeError("sub2api admin key secret is empty")
    return key


def fetch_json(name, url):
    headers = {
        "Accept": "application/json",
        "User-Agent": "newapi-about-monitor/2.1",
    }
    headers["x-api-key"] = read_sub2api_admin_key()
    request = urllib.request.Request(
        url,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            body = response.read(2_000_000)
            status = response.status
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(str(exc.reason if hasattr(exc, "reason") else exc)) from exc
    try:
        return status, json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("upstream did not return valid JSON") from exc


def source_result(name, previous):
    checked_at = iso_now()
    try:
        http_status, payload = fetch_json(name, SOURCES[name])
        return {
            "ok": True,
            "stale": False,
            "http_status": http_status,
            "checked_at": checked_at,
            "last_success_at": checked_at,
            "error": None,
            "data": NORMALIZERS[name](payload),
        }
    except Exception as exc:
        old_data = previous.get("data") if isinstance(previous, dict) else None
        return {
            "ok": False,
            "stale": old_data is not None,
            "http_status": None,
            "checked_at": checked_at,
            "last_success_at": previous.get("last_success_at") if isinstance(previous, dict) else None,
            "error": str(exc)[:300],
            "data": old_data,
        }


def main_usage_point(sources):
    """Extract cumulative main-account usage used by the sampler."""
    usage_data = ((sources or {}).get("usage") or {}).get("data") or {}
    usage_windows = usage_data.get("windows") or []
    usage_window = next(
        (item for item in usage_windows if item.get("key") == "seven_day"),
        usage_windows[0] if usage_windows else {},
    )
    stats = usage_window.get("stats") or {}
    return {
        "amount": number(stats.get("cost")),
        "requests": number(stats.get("requests")),
    }


def _relative_change(previous, current):
    if previous is None or current is None:
        return None
    baseline = max(abs(previous), 1.0)
    return abs(current - previous) / baseline


def large_usage_change(previous_sources, current_sources, previous_interval_seconds):
    previous = main_usage_point(previous_sources)
    current = main_usage_point(current_sources)
    previous_amount = previous.get("amount")
    current_amount = current.get("amount")
    previous_requests = previous.get("requests")
    current_requests = current.get("requests")
    amount_delta = (
        current_amount - previous_amount
        if previous_amount is not None and current_amount is not None
        else None
    )
    request_delta = (
        current_requests - previous_requests
        if previous_requests is not None and current_requests is not None
        else None
    )
    # Compare equivalent five-minute usage so the trigger remains stable while
    # the collector is already running in its one-minute fast mode.
    scale = NORMAL_INTERVAL_SECONDS / max(previous_interval_seconds, 1)
    scaled_amount = amount_delta * scale if amount_delta is not None else None
    scaled_requests = request_delta * scale if request_delta is not None else None
    return bool(
        (scaled_amount is not None and scaled_amount >= FAST_USAGE_AMOUNT_THRESHOLD)
        or (scaled_requests is not None and scaled_requests >= FAST_USAGE_REQUEST_THRESHOLD)
    )


def persist(snapshot):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(CACHE_FILE)
    with sqlite3.connect(HISTORY_DB, timeout=30) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generated_at TEXT NOT NULL,
                collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                snapshot_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_generated_at ON snapshots(generated_at)"
        )
        connection.execute(
            "INSERT INTO snapshots (generated_at, snapshot_json) VALUES (?, ?)",
            (snapshot["generated_at"], json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))),
        )
        connection.execute(
            "DELETE FROM snapshots WHERE generated_at < datetime('now', ?)",
            (f"-{HISTORY_RETENTION_DAYS} days",),
        )


def history_points(hours):
    hours = max(1, min(24 * 90, int(hours)))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(HISTORY_DB, timeout=10) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generated_at TEXT NOT NULL,
                collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                snapshot_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_generated_at ON snapshots(generated_at)"
        )
        rows = connection.execute(
            """
            SELECT generated_at, snapshot_json
            FROM snapshots
            WHERE julianday(generated_at) >= julianday('now', ?)
            ORDER BY julianday(generated_at) ASC
            """,
            (f"-{hours} hours",),
        ).fetchall()
    if not rows:
        try:
            cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            cached_at = cached.get("generated_at") if isinstance(cached, dict) else None
            if cached_at:
                rows = [(cached_at, json.dumps(cached, ensure_ascii=False))]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
    points = []
    for generated_at, raw in rows:
        try:
            snapshot = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        point = {"generated_at": generated_at}
        for prefix, quota_name, usage_name, group_match in (
            ("main", "quota", "usage", "main"),
            ("spark", "spark_quota", "spark_usage", "spark"),
        ):
            quota_data = ((snapshot.get("sources") or {}).get(quota_name) or {}).get("data") or {}
            groups = quota_data.get("groups") or []
            if group_match == "main":
                quota_group = next((group for group in groups if group.get("name") == "主额度"), None)
            else:
                quota_group = next((group for group in groups if "spark" in str(group.get("name", "")).lower()), None)
            quota_windows = (quota_group or {}).get("windows") or []
            quota_window = next((item for item in quota_windows if item.get("key") == "primary_window"), quota_windows[0] if quota_windows else {})
            usage_data = ((snapshot.get("sources") or {}).get(usage_name) or {}).get("data") or {}
            usage_windows = usage_data.get("windows") or []
            usage_window = next((item for item in usage_windows if item.get("key") == "seven_day"), usage_windows[0] if usage_windows else {})
            stats = usage_window.get("stats") or {}
            point[prefix] = {
                "quota_percent": quota_window.get("utilization"),
                "amount": stats.get("cost"),
                "standard_amount": stats.get("standard_cost"),
                "user_amount": stats.get("user_cost"),
                "resets_at": quota_window.get("resets_at"),
            }
        points.append(point)
    return points


def _main_value(point, key):
    return number((point.get("main") or {}).get(key))


def _looks_like_reset(previous, current):
    previous_amount = _main_value(previous, "amount")
    current_amount = _main_value(current, "amount")
    previous_percent = _main_value(previous, "quota_percent")
    current_percent = _main_value(current, "quota_percent")
    amount_drop = (
        previous_amount is not None
        and current_amount is not None
        and previous_amount >= 1
        and current_amount <= max(1, previous_amount * 0.1)
    )
    percent_drop = (
        previous_percent is not None
        and current_percent is not None
        and previous_percent >= 10
        and current_percent <= 5
    )
    return amount_drop or percent_drop


def _iso_datetime(value):
    parsed = parse_time(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def _median(values):
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


def _quantile(values, fraction):
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(len(values) - 1, lower + 1)
    weight = position - lower
    return values[lower] + (values[upper] - values[lower]) * weight


def _regression_series(rows):
    """Collapse repeated rounded percentages into ordered regression observations."""
    series = []
    bucket = []
    bucket_percent = None
    bucket_row = None

    def flush():
        if not bucket or bucket_percent is None:
            return
        series.append(
            {
                "x": bucket_percent,
                "y": _median(bucket),
                "generated_at": bucket_row.get("generated_at") if bucket_row else None,
            }
        )

    for row in rows:
        percent = _main_value(row, "quota_percent")
        amount = _main_value(row, "amount")
        if percent is None or amount is None:
            continue
        if bucket_percent is not None and percent != bucket_percent:
            flush()
            bucket = []
        bucket_percent = percent
        bucket.append(amount)
        bucket_row = row
    flush()
    return [item for item in series if item["y"] is not None]


def _smooth_regression_series(series, window=3):
    """Smooth amount across adjacent integer-percent levels before fitting."""
    if len(series) < 3:
        return list(series)
    half = window // 2
    smoothed = []
    for index, item in enumerate(series):
        start = max(0, index - half)
        end = min(len(series), index + half + 1)
        neighborhood = [row["y"] for row in series[start:end]]
        smoothed.append(
            {
                "x": item["x"],
                "y": _median(neighborhood),
                "raw_y": item["y"],
                "generated_at": item.get("generated_at"),
            }
        )
    return smoothed


def _regression_fit(series):
    if len(series) < 2:
        return None
    n = len(series)
    x_mean = sum(item["x"] for item in series) / n
    y_mean = sum(item["y"] for item in series) / n
    sxx = sum((item["x"] - x_mean) ** 2 for item in series)
    syy = sum((item["y"] - y_mean) ** 2 for item in series)
    sxy = sum((item["x"] - x_mean) * (item["y"] - y_mean) for item in series)
    if sxx <= 0:
        return None
    slope = sxy / sxx
    intercept = y_mean - slope * x_mean
    residuals = [item["y"] - (intercept + slope * item["x"]) for item in series]
    sse = sum(value * value for value in residuals)
    degrees = n - 2
    mse = sse / degrees if degrees > 0 else None
    slope_se = math.sqrt(mse / sxx) if mse is not None else None
    r2 = 1 - sse / syy if syy > 0 else 1.0
    return {
        "n": n,
        "slope": slope,
        "intercept": intercept,
        "slope_se": slope_se,
        "r2": max(0.0, min(1.0, r2)),
        "rmse": math.sqrt(sse / n) if n else None,
    }


def _normal_two_sided_p(z_value):
    if z_value is None:
        return None
    return math.erfc(abs(z_value) / math.sqrt(2.0))


def _best_change_point(series, relative_threshold=0.10):
    """Find one statistically significant slope shift in amount vs. percent."""
    minimum_points = 5
    if len(series) < minimum_points * 2:
        return None
    best = None
    comparisons = len(series) - minimum_points * 2 + 1
    for split in range(minimum_points, len(series) - minimum_points + 1):
        left = _regression_fit(series[:split])
        right = _regression_fit(series[split:])
        if not left or not right:
            continue
        difference = abs(right["slope"] - left["slope"])
        scale = max(abs(left["slope"]), abs(right["slope"]), 1e-9)
        relative_change = difference / scale
        if left["slope_se"] is None or right["slope_se"] is None:
            continue
        standard_error = math.sqrt(left["slope_se"] ** 2 + right["slope_se"] ** 2)
        z_value = difference / standard_error if standard_error > 0 else None
        raw_p = _normal_two_sided_p(z_value)
        adjusted_p = min(1.0, raw_p * comparisons) if raw_p is not None else None
        candidate = {
            "split": split,
            "left": left,
            "right": right,
            "relative_change": relative_change,
            "z": z_value,
            "p_value": adjusted_p,
        }
        if best is None or (candidate["z"] or 0) > (best["z"] or 0):
            best = candidate
    if not best:
        return None
    if best["relative_change"] < relative_threshold or best["p_value"] is None or best["p_value"] > 0.05:
        return None
    return best


def _detect_change_points(series, max_changes=4):
    """Recursively find multiple significant slope shifts."""
    changes = []

    def walk(segment, offset):
        if len(changes) >= max_changes:
            return
        candidate = _best_change_point(segment)
        if not candidate:
            return
        split = candidate["split"]
        candidate["index"] = offset + split
        changes.append(candidate)
        walk(segment[:split], offset)
        walk(segment[split:], offset + split)

    walk(series, 0)
    return sorted(changes, key=lambda item: item["index"])


def _fit_line(fit, series):
    if not fit or len(series) < 2:
        return []
    x_values = [item["x"] for item in series]
    start_x = min(x_values)
    end_x = max(x_values)
    return [
        {
            "amount": fit["intercept"] + fit["slope"] * start_x,
            "percent": start_x,
            "slope": fit["slope"],
        },
        {
            "amount": fit["intercept"] + fit["slope"] * end_x,
            "percent": end_x,
            "slope": fit["slope"],
        },
    ]


def _percent_rate_samples(rows, recent_only=False):
    samples = []
    if len(rows) < 2:
        return samples
    recent_start = max(0, len(rows) - 8)
    max_span = min(8, len(rows) - 1)
    for span in range(1, max_span + 1):
        for start in range(0, len(rows) - span):
            end = start + span
            if recent_only and end < recent_start:
                continue
            start_time = parse_time(rows[start].get("generated_at"))
            end_time = parse_time(rows[end].get("generated_at"))
            start_percent = _main_value(rows[start], "quota_percent")
            end_percent = _main_value(rows[end], "quota_percent")
            if None in (start_time, end_time, start_percent, end_percent):
                continue
            elapsed = (end_time - start_time).total_seconds()
            percent_delta = end_percent - start_percent
            if elapsed <= 0 or percent_delta <= 0:
                continue
            samples.append(percent_delta / elapsed)
    return samples


def _amount_rate_samples(rows, recent_only=False):
    """Return positive usage-amount rates in amount units per second."""
    samples = []
    if len(rows) < 2:
        return samples
    recent_start = max(0, len(rows) - 8)
    max_span = min(8, len(rows) - 1)
    for span in range(1, max_span + 1):
        for start in range(0, len(rows) - span):
            end = start + span
            if recent_only and end < recent_start:
                continue
            start_time = parse_time(rows[start].get("generated_at"))
            end_time = parse_time(rows[end].get("generated_at"))
            start_amount = _main_value(rows[start], "amount")
            end_amount = _main_value(rows[end], "amount")
            if None in (start_time, end_time, start_amount, end_amount):
                continue
            elapsed = (end_time - start_time).total_seconds()
            amount_delta = end_amount - start_amount
            if elapsed <= 0 or amount_delta <= 0:
                continue
            samples.append(amount_delta / elapsed)
    return samples


def _cycle_summary(rows, index, total):
    first = rows[0]
    latest = rows[-1]
    latest_percent = _main_value(latest, "quota_percent")
    latest_amount = _main_value(latest, "amount")
    valid_percent = [
        value for value in (_main_value(row, "quota_percent") for row in rows) if value is not None
    ]
    valid_amount = [
        value for value in (_main_value(row, "amount") for row in rows) if value is not None
    ]
    peak_percent = max(valid_percent) if valid_percent else None
    peak_amount = max(valid_amount) if valid_amount else None
    raw_regression_series = _regression_series(rows)
    regression_series = _smooth_regression_series(raw_regression_series)
    full_fit = _regression_fit(regression_series)
    change_points = _detect_change_points(regression_series)
    selected_series = regression_series
    selected_fit = full_fit
    model_scope = "full_cycle"
    if change_points:
        last_index = change_points[-1]["index"]
        candidate_series = regression_series[last_index:]
        candidate_fit = _regression_fit(candidate_series)
        if candidate_fit and candidate_fit["slope"] > 0:
            selected_series = candidate_series
            selected_fit = candidate_fit
            model_scope = "after_last_change"
    if selected_fit and selected_fit["slope"] <= 0:
        selected_fit = None
    change_detected = bool(change_points)
    change_markers = []
    for point in change_points:
        marker_index = point["index"]
        if marker_index < len(regression_series):
            marker = regression_series[marker_index]
            change_markers.append(
                {
                    "amount": marker["y"],
                    "percent": marker["x"],
                    "generated_at": marker.get("generated_at"),
                    "relative_change": point.get("relative_change"),
                    "p_value": point.get("p_value"),
                    "slope_before": (point.get("left") or {}).get("slope"),
                    "slope_after": (point.get("right") or {}).get("slope"),
                }
            )
    change_marker = change_markers[-1] if change_markers else None
    slope_per_percent = selected_fit["slope"] if selected_fit else None
    slope_se = selected_fit.get("slope_se") if selected_fit else None
    slope_margin = 1.96 * slope_se if slope_se is not None else None
    slope_low = max(0, slope_per_percent - slope_margin) if slope_per_percent is not None and slope_margin is not None else slope_per_percent
    slope_high = slope_per_percent + slope_margin if slope_per_percent is not None and slope_margin is not None else slope_per_percent
    estimated_total_amount = None
    estimated_total_low = None
    estimated_total_high = None
    if latest_amount is not None and latest_percent is not None and slope_per_percent is not None:
        remaining_percent = max(0, 100 - latest_percent)
        estimated_total_amount = latest_amount + slope_per_percent * remaining_percent
        estimated_total_low = latest_amount + (slope_low or slope_per_percent) * remaining_percent
        estimated_total_high = latest_amount + (slope_high or slope_per_percent) * remaining_percent
    elif peak_percent is not None and peak_percent > 0 and peak_amount is not None:
        estimated_total_amount = peak_amount * 100 / peak_percent
        estimated_total_low = estimated_total_amount
        estimated_total_high = estimated_total_amount
    expected_available_amount = estimated_total_amount
    expected_remaining_amount = None
    if estimated_total_amount is not None and latest_amount is not None:
        expected_remaining_amount = max(0, estimated_total_amount - latest_amount)
    expected_available_percent = (
        max(0, 100 - latest_percent) if latest_percent is not None else None
    )

    expected_exhausted_at = None
    latest_time = parse_time(latest.get("generated_at"))
    recent_amount_rates = _amount_rate_samples(rows, recent_only=True)
    all_amount_rates = _amount_rate_samples(rows)
    amount_rate = _median(recent_amount_rates or all_amount_rates)
    if latest_time and expected_remaining_amount is not None and amount_rate and amount_rate > 0:
        expected_seconds = expected_remaining_amount / amount_rate
        expected_exhausted_at = latest_time + timedelta(seconds=max(0, expected_seconds))
    if expected_exhausted_at is None:
        expected_exhausted_at = parse_time((latest.get("main") or {}).get("resets_at"))

    return {
        "index": index,
        "label": f"额度周期 {index + 1}",
        "is_latest": index == total - 1,
        "started_at": first.get("generated_at"),
        "ended_at": latest.get("generated_at"),
        "reset_at": (latest.get("main") or {}).get("resets_at"),
        "current_quota_percent": latest_percent,
        "current_amount": latest_amount,
        "expected_available_percent": expected_available_percent,
        "expected_available_amount": expected_available_amount,
        "expected_remaining_amount": expected_remaining_amount,
        "estimated_total_amount": estimated_total_amount,
        "estimated_total_amount_low": estimated_total_low,
        "estimated_total_amount_high": estimated_total_high,
        "slope_per_percent": slope_per_percent,
        "slope_per_percent_low": slope_low,
        "slope_per_percent_high": slope_high,
        "slope_samples": len(selected_series),
        "regression_points": len(regression_series),
        "raw_regression_points": len(raw_regression_series),
        "regression_r2": selected_fit.get("r2") if selected_fit else None,
        "regression_rmse": selected_fit.get("rmse") if selected_fit else None,
        "regression_scope": model_scope,
        "change_detected": change_detected,
        "change_at": change_marker.get("generated_at") if change_marker else None,
        "change_p_value": change_marker.get("p_value") if change_marker else None,
        "change_relative": change_marker.get("relative_change") if change_marker else None,
        "change_count": len(change_markers),
        "regression_line": _fit_line(selected_fit, selected_series),
        "regression_before_line": _fit_line(change_points[0]["left"], regression_series[: change_points[0]["index"]]) if change_points else [],
        "regression_after_line": _fit_line(change_points[-1]["right"], regression_series[change_points[-1]["index"] :]) if change_points else [],
        "regression_segments": [
            _fit_line(
                _regression_fit(regression_series[start:end]),
                regression_series[start:end],
            )
            for start, end in zip(
                [0] + [point["index"] for point in change_points],
                [point["index"] for point in change_points] + [len(regression_series)],
            )
        ],
        "change_markers": change_markers,
        "change_marker": change_marker,
        "expected_exhausted_at": _iso_datetime(expected_exhausted_at),
        "points": rows,
    }


def history_cycles(points):
    if not points:
        return []
    starts = [0]
    for index in range(1, len(points)):
        if _looks_like_reset(points[index - 1], points[index]):
            starts.append(index)
    cycles = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(points)
        cycles.append(_cycle_summary(points[start:end], index, len(starts)))
    return cycles


def _bark_endpoint():
    if not BARK_ENABLED:
        return None
    if BARK_URL:
        parsed = urlsplit(BARK_URL.rstrip("/"))
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return BARK_URL.rstrip("/")
    try:
        endpoint = BARK_URL_FILE.read_text(encoding="utf-8").strip().rstrip("/")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    parsed = urlsplit(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return endpoint


def _bark_amount(value):
    parsed = number(value)
    return f"${parsed:,.2f}" if parsed is not None else "--"


def _bark_slope(value):
    parsed = number(value)
    return f"{parsed:,.2f} / 1%" if parsed is not None else "--"


def _bark_fingerprint(cycle, marker):
    cycle_started = cycle.get("started_at") or "unknown-cycle"
    marker_time = marker.get("generated_at") or "unknown-marker"
    marker_percent = marker.get("percent")
    return f"{cycle_started}|{marker_time}|{marker_percent}"


def _send_bark(title, body):
    endpoint = _bark_endpoint()
    if not endpoint:
        return False
    query = urlencode({"title": title, "body": body, "group": "newapi-about-page"})
    separator = "&" if "?" in endpoint else "?"
    request = urllib.request.Request(
        f"{endpoint}{separator}{query}",
        headers={"User-Agent": "newapi-about-page/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=BARK_REQUEST_TIMEOUT) as response:
            status = getattr(response, "status", response.getcode())
        if 200 <= status < 300:
            print("bark notification sent", flush=True)
            return True
        print(f"bark notification failed: HTTP {status}", flush=True)
    except urllib.error.HTTPError as exc:
        print(f"bark notification failed: HTTP {exc.code}", flush=True)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"bark notification failed: {type(exc).__name__}", flush=True)
    return False


def send_bark_startup_notification():
    if not _bark_endpoint():
        return
    body = (
        "额度监控容器已启动，开始采集。\n"
        f"主账号：{MAIN_ACCOUNT_ID}\n"
        f"Spark 账号：{SPARK_ACCOUNT_ID}\n"
        f"普通采样：{NORMAL_INTERVAL_SECONDS}s；高频采样：{FAST_INTERVAL_SECONDS}s"
    )
    _send_bark("额度监控已启动", body)


def notify_slope_changes(cycles):
    global bark_previous_fingerprints, bark_pending_notifications
    if not _bark_endpoint() or not cycles:
        return
    cycle = cycles[-1]
    current = {}
    for marker in cycle.get("change_markers") or []:
        fingerprint = _bark_fingerprint(cycle, marker)
        current[fingerprint] = marker
    if bark_previous_fingerprints is None:
        bark_previous_fingerprints = set(current)
        return
    for fingerprint, marker in current.items():
        if fingerprint not in bark_previous_fingerprints:
            bark_pending_notifications.setdefault(fingerprint, (cycle, marker))
    bark_previous_fingerprints = set(current)
    for fingerprint, (pending_cycle, marker) in list(bark_pending_notifications.items()):
        if fingerprint not in current:
            bark_pending_notifications.pop(fingerprint, None)
            continue
        relative = number(marker.get("relative_change"))
        relative_text = f"{relative * 100:.1f}%" if relative is not None else "--"
        percent = number(marker.get("percent"))
        percent_text = f"{percent:.0f}%" if percent is not None else "--"
        body = (
            f"主账号 {pending_cycle.get('label', '当前额度周期')} 检测到使用额度斜率拐点\n"
            f"拐点时间（UTC）：{marker.get('generated_at') or '--'}\n"
            f"位置：{_bark_amount(marker.get('amount'))} / 已用 {percent_text}\n"
            f"斜率：{_bark_slope(marker.get('slope_before'))} -> {_bark_slope(marker.get('slope_after'))}\n"
            f"相对变化：{relative_text}"
        )
        if _send_bark("额度斜率拐点", body):
            bark_pending_notifications.pop(fingerprint, None)


def collect_once():
    global state, adaptive_fast_until
    with collect_lock:
        with state_lock:
            previous = dict(state.get("sources", {})) if isinstance(state, dict) else {}
            previous_interval_seconds = (
                number(state.get("interval_seconds"))
                if isinstance(state, dict)
                else None
            ) or NORMAL_INTERVAL_SECONDS

        results = {}
        with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
            futures = {pool.submit(source_result, name, previous.get(name, {})): name for name in SOURCES}
            for future in as_completed(futures):
                name = futures[future]
                results[name] = future.result()

        if large_usage_change(previous, results, previous_interval_seconds):
            adaptive_fast_until = max(
                adaptive_fast_until,
                time.monotonic() + FAST_HOLD_SECONDS,
            )

        snapshot = {
            "version": 2,
            "generated_at": iso_now(),
            "interval_seconds": sampling_interval_seconds(),
            "sampling_policy": sampling_policy(),
            "sources": results,
        }
        persist(snapshot)
        try:
            points = history_points(24 * HISTORY_RETENTION_DAYS)
            notify_slope_changes(history_cycles(points))
        except Exception as exc:
            print(f"bark notification error: {type(exc).__name__}", flush=True)
        with state_lock:
            state = snapshot
        return snapshot


def collector_loop():
    while True:
        started = time.monotonic()
        try:
            collect_once()
        except Exception as exc:
            print(f"collector error: {exc}", flush=True)
        elapsed = time.monotonic() - started
        interval = sampling_interval_seconds()
        time.sleep(max(5, interval - elapsed))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HTML_DIR), **kwargs)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header(
            "Content-Security-Policy",
            f"frame-ancestors {FRAME_ANCESTORS}",
        )
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_GET(self):
        parsed = urlsplit(self.path)
        clean_path = parsed.path
        if clean_path == "/about-monitor" or clean_path.startswith("/about-monitor/"):
            self.path = self.path[len("/about-monitor"):]
            if not self.path or self.path.startswith("?"):
                self.path = "/" + self.path
            clean_path = self.path.split("?", 1)[0]
        if clean_path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if clean_path == "/api/status":
            with state_lock:
                snapshot = state
            body = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(200 if snapshot else 503)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if clean_path == "/api/history":
            try:
                hours = int(parse_qs(parsed.query).get("hours", [24 * 90])[0])
                points = history_points(hours)
                body = json.dumps(
                    {"hours": hours, "points": points, "cycles": history_cycles(points)},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
            except Exception as exc:
                body = json.dumps({"error": str(exc)[:300]}, ensure_ascii=False).encode("utf-8")
                self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):
        clean_path = self.path.split("?", 1)[0]
        if clean_path == "/about-monitor" or clean_path.startswith("/about-monitor/"):
            clean_path = clean_path[len("/about-monitor"):]
            if not clean_path:
                clean_path = "/"
        if clean_path != "/api/refresh":
            self.send_error(404)
            return
        try:
            snapshot = collect_once()
            body = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
        except Exception as exc:
            body = json.dumps(
                {"error": str(exc)[:300]},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(500)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print(f"http {self.address_string()} {format % args}", flush=True)


def load_cache():
    global state
    try:
        cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(cached, dict):
            state = cached
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state = {}


def main():
    load_cache()
    try:
        send_bark_startup_notification()
    except Exception as exc:
        print(f"bark startup notification error: {type(exc).__name__}", flush=True)
    threading.Thread(target=collector_loop, name="collector", daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(
        f"serving on :{PORT}; sampling normal={NORMAL_INTERVAL_SECONDS}s "
        f"fast={FAST_INTERVAL_SECONDS}s hold={FAST_HOLD_SECONDS}s",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
