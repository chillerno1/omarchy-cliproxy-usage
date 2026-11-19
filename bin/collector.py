#!/usr/bin/env python3
"""Rate-limit usage for every account behind a local CLIProxyAPI.

Prints one JSON record for the CLIProxy Usage bar widget. For each OAuth
credential file in CLIProxyAPI's auth-dir, it asks the provider's own usage
endpoint how much of each plan window is used and when it resets, the same
way the stock Omarchy Agents panel and CLIProxyAPI's management center do.

- Credential files are only read, never changed. CLIProxyAPI keeps the
  tokens fresh; a lapsed one is not used, since only a 401 would come back.
- Each token is sent only to the provider that issued it, over HTTPS, to a
  fixed URL. The printed record and the cache hold no secrets.
- The last good answer per account is cached (0700 dir, 0600 file) and shown,
  marked stale, when a check fails. Answers under FRESH_SECONDS old are
  reused unless --force is given.

Standard library only.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

FRESH_SECONDS = 120
HTTP_TIMEOUT = 10
BODY_LIMIT = 1 << 20
MAX_ACCOUNTS = 64
MAX_TEXT = 120

# type in the credential file -> (icon / provider id, display name)
PROVIDERS = {
  "claude": ("claude", "Claude"),
  "codex": ("codex", "Codex"),
  "antigravity": ("antigravity", "Antigravity"),
  "kimi": ("kimi", "Kimi"),
  "kimi-ai": ("kimi", "Kimi"),
  "xai": ("grok", "Grok"),
  "devin": ("devin", "Devin"),
  "meta": ("meta", "Meta"),
}

CLAUDE_USAGE = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_PROFILE = "https://api.anthropic.com/api/oauth/profile"
CODEX_USAGE = "https://chatgpt.com/backend-api/wham/usage"
ANTIGRAVITY_BASES = (
  "https://daily-cloudcode-pa.googleapis.com",
  "https://daily-cloudcode-pa.sandbox.googleapis.com",
  "https://cloudcode-pa.googleapis.com",
)
# Match CLIProxyAPI's management center: an unversioned client can receive a
# successful Claude/GPT-only summary with the Gemini quota silently omitted.
ANTIGRAVITY_USER_AGENT = "antigravity/hub/2.23.0 darwin/arm64"
KIMI_USAGE = {"kimi": "https://api.kimi.com/coding/v1/usages", "kimi-ai": "https://api.kimi.ai/coding/v1/usages"}
GROK_CREDITS = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_MONTHLY = "https://cli-chat-proxy.grok.com/v1/billing"
GROK_VERSION = "1.0.70"
DEVIN_STATUS = "https://server.codeium.com/exa.seat_management_pb.SeatManagementService/GetUserStatus"
META_KEY = "https://api.meta.ai/muse-code/key"


class ProbeError(Exception):
  pass


# ------------------------------------------------------------------ plumbing


def now_ms() -> int:
  return int(time.time() * 1000)


def text(value: Any, limit: int = MAX_TEXT) -> str:
  """A bounded, single-line string from untrusted data."""
  if value is None or isinstance(value, (dict, list)):
    return ""
  return " ".join(str(value).split())[:limit]


def number(value: Any) -> float | None:
  if isinstance(value, bool) or value is None:
    return None
  if isinstance(value, dict):
    value = value.get("val", 0)
  try:
    n = float(value)
  except (TypeError, ValueError):
    return None
  return n if math.isfinite(n) else None


def parse_time(value: Any) -> int:
  """Epoch ms from an ISO-8601 string or epoch seconds/ms; 0 when unknown."""
  if value is None or isinstance(value, bool):
    return 0
  n = number(value) if not isinstance(value, str) or value.strip().lstrip("-").isdigit() else None
  if n is not None:
    if n <= 0:
      return 0
    return int(n if n >= 1e12 else n * 1000)
  raw = str(value).strip()
  if not raw:
    return 0
  raw = raw.replace("Z", "+00:00").replace("z", "+00:00")
  # Python reads at most six fraction digits; some providers send nine.
  raw = re.sub(r"\.(\d{6})\d+", r".\1", raw)
  try:
    parsed = datetime.fromisoformat(raw)
  except ValueError:
    return 0
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=timezone.utc)
  return int(parsed.timestamp() * 1000)


def limit(title: str, used_percent: float | None, resets_at: Any = None) -> dict[str, Any] | None:
  """One window: used share 0..1, reset as epoch ms (0 when unknown)."""
  if used_percent is None:
    return None
  return {
    "title": text(title, 48) or "Limit",
    "percent": round(min(1.0, max(0.0, used_percent / 100.0)), 4),
    "resetsAt": parse_time(resets_at),
  }


def http_json(url: str, headers: dict[str, str], body: Any = None) -> Any:
  data = None if body is None else json.dumps(body).encode()
  request = urllib.request.Request(url, data=data, method="GET" if body is None else "POST")
  request.add_header("Accept", "application/json")
  if body is not None:
    request.add_header("Content-Type", "application/json")
  for key, value in headers.items():
    request.add_header(key, value)
  try:
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
      raw = response.read(BODY_LIMIT + 1)
  except urllib.error.HTTPError as error:
    raise ProbeError(status_note(error.code)) from None
  except (urllib.error.URLError, TimeoutError, OSError):
    raise ProbeError("Couldn't reach the usage endpoint") from None
  if len(raw) > BODY_LIMIT:
    raise ProbeError("The usage endpoint's answer was too large")
  try:
    return json.loads(raw)
  except ValueError:
    raise ProbeError("The usage endpoint returned an unreadable answer") from None


def status_note(code: int) -> str:
  if code == 429:
    return "The provider is rate limiting usage checks right now"
  if code in (401, 403):
    return "The provider refused the token; CLIProxyAPI refreshes it on the account's next request"
  return f"The usage endpoint answered with status {code}"


def bearer(token: str) -> dict[str, str]:
  return {"Authorization": "Bearer " + token}


def obj(value: Any) -> dict[str, Any]:
  return value if isinstance(value, dict) else {}


def title_case(value: str) -> str:
  return value[:1].upper() + value[1:] if value else ""


# ------------------------------------------------------------------ providers

Probe = dict[str, Any]


def probe_claude(cred: dict[str, Any]) -> Probe:
  headers = {**bearer(cred.get("access_token", "")), "anthropic-beta": "oauth-2025-04-20"}
  usage = obj(http_json(CLAUDE_USAGE, headers))
  limits = []
  for key, title in (("five_hour", "Session"), ("seven_day", "Weekly")):
    bucket = obj(usage.get(key))
    limits.append(limit(title, number(bucket.get("utilization")), bucket.get("resets_at")))
  # Model-scoped allowances ("Fable" weekly) are listed in `limits`.
  for entry in usage.get("limits") or []:
    entry = obj(entry)
    if not str(entry.get("kind", "")).endswith("_scoped"):
      continue
    model = text(obj(obj(entry.get("scope")).get("model")).get("display_name"), 32) or "Model"
    span = "session" if entry.get("group") == "session" else "weekly"
    limits.append(limit(f"{model} {span}", number(entry.get("percent")), entry.get("resets_at")))

  plan = ""
  try:
    org = obj(obj(http_json(CLAUDE_PROFILE, headers)).get("organization"))
    tier = str(org.get("rate_limit_tier", ""))
    plan = text(org.get("plan_display_name"), 32) or (
      "Max 20x" if "max_20x" in tier else "Max 5x" if "max_5x" in tier
      else "Pro" if "pro" in str(org.get("organization_type", "")) else ""
    )
  except ProbeError:
    pass

  extra = ""
  extra_usage = obj(usage.get("extra_usage"))
  if extra_usage.get("is_enabled") is True:
    used = number(extra_usage.get("used_credits")) or 0
    cap = number(extra_usage.get("monthly_limit")) or 0
    extra = f"Extra usage {used / 100:.2f} / {cap / 100:.2f} {text(extra_usage.get('currency'), 4)}".strip()
  return {"limits": limits, "plan": plan, "extra": extra}


def codex_windows(rate_limit: dict[str, Any], prefix: str = "") -> list[dict[str, Any] | None]:
  out = []
  for key in ("primary_window", "secondary_window"):
    window = obj(rate_limit.get(key))
    if not window:
      continue
    seconds = int(number(window.get("limit_window_seconds")) or 0)
    if seconds >= 28 * 86400:
      span = "Monthly"
    elif seconds >= 86400:
      span = "Weekly"
    elif seconds > 0:
      span = "Session"
    else:
      span = "Limit"
    title = f"{prefix} {span.lower()}" if prefix else span
    out.append(limit(title, number(window.get("used_percent")), window.get("reset_at")))
  return out


def probe_codex(cred: dict[str, Any]) -> Probe:
  headers = {**bearer(cred.get("access_token", "")), "User-Agent": "codex_cli_rs"}
  account_id = str(cred.get("account_id") or "")
  if account_id:
    headers["ChatGPT-Account-Id"] = account_id
  usage = obj(http_json(CODEX_USAGE, headers))
  limits = codex_windows(obj(usage.get("rate_limit")))
  for entry in usage.get("additional_rate_limits") or []:
    entry = obj(entry)
    limits += codex_windows(obj(entry.get("rate_limit")), text(entry.get("limit_name"), 32))
  credits = obj(usage.get("credits"))
  extra = ""
  if credits.get("unlimited") is True:
    extra = "Credits unlimited"
  elif credits.get("has_credits") is True and number(credits.get("balance")) is not None:
    extra = f"Credits {round(number(credits.get('balance'))):,}"
  plan = text(usage.get("plan_type"), 24) or text(cred.get("plan_type"), 24)
  return {"limits": limits, "plan": title_case(plan), "extra": extra}


def antigravity_used(bucket: dict[str, Any]) -> float | None:
  remaining = number(bucket.get("remainingFraction", bucket.get("remaining_fraction")))
  if remaining is None:
    # Proto3 JSON leaves a zero out: a bucket with a reset but no fraction is spent.
    return 100.0 if bucket.get("resetTime") or bucket.get("reset_time") else None
  return (1.0 - min(1.0, max(0.0, remaining))) * 100.0


def probe_antigravity(cred: dict[str, Any]) -> Probe:
  headers = {**bearer(cred.get("access_token", "")), "User-Agent": ANTIGRAVITY_USER_AGENT}
  last: ProbeError | None = None
  assist = {}
  groups = []
  seen = set()
  for base in ANTIGRAVITY_BASES:
    try:
      loaded = obj(http_json(f"{base}/v1internal:loadCodeAssist", headers, {}))
      if not assist:
        assist = loaded
      project = loaded.get("cloudaicompanionProject") or cred.get("project_id") or ""
      project = obj(project).get("id", "") if isinstance(project, dict) else project
      body = {"project": project} if isinstance(project, str) and project else {}
      summary = obj(http_json(f"{base}/v1internal:retrieveUserQuotaSummary", headers, body))
      incoming = [obj(g) for g in summary.get("groups") or [] if isinstance(g, dict)]
      if not incoming and isinstance(summary.get("buckets"), list):
        incoming = [{"displayName": "Gemini", "buckets": summary["buckets"]}]
      # A 200 response can be partial. Keep the first answer for each bucket,
      # and try the other hosts until we have real Gemini quota too.
      for group in incoming:
        name = text(group.get("displayName") or group.get("display_name"), 32)
        buckets = []
        for raw in group.get("buckets") or []:
          bucket = obj(raw)
          if antigravity_used(bucket) is None:
            continue
          identity = (name, text(bucket.get("bucketId") or bucket.get("bucket_id") or bucket.get("window")))
          if identity not in seen:
            seen.add(identity)
            buckets.append(bucket)
        if buckets:
          groups.append({"displayName": name, "buckets": buckets})
      if any("gemini" in g["displayName"].lower() or any(
        str(b.get("bucketId") or b.get("bucket_id") or "").lower().startswith("gemini-")
        for b in g["buckets"]
      ) for g in groups):
        break
    except ProbeError as error:
      last = error
  if not groups:
    raise last or ProbeError("Couldn't reach Google's quota service")

  plan = ""
  for key in ("paidTier", "currentTier"):
    tier = obj(assist.get(key))
    if tier.get("name"):
      plan = text(tier.get("name"), 32)
      break

  limits = []
  for group in groups:
    name = text(group.get("displayName") or group.get("display_name"), 32)
    for bucket in group.get("buckets") or []:
      bucket = obj(bucket)
      plan_group = "gemini" in name.lower() or str(bucket.get("bucketId") or bucket.get("bucket_id") or "").lower().startswith("gemini-")
      used = antigravity_used(bucket)
      if used is None or (not plan_group and used <= 0):
        continue
      window = str(bucket.get("window") or "").lower()
      span = "Weekly" if "week" in window else "Session"
      title = span if plan_group else f"{name} {span.lower()}"
      limits.append(limit(title, used, bucket.get("resetTime") or bucket.get("reset_time")))
  return {"limits": limits, "plan": plan, "extra": ""}


def kimi_reset(detail: dict[str, Any]) -> Any:
  for key in ("reset_at", "resetAt", "reset_time", "resetTime"):
    if detail.get(key):
      return detail[key]
  return None


def kimi_row(detail: dict[str, Any], title: str) -> dict[str, Any] | None:
  cap = number(detail.get("limit"))
  used = number(detail.get("used"))
  if used is None and cap is not None and number(detail.get("remaining")) is not None:
    used = cap - number(detail.get("remaining"))
  if not cap or cap <= 0 or used is None:
    return None
  return limit(text(detail.get("name") or detail.get("title"), 32) or title, used / cap * 100, kimi_reset(detail))


def probe_kimi(cred: dict[str, Any]) -> Probe:
  url = KIMI_USAGE.get(str(cred.get("type")), KIMI_USAGE["kimi"])
  data = obj(http_json(url, bearer(cred.get("access_token", ""))))
  limits = []
  usages = data.get("usages")
  if isinstance(usages, dict):
    for key, title in (("limit_5h", "Session"), ("limit_7d", "Weekly"),
                       ("limit_month_total", "Monthly"), ("limit_month_code", "Code monthly")):
      detail = obj(usages.get(key))
      ratio = number(detail.get("used_ratio"))
      if ratio is not None:
        limits.append(limit(title, ratio * 100, kimi_reset(detail)))
  else:
    if isinstance(data.get("usage"), dict):
      limits.append(kimi_row(data["usage"], "Weekly"))
    for index, entry in enumerate(data.get("limits") or []):
      entry = obj(entry)
      detail = obj(entry.get("detail")) or entry
      window = obj(entry.get("window"))
      duration = int(number(window.get("duration") or entry.get("duration")) or 0)
      unit = str(window.get("timeUnit") or entry.get("timeUnit") or "").upper()
      if "MINUTE" in unit and duration == 300 or "HOUR" in unit and duration == 5:
        title = "Session"
      elif "DAY" in unit and duration == 7:
        title = "Weekly"
      else:
        title = f"Limit {index + 1}"
      limits.append(kimi_row(detail, title))
  return {"limits": limits, "plan": "", "extra": ""}


def probe_grok(cred: dict[str, Any]) -> Probe:
  headers = {
    **bearer(cred.get("access_token", "")),
    "x-xai-token-auth": "xai-grok-cli",
    "x-grok-client-version": GROK_VERSION,
    "User-Agent": f"grok-pager/{GROK_VERSION} grok-shell/{GROK_VERSION}",
  }
  config = obj(obj(http_json(GROK_CREDITS, headers)).get("config"))
  unified = config.get("isUnifiedBillingUser") is True
  limits = []
  if "creditUsagePercent" in config or not unified:
    period = obj(config.get("currentPeriod"))
    title = "Weekly" if "WEEK" in str(period.get("type", "")).upper() else "Monthly"
    limits.append(limit(title, number(config.get("creditUsagePercent")) or 0.0, period.get("end")))
  if unified:
    try:
      monthly = obj(obj(http_json(GROK_MONTHLY, headers)).get("config"))
      quota = number(monthly.get("monthlyLimit")) or 0
      if quota > 0:
        used = number(monthly.get("used")) or 0
        limits.append(limit("Monthly", used / quota * 100, monthly.get("billingPeriodEnd")))
    except ProbeError:
      pass
  return {"limits": limits, "plan": "", "extra": ""}


def probe_devin(cred: dict[str, Any]) -> Probe:
  token = str(cred.get("api_key") or cred.get("session_token") or "")
  body = {"metadata": {
    "ideName": "chisel", "ideVersion": "3000.10.21", "extensionVersion": "3000.10.21",
    "clientName": "chisel", "locale": "en", "os": "linux", "apiKey": token,
  }}
  status = obj(obj(http_json(DEVIN_STATUS, {"Connect-Protocol-Version": "1"}, body)).get("userStatus"))
  plan_status = obj(status.get("planStatus"))
  limits = []
  for key, title in (("daily", "Daily"), ("weekly", "Weekly")):
    remaining_key = f"{key}QuotaRemainingPercent"
    reset_key = f"{key}QuotaResetAtUnix"
    if remaining_key not in plan_status and reset_key not in plan_status:
      continue
    # A zero is left out of proto3 JSON, so a window with a reset and no
    # remainder has none left.
    remaining = number(plan_status.get(remaining_key)) or 0.0
    limits.append(limit(title, 100.0 - remaining, plan_status.get(reset_key)))
  plan = text(obj(plan_status.get("planInfo")).get("planName"), 32) or text(cred.get("plan"), 32)
  return {"limits": limits, "plan": plan, "extra": ""}


def probe_meta(cred: dict[str, Any]) -> Probe:
  token = str(cred.get("dca_token") or cred.get("access_token") or "")
  payload = obj(http_json(META_KEY, {**bearer(token), "x-api-version": "1.0.0"}, {}))
  usage = obj(payload.get("subs_usage"))
  limits = []
  window = obj(usage.get("window"))
  if window:
    limits.append(limit("Session", number(window.get("used_percent")), window.get("resets_at")))
  weekly = obj(usage.get("weekly"))
  if weekly:
    limits.append(limit("Weekly", number(weekly.get("used_percent")), weekly.get("resets_at")))
  return {"limits": limits, "plan": text(payload.get("subs_tier_name"), 32), "extra": ""}


PROBES: dict[str, Callable[[dict[str, Any]], Probe]] = {
  "claude": probe_claude,
  "codex": probe_codex,
  "antigravity": probe_antigravity,
  "kimi": probe_kimi,
  "kimi-ai": probe_kimi,
  "xai": probe_grok,
  "devin": probe_devin,
  "meta": probe_meta,
}


# ------------------------------------------------------------------ proxy


def home() -> Path:
  return Path(os.environ.get("HOME") or "/")


def expand(path: str) -> Path:
  return home() / path[2:] if path.startswith("~/") else home() if path == "~" else Path(path)


def config_from_process() -> Path | None:
  """The --config of a running cli-proxy-api, so a custom path needs no setup."""
  for proc in Path("/proc").glob("[0-9]*"):
    try:
      if proc.stat().st_uid != os.getuid():
        continue
      argv = (proc / "cmdline").read_bytes().split(b"\0")
    except OSError:
      continue
    if not argv or not argv[0].endswith(b"cli-proxy-api"):
      continue
    for index, arg in enumerate(argv):
      arg = arg.decode(errors="replace")
      if arg in ("-config", "--config") and index + 1 < len(argv):
        return expand(argv[index + 1].decode(errors="replace"))
      if arg.startswith(("-config=", "--config=")):
        return expand(arg.split("=", 1)[1])
  return None


def config_scalars(lines: list[str]) -> dict[tuple[str, ...], str]:
  """Read block-mapping scalar paths without loading credentials or YAML tags.

  Only exact allowlisted paths are consumed below. Complex YAML values stay
  uninterpreted and are displayed as unavailable rather than executed.
  """
  values = {}
  parents = []
  for line in lines:
    match = re.match(r"^( *)([\w-]+):(?:\s+(.*)|\s*)$", line)
    if not match:
      continue
    indent, key, raw = len(match[1]), match[2], match[3] or ""
    if raw.startswith("#"):
      raw = ""
    while parents and parents[-1][0] >= indent:
      parents.pop()
    path = tuple(p[1] for p in parents) + (key,)
    # Comments outside quotes only; a quoted path can itself contain ' #'.
    scalar = re.fullmatch(r'''("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^'"#]*?)(?:\s+#.*)?\s*''', raw)
    value = scalar[1].strip() if scalar else raw
    if value.startswith('"'):
      try:
        value = json.loads(value)
      except ValueError:
        pass
    elif value.startswith("'") and value.endswith("'"):
      value = value[1:-1].replace("''", "'")
    values[path] = value
    parents.append((indent, key))
  return values


def gateway_policy(values: dict[tuple[str, ...], str]) -> dict[str, Any]:
  def read(path: tuple[str, ...], default: str, legacy: tuple[str, ...] | None = None) -> str | None:
    if any(values.get(path[:i], "") for i in range(1, len(path))):
      return None  # flow mappings, aliases and other unsupported parent values
    return values.get(path, values.get(legacy, default) if legacy else default)

  strategy = read(("routing", "strategy"), "round-robin")
  affinity = read(("routing", "session-affinity"), "false")

  def count(key: str, default: str) -> int | None:
    raw = read(("routing", "retry", key), default, (key,))
    return int(raw) if raw and re.fullmatch(r"[0-9]{1,9}", raw) else None

  return {
    "strategy": strategy if strategy in ("round-robin", "weighted-round-robin", "fill-first") else None,
    "sessionAffinity": affinity.lower() == "true" if affinity and affinity.lower() in ("true", "false") else None,
    "retryRounds": count("request-retry", "3"),
    "credentialsPerRound": count("max-retry-credentials", "0"),
  }


def proxy_config() -> dict[str, Any]:
  """Read only the proxy location and gateway policy from block YAML."""
  env = os.environ.get("CLIPROXY_CONFIG")
  path = expand(env) if env else config_from_process() or home() / ".cli-proxy-api" / "config.yaml"
  config = {"path": str(path), "authDir": home() / ".cli-proxy-api", "port": 8317, "gatewayPolicy": None}
  try:
    lines = path.read_text(errors="replace").splitlines()
  except OSError:
    return config
  values = config_scalars(lines)
  port = values.get(("server", "port"), values.get(("port",), ""))
  auth_dir = values.get(("oauth", "auth-dir"), values.get(("auth-dir",), ""))
  if port.isdigit():
    config["port"] = int(port)
  if auth_dir:
    config["authDir"] = expand(auth_dir)
  config["gatewayPolicy"] = gateway_policy(values)
  return config


def proxy_up(port: int) -> bool:
  try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
      return b"CLI Proxy API" in response.read(4096)
  except (urllib.error.URLError, OSError, ValueError):
    return False


# ------------------------------------------------------------------ collect


def cache_path() -> Path:
  base = Path(os.environ.get("XDG_CACHE_HOME") or home() / ".cache")
  return base / "chillerno1.cliproxy-usage" / "usage.json"


def read_json(path: Path) -> Any:
  try:
    with open(path, "rb") as handle:
      return json.loads(handle.read(BODY_LIMIT))
  except (OSError, ValueError):
    return None


def write_private(path: Path, payload: Any) -> None:
  try:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".usage.")
    with os.fdopen(fd, "w") as handle:
      json.dump(payload, handle)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
  except OSError:
    pass


def account(account_id: str, cred: dict[str, Any], old: dict[str, Any], force: bool, now: int) -> dict[str, Any]:
  kind = str(cred.get("type"))
  provider, name = PROVIDERS[kind]
  out = {
    "id": account_id,
    "provider": provider,
    "providerName": name,
    "email": text(cred.get("email") or cred.get("user_name"), 254),
    "disabled": cred.get("disabled") is True,
  }

  def keep(status: str, note: str) -> dict[str, Any]:
    out.update(
      limits=old.get("limits") if isinstance(old.get("limits"), list) else [],
      plan=text(old.get("plan"), 32),
      extra=text(old.get("extra")),
      fetchedAt=int(number(old.get("fetchedAt")) or 0),
      status=status,
      note=note,
    )
    return out

  if out["disabled"]:
    return keep("disabled", "Disabled in CLIProxyAPI")
  if not force and old.get("status") == "ok" and now - (number(old.get("fetchedAt")) or 0) < FRESH_SECONDS * 1000:
    return keep("ok", "")
  expires = parse_time(cred.get("expired"))
  if expires and expires <= now:
    return keep("stale", "Token lapsed; CLIProxyAPI refreshes it on the account's next request")
  try:
    result = PROBES[kind](cred)
  except ProbeError as error:
    return keep("stale", str(error))
  except Exception:  # an answer of a shape nobody expected
    return keep("stale", "The usage endpoint returned an unexpected answer")
  limits = [entry for entry in result.get("limits", []) if entry]
  if not limits:
    return keep("stale", "The usage endpoint reported no limits")
  out.update(
    limits=limits,
    plan=result.get("plan") or text(old.get("plan"), 32),
    extra=result.get("extra", ""),
    fetchedAt=now,
    status="ok",
    note="",
  )
  return out


def collect(force: bool) -> dict[str, Any]:
  config = proxy_config()
  now = now_ms()
  previous = read_json(cache_path())
  kept = {}
  if isinstance(previous, dict) and isinstance(previous.get("accounts"), list):
    kept = {str(a.get("id")): a for a in previous["accounts"] if isinstance(a, dict)}

  creds = []
  unsupported = 0
  try:
    files = sorted(Path(config["authDir"]).glob("*.json"))
  except OSError:
    files = []
  for path in files[: MAX_ACCOUNTS * 2]:
    cred = read_json(path)
    if not isinstance(cred, dict) or not cred.get("type"):
      continue
    if str(cred["type"]) in PROVIDERS:
      creds.append((path.stem, cred))
    else:
      unsupported += 1
  creds = creds[:MAX_ACCOUNTS]

  with ThreadPoolExecutor(max_workers=8) as pool:
    up = pool.submit(proxy_up, config["port"])
    futures = [pool.submit(account, aid, cred, obj(kept.get(aid)), force, now) for aid, cred in creds]
    accounts = [future.result() for future in futures]
    running = up.result()

  record = {
    "schemaVersion": 1,
    "updatedAt": now,
    "proxy": {"up": running, "port": config["port"], "authDir": str(config["authDir"]), "config": config["path"]},
    "gatewayPolicy": config["gatewayPolicy"],
    "accounts": accounts,
    "unsupportedAccounts": unsupported,
  }
  write_private(cache_path(), record)
  return record


def main(argv: list[str]) -> int:
  record = collect("--force" in argv)
  sys.stdout.write(json.dumps(record) + "\n")
  return 0


if __name__ == "__main__":
  sys.exit(main(sys.argv[1:]))
