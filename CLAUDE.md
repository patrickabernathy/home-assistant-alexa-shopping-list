# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

A Home Assistant integration that two-way-syncs the Alexa Shopping List with the
Home Assistant shopping list. Amazon cut off third-party shopping-list API access
in Summer 2024, so the sync is a hybrid: a human logs in once through a real
browser (robust against CAPTCHA / device-approval), and the server then drives the
list over Amazon's own private list JSON API using the persisted session cookies —
no DOM scraping in the steady state. See `docs/json-api-hardening.md` for the full
API contract and the reasoning behind the merge.

This is a **personal fork** of
[madmachinations/home-assistant-alexa-shopping-list](https://github.com/madmachinations/home-assistant-alexa-shopping-list),
maintained for a self-hosted US deployment. See `README.md` → "About this fork"
for the user-facing summary of what differs.

## Components

Three independent parts, each in its own directory:

- **`server/`** — a WebSocket bridge (port 4000) to Amazon's Alexa-list JSON API,
  packaged as a Docker image. Runs on the HA host or another LAN machine. This is
  where almost all logic lives.
  - `server.py` — WebSocket server + command router (`ping`, `authenticated`,
    `login`, `mfa`, `get_list`, `add_item`, `update_item`, `remove_item`,
    `config_*`, `reset`, `shutdown`). Each command spins up an `AlexaShoppingList`,
    runs, and tears it down. Config persists to `config.json` under
    `ASL_CONFIG_PATH`.
  - `alexa.py` — the `AlexaShoppingList` JSON-API client (Phase 2 of
    `docs/json-api-hardening.md`): a `requests.Session` that loads the persisted
    cookies and reads/adds/updates/removes items via the
    `amazon.com/alexashoppinglists/api` endpoints, no CSRF/bearer token needed.
    Selenium is no longer used here for list operations.
- **`client/`** — a Python CLI (`client.py` + `authenticator.py`) used mainly for
  first-time setup: it opens a real browser for login and ships the resulting
  session cookies to the server.
- **`custom_components/alexa_shopping_list/`** — the Home Assistant integration
  that talks to the server over WebSocket (`manifest.json`, HACS-installable).

## Fork-specific behaviour (do not "fix" these back to upstream)

- **JSON API, not DOM scraping.** Upstream drives the Alexa list web page with
  Selenium. This fork (Phase 2 of `docs/json-api-hardening.md`) replaced that with
  direct `requests` calls to `amazon.com/alexashoppinglists/api`, authorized purely
  by the persisted session cookies. The public method signatures on
  `AlexaShoppingList` are unchanged, so `server.py` and the HA integration didn't
  move. The JSON `completed` flag also makes item check-off available for free.
- **Forced `amazon.com`.** `server.py` hard-codes `amazon.com` when constructing
  `AlexaShoppingList`, ignoring any stored `amazon_url` config value. A regional
  URL silently breaks the list flow for this deployment. See the comment in
  `_start_alexa()`.
- **Persistent rotated cookies.** `alexa.py:save_session()` re-saves the cookies
  Amazon rotates on each request — after every list operation (not just on
  teardown), written atomically via a temp file + `os.replace`, throttled to once
  per `COOKIE_SAVE_THROTTLE_SECONDS` (60s). This keeps the session alive across
  restarts. Requirements R1–R7 in the grocery-sync PRD.
- **GHCR publishing.** `.github/workflows/build-release.yml` builds the
  `server/` image and pushes to `ghcr.io/<owner>/ha-alexa-shopping-list-sync`
  using the built-in `GITHUB_TOKEN` (multi-arch: amd64 + arm64). Triggers on push
  to `main`.

## Stack & conventions

- **Python 3** throughout. Deps pinned in `server/requirements.txt` and
  `client/requirements.txt`: `selenium==4.23.1`, `websockets==13.0.1`,
  `requests==2.32.3`. The server's list operations now run on `requests`;
  `selenium` stays pinned for the client login flow and as a vestigial fallback in
  the server image (a clean-up follow-up per `docs/json-api-hardening.md`). No
  package manager beyond `pip`.
- **Server container** (`server/Dockerfile`): Alpine + Chromium +
  chromium-chromedriver (retained for login/fallback; not used by the JSON-API hot
  path). Env: `ASL_CONFIG_PATH=/config/`, `CHROME_DRIVER=/usr/bin/chromedriver`.
  Exposes port 4000.
- No test suite or linter is configured. Validate Python changes with
  `python -m py_compile <files>`.
- Code style matches the surrounding files — plain stdlib, no type-checking
  tooling, print-based logging.

## Git conventions

- Branch prefixes: `feat/`, `fix/`, `chore/`; use `pa/` for initials-prefixed
  branches. Regular merge commits (`--no-ff`), never squash/rebase unless asked.
- Reference issues with `Addresses #NNN`; never use auto-closing keywords.

---

<sub>Last updated: 2026-07-23 · [commit history](https://github.com/patrickabernathy/home-assistant-alexa-shopping-list/commits/main)</sub>
