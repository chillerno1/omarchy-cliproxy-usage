"""Collector tests: every provider parser against recorded answer shapes,
plus the caching, lapsed-token and secrecy rules. No network.

Run: python3 -m unittest discover -s tests
"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("collector", ROOT / "bin" / "collector.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

FUTURE = "2099-01-01T00:00:00Z"
SECRET = "sk-test-SECRET-token-value"


def answers(table):
  """A fake http_json answering by URL substring."""
  def fake(url, headers, body=None):
    for needle, answer in table.items():
      if needle in url:
        if isinstance(answer, Exception):
          raise answer
        return answer
    raise collector.ProbeError(f"unexpected url {url}")
  return fake


def titles(probe):
  return [(l["title"], l["percent"]) for l in probe["limits"] if l]


class ParseTime(unittest.TestCase):
  def test_formats(self):
    self.assertEqual(collector.parse_time("1970-01-01T00:00:00Z"), 0)
    self.assertEqual(collector.parse_time("2026-11-12T02:29:59.548337+00:00"), 1794450599548)
    self.assertEqual(collector.parse_time("2026-11-12T13:29:59+11:00"), 1794450599000)
    self.assertEqual(collector.parse_time("2026-11-12T02:29:59.123456789Z"), 1794450599123)
    self.assertEqual(collector.parse_time(1794451196), 1794451196000)
    self.assertEqual(collector.parse_time("1794451196"), 1794451196000)
    self.assertEqual(collector.parse_time(1794451196000), 1794451196000)
    for bad in (None, "", "garbage", True, {}, -5):
      self.assertEqual(collector.parse_time(bad), 0)


class Providers(unittest.TestCase):
  def test_claude(self):
    usage = {
      "five_hour": {"utilization": 60.0, "resets_at": FUTURE},
      "seven_day": {"utilization": 84.0, "resets_at": FUTURE},
      "limits": [
        {"kind": "weekly_all", "group": "weekly", "percent": 84},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 100, "resets_at": FUTURE,
         "scope": {"model": {"display_name": "Fable"}}},
      ],
      "extra_usage": {"is_enabled": False},
    }
    profile = {"organization": {"rate_limit_tier": "default_claude_max_5x"}}
    with mock.patch.object(collector, "http_json", answers({"oauth/usage": usage, "oauth/profile": profile})):
      probe = collector.probe_claude({"access_token": "t"})
    self.assertEqual(titles(probe), [("Session", 0.6), ("Weekly", 0.84), ("Fable weekly", 1.0)])
    self.assertEqual(probe["plan"], "Max 5x")

  def test_claude_without_session_window(self):
    usage = {"five_hour": None, "seven_day": {"utilization": 50, "resets_at": FUTURE}}
    with mock.patch.object(collector, "http_json", answers({"oauth/usage": usage, "oauth/profile": collector.ProbeError("x")})):
      probe = collector.probe_claude({"access_token": "t"})
    self.assertEqual(titles(probe), [("Weekly", 0.5)])
    self.assertEqual(probe["plan"], "")

  def test_codex(self):
    usage = {
      "plan_type": "plus",
      "rate_limit": {
        "primary_window": {"used_percent": 7, "limit_window_seconds": 18000, "reset_at": 1794451196},
        "secondary_window": {"used_percent": 2, "limit_window_seconds": 604800, "reset_at": 1794523839},
      },
      "additional_rate_limits": [
        {"limit_name": "GPT-Spark", "rate_limit": {"primary_window": {"used_percent": 40, "limit_window_seconds": 604800}}},
      ],
      "credits": {"has_credits": True, "balance": "67415.07"},
    }
    with mock.patch.object(collector, "http_json", answers({"wham/usage": usage})):
      probe = collector.probe_codex({"access_token": "t", "account_id": "a"})
    self.assertEqual(titles(probe), [("Session", 0.07), ("Weekly", 0.02), ("GPT-Spark weekly", 0.4)])
    self.assertEqual(probe["plan"], "Plus")
    self.assertEqual(probe["extra"], "Credits 67,415")
    self.assertEqual(probe["limits"][0]["resetsAt"], 1794451196000)

  def test_antigravity(self):
    assist = {"cloudaicompanionProject": "proj-1", "paidTier": {"name": "Google AI Pro"}}
    summary = {"groups": [
      {"displayName": "Gemini", "buckets": [
        {"window": "5h", "remainingFraction": 0.75, "resetTime": FUTURE},
        {"window": "weekly", "remainingFraction": 0.5, "resetTime": FUTURE},
      ]},
      {"displayName": "Claude and GPT models", "buckets": [
        {"window": "weekly", "remainingFraction": 1.0, "resetTime": FUTURE},
        {"window": "5h", "resetTime": FUTURE},
      ]},
    ]}
    seen = {}
    def fake(url, headers, body=None):
      seen[url.rsplit(":", 1)[-1]] = body
      return assist if "loadCodeAssist" in url else summary
    with mock.patch.object(collector, "http_json", fake):
      probe = collector.probe_antigravity({"access_token": "t"})
    self.assertEqual(seen["retrieveUserQuotaSummary"], {"project": "proj-1"})
    self.assertEqual(titles(probe), [("Session", 0.25), ("Weekly", 0.5), ("Claude and GPT models session", 1.0)])
    self.assertEqual(probe["plan"], "Google AI Pro")

  def test_antigravity_falls_back_to_second_host(self):
    calls = []
    def fake(url, headers, body=None):
      calls.append(url)
      if "daily-" in url:
        raise collector.ProbeError("down")
      return {} if "loadCodeAssist" in url else {"buckets": [{"window": "5h", "remainingFraction": 0.9}]}
    with mock.patch.object(collector, "http_json", fake):
      probe = collector.probe_antigravity({"access_token": "t"})
    self.assertEqual(titles(probe), [("Session", 0.1)])
    self.assertTrue(any(u.startswith("https://cloudcode-pa") for u in calls))

  def test_kimi_usages_shape(self):
    data = {"usages": {
      "limit_5h": {"used_ratio": 0.3, "reset_at": FUTURE},
      "limit_7d": {"used_ratio": 0.6, "reset_at": "1794451196"},
    }}
    with mock.patch.object(collector, "http_json", answers({"api.kimi.ai": data})):
      probe = collector.probe_kimi({"type": "kimi-ai", "access_token": "t"})
    self.assertEqual(titles(probe), [("Session", 0.3), ("Weekly", 0.6)])

  def test_kimi_legacy_shape(self):
    data = {
      "usage": {"limit": 100, "remaining": 25, "resetTime": FUTURE},
      "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"}, "detail": {"limit": 10, "used": 5}}],
    }
    with mock.patch.object(collector, "http_json", answers({"api.kimi.com": data})):
      probe = collector.probe_kimi({"type": "kimi", "access_token": "t"})
    self.assertEqual(titles(probe), [("Weekly", 0.75), ("Session", 0.5)])

  def test_grok(self):
    credits = {"config": {"creditUsagePercent": 12.5, "isUnifiedBillingUser": True,
                          "currentPeriod": {"type": "PERIOD_TYPE_WEEKLY", "end": FUTURE}}}
    monthly = {"config": {"monthlyLimit": {"val": 2000}, "used": {"val": 500}, "billingPeriodEnd": FUTURE}}
    with mock.patch.object(collector, "http_json", answers({"format=credits": credits, "v1/billing": monthly})):
      probe = collector.probe_grok({"access_token": "t"})
    self.assertEqual(titles(probe), [("Weekly", 0.125), ("Monthly", 0.25)])

  def test_grok_new_period_without_percent_is_zero(self):
    credits = {"config": {"currentPeriod": {"type": "PERIOD_TYPE_MONTHLY", "end": FUTURE}}}
    with mock.patch.object(collector, "http_json", answers({"format=credits": credits})):
      probe = collector.probe_grok({"access_token": "t"})
    self.assertEqual(titles(probe), [("Monthly", 0.0)])

  def test_devin(self):
    status = {"userStatus": {"planStatus": {
      "dailyQuotaRemainingPercent": 80, "dailyQuotaResetAtUnix": "1794451196",
      "weeklyQuotaResetAtUnix": "1794523839",
      "planInfo": {"planName": "Pro"},
    }}}
    seen = {}
    def fake(url, headers, body=None):
      seen["body"] = body
      return status
    with mock.patch.object(collector, "http_json", fake):
      probe = collector.probe_devin({"api_key": "k"})
    self.assertEqual(titles(probe), [("Daily", 0.2), ("Weekly", 1.0)])
    self.assertEqual(probe["plan"], "Pro")
    self.assertEqual(seen["body"]["metadata"]["apiKey"], "k")

  def test_meta(self):
    payload = {"subs_tier_name": "Plus", "subs_usage": {
      "window": {"used_percent": 30, "resets_at": 1794451196, "window_duration_mins": 300},
      "weekly": {"used_percent": 10, "resets_at": FUTURE},
    }}
    with mock.patch.object(collector, "http_json", answers({"muse-code/key": payload})):
      probe = collector.probe_meta({"dca_token": "d", "access_token": "a"})
    self.assertEqual(titles(probe), [("Session", 0.3), ("Weekly", 0.1)])
    self.assertEqual(probe["plan"], "Plus")

  def test_meta_without_subscription_has_no_limits(self):
    with mock.patch.object(collector, "http_json", answers({"muse-code/key": {"is_subs_active": False}})):
      self.assertEqual(collector.probe_meta({"access_token": "a"})["limits"], [])

  def test_percent_is_clamped_and_text_bounded(self):
    row = collector.limit("<b>x</b>\n" + "y" * 200, 250, None)
    self.assertEqual(row["percent"], 1.0)
    self.assertNotIn("\n", row["title"])
    self.assertLessEqual(len(row["title"]), 48)
    self.assertIsNone(collector.limit("x", None))


class Collect(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    root = Path(self.tmp.name)
    self.auth = root / "auth"
    self.auth.mkdir()
    (root / "config.yaml").write_text(
      f'server:\n  host: "127.0.0.1"\n  port: 9999\noauth:\n  auth-dir: "{self.auth}"\n'
    )
    self.env = mock.patch.dict(os.environ, {
      "CLIPROXY_CONFIG": str(root / "config.yaml"),
      "XDG_CACHE_HOME": str(root / "cache"),
    })
    self.env.start()
    self.addCleanup(self.env.stop)
    self.addCleanup(self.tmp.cleanup)
    mock.patch.object(collector, "proxy_up", lambda port: True).start()
    self.addCleanup(mock.patch.stopall)

  def cred(self, name, **fields):
    (self.auth / f"{name}.json").write_text(json.dumps(fields))

  def test_full_record(self):
    self.cred("claude-a", type="claude", email="a@example.com", access_token=SECRET, expired=FUTURE)
    self.cred("xai-b", type="xai", email="b@example.com", access_token=SECRET, expired="2000-01-01T00:00:00Z")
    self.cred("vertex-c", type="vertex", service_account={"private_key": SECRET})
    self.cred("codex-d", type="codex", access_token=SECRET, disabled=True)
    usage = {"seven_day": {"utilization": 10, "resets_at": FUTURE}}
    with mock.patch.object(collector, "http_json", answers({"oauth/usage": usage, "oauth/profile": {}})):
      record = collector.collect(force=True)

    self.assertEqual(record["proxy"]["port"], 9999)
    self.assertEqual(record["proxy"]["authDir"], str(self.auth))
    self.assertEqual(record["unsupportedAccounts"], 1)
    by_id = {a["id"]: a for a in record["accounts"]}
    self.assertEqual(set(by_id), {"claude-a", "xai-b", "codex-d"})
    self.assertEqual(by_id["claude-a"]["status"], "ok")
    self.assertEqual(by_id["xai-b"]["status"], "stale")
    self.assertIn("lapsed", by_id["xai-b"]["note"])
    self.assertEqual(by_id["codex-d"]["status"], "disabled")

    cache = Path(os.environ["XDG_CACHE_HOME"]) / "chillerno1.cliproxy-usage" / "usage.json"
    self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
    self.assertEqual(cache.parent.stat().st_mode & 0o777, 0o700)
    self.assertNotIn(SECRET, json.dumps(record))
    self.assertNotIn(SECRET, cache.read_text())

  def test_recent_answer_is_reused_and_failure_keeps_last_numbers(self):
    self.cred("claude-a", type="claude", access_token=SECRET, expired=FUTURE)
    usage = {"seven_day": {"utilization": 10, "resets_at": FUTURE}}
    calls = []
    def counting(url, headers, body=None):
      calls.append(url)
      return answers({"oauth/usage": usage, "oauth/profile": {}})(url, headers, body)
    with mock.patch.object(collector, "http_json", counting):
      collector.collect(force=False)
      collector.collect(force=False)
    self.assertEqual(sum("oauth/usage" in u for u in calls), 1)

    with mock.patch.object(collector, "http_json", answers({"oauth": collector.ProbeError("The provider is rate limiting usage checks right now")})):
      account = collector.collect(force=True)["accounts"][0]
    self.assertEqual(account["status"], "stale")
    self.assertIn("rate limiting", account["note"])
    self.assertEqual([l["title"] for l in account["limits"]], ["Weekly"])

  def test_unexpected_answer_shape_does_not_crash(self):
    self.cred("claude-a", type="claude", access_token=SECRET, expired=FUTURE)
    with mock.patch.object(collector, "http_json", answers({"oauth": ["not", "an", "object"]})):
      account = collector.collect(force=True)["accounts"][0]
    self.assertEqual(account["status"], "stale")


class Config(unittest.TestCase):
  def test_legacy_top_level_layout(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "config.yaml"
      path.write_text('port: 8400 # legacy\nauth-dir: "~/elsewhere"\nserver:\n  tls:\n    port: 1\n')
      with mock.patch.dict(os.environ, {"CLIPROXY_CONFIG": str(path), "HOME": tmp}):
        config = collector.proxy_config()
    self.assertEqual(config["port"], 8400)
    self.assertEqual(config["authDir"], Path(tmp) / "elsewhere")


if __name__ == "__main__":
  unittest.main()
