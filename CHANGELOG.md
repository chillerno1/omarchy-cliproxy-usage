# Changelog

## 0.1.2

- Show a compact, read-only Gateway policy section below the account meters: within-tier routing, session affinity,
  retry rounds, and credentials per round.
- Read gateway settings from current and legacy block YAML config layouts on every check; show unavailable values
  when the config cannot be read or a setting uses an unsupported value.
- Update the panel screenshot and document how to regenerate it with fictional accounts.

## 0.1.1

- Fix Antigravity showing 0% when Gemini quota has been used by sending the versioned client User-Agent expected by Google.
- Try alternate quota hosts when a successful response omits Gemini limits, retaining the first result for each bucket.
- Keep Claude/GPT labels when Google returns only that group's limits.
- Clarify that the widget shows quota used, while CLIProxyAPI's Antigravity panel shows quota remaining.

## 0.1.0

First release.

- Bar icon and panel showing plan limits for every OAuth account in CLIProxyAPI's auth-dir: Claude, Codex, Antigravity,
  Kimi, Grok, Devin and Meta.
- Finds the proxy's config from the running process, and shows whether the proxy answers on its port.
- Keeps the last good numbers per account, marked stale, when a check fails or a token has lapsed.
- Accounts with nothing to show are named in one line, with the reason on hover.
- Standard-library Python collector with fixture tests for every provider.
