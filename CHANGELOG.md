# Changelog

## 0.1.0

First release.

- Bar icon and panel showing plan limits for every OAuth account in CLIProxyAPI's auth-dir: Claude, Codex, Antigravity,
  Kimi, Grok, Devin and Meta.
- Finds the proxy's config from the running process, and shows whether the proxy answers on its port.
- Keeps the last good numbers per account, marked stale, when a check fails or a token has lapsed.
- Accounts with nothing to show are named in one line, with the reason on hover.
- Standard-library Python collector with fixture tests for every provider.
