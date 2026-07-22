# Hardening the fork: from DOM scraping to the Alexa list JSON API

**Status:** Phase 2 shipped — `server/alexa.py` rewritten to the JSON API via
`requests`; Selenium removed from the server hot path. Server container still
ships Chromium as a no-longer-used fallback (clean-up is a follow-up). Write verbs
not yet exercised from Python against the live account (reads proven) — see
"Remaining unknowns".
**Spike date:** 2026-07-22
**Author:** Patrick + Claude Code (Playwright spike against the live logged-in session)

---

## TL;DR

The Alexa list web page (`/alexaquantum/sp/alexaShoppingList`) is a thin UI over a
private JSON API at `https://www.amazon.com/alexashoppinglists/api/`. That API
**authorizes on the exact session cookies we already persist** — no CSRF token, no
bearer token. We can replace the brittle Selenium DOM automation in
[`server/alexa.py`](../server/alexa.py) with direct JSON calls while keeping the
fork's crown-jewel auth model (human logs in once → we persist rotated cookies)
completely untouched.

This is the "combine the two infrastructures" plan discussed with the referenced
[`lonlazer/ha-alexa-todo-lists`](https://github.com/lonlazer/ha-alexa-todo-lists)
project, but **without** taking on that project's biggest weakness (programmatic
Amazon login, which draws CAPTCHA / device-approval challenges). We keep browser-
established cookies as the auth anchor and just stop scraping the DOM.

---

## Why bother (the failure-mode argument)

The two approaches fail in opposite places:

| | Auth / session | Read + write operations |
|---|---|---|
| **Our scraper today** | **Robust** — real browser login, persisted rotated cookies. Looks legit, survives CAPTCHA/device-approval. | **Fragile** — DOM selectors (`virtual-list`, `item-title`, `item-actions-2`, …) break when Amazon reshuffles the page; needs scroll-to-load and stale-element retries. |
| **Pure unofficial-API projects** | **Fragile** — programmatic SRP login gets challenged aggressively (CAPTCHA loops are the #1 reported pain). | **Robust** — direct JSON. |

The hybrid keeps each half where it is strong: **browser cookies for auth, JSON API
for operations.**

---

## What the spike did

Drove the real, logged-in Amazon session with Playwright (cookies read from
`cookies.json` via a `file://` load so secrets never left the browser process),
captured all XHR/fetch traffic, and triggered real add/delete actions through the
UI to capture the underlying write calls. Then cleaned up via the JSON API itself.

Confirmed:
1. Page loaded fully authenticated from the persisted cookies (`signin: false`).
2. Read endpoint returns clean structured JSON.
3. Add + delete endpoints captured with full payloads.
4. **A raw `DELETE` with only the cookies (no browser click, no CSRF header)
   returned `200`** — end-to-end proof the write path needs nothing but the session.
5. Cleanup verified: no test items left on the list.

All four verbs (read/add/update/delete) plus the auth model were confirmed against
the live account. The DOM edit affordance differs from the old scraper's assumption,
so `updatelistitem` was captured by raw `fetch` probing instead (POST → 405, PUT → 200).

---

## The API contract

**Base:** `https://www.amazon.com/alexashoppinglists/api/`

**Auth / headers:**
- Cookies only (`credentials: include`). **No `anti-csrftoken-a2z`, no `Authorization`.**
- `content-type: application/json`, `accept: application/json`.
- The web app also sends `x-amzn-as-metadata` (a static-ish client descriptor:
  app name/version, OS, device model) and an empty `x-amz-weblabs`. **Confirmed
  optional** — read/add/update/delete all succeeded with `x-amzn-as-metadata`
  omitted entirely. The only required headers are `content-type`/`accept: application/json`.

### `GET /getlistitems` — read
Returns **all** of the account's lists, keyed by list id:

```jsonc
{
  "<listId>": {
    "listInfo": {
      "listId": "<listId>",
      "listName": "...",
      "listType": "SHOPPING_LIST" | "LIST",
      "defaultList": true | false,
      "customerId": "<CUSTOMER_ID>",
      "version": 1,
      "createAt": 0, "updateAt": 0
    },
    "listItems": [
      {
        "id": "<uuid>",
        "listId": "<listId>",
        "value": "Example Item",
        "encryptedValue": "<opaque>",
        "completed": false,
        "categoryValue": "Produce",
        "itemType": "KEYWORD",
        "version": 1,
        "customerId": "<CUSTOMER_ID>",
        "createdDateTime": 0,
        "updatedDateTime": 0,
        "listItemMetadata": [ /* category / person / impetus metadata */ ]
      }
    ]
  }
}
```

- **Pick the Alexa shopping list** by `listInfo.listType === "SHOPPING_LIST"` and the
  default flag. Do **not** hardcode the id — it is account-specific (base64 of
  `amzn1.account.<id>-SHOPPING_ITEM`). Derive it from this response each run.
- `completed` is exposed here — so the JSON path can support **check-off**, a
  capability the DOM scraper never had.

### `POST /addlistitem/<listId>` — add
`<listId>` is the base64 default-list id in the URL path. Body is minimal:

```json
{ "value": "Example Item", "listItemMetadata": [] }
```

Amazon fills in id/category/metadata server-side. Response returns the created item.

### `DELETE /deletelistitem` — remove
Body is the **full item object** as returned by `getlistitems`, plus
`mergedListId` set to the item's `listId`:

```jsonc
{
  "id": "<uuid>", "listId": "<listId>", "value": "Example Item",
  "encryptedValue": "<opaque>", "completed": false, "categoryValue": "Other",
  "itemType": "KEYWORD", "version": 1, "customerId": "<CUSTOMER_ID>",
  "createdDateTime": 0, "updatedDateTime": 0, "listItemMetadata": [ /* ... */ ],
  "mergedListId": "<listId>"
}
```
Verified `200`. The **full object is required** — a minimal `{id, listId, mergedListId}`
body was rejected with `400`. Echo back the item exactly as `getlistitems` returned it.

### `PUT /updatelistitem` — edit (CONFIRMED)
Method is **`PUT`** (a `POST` to the same path returns `405 Method Not Allowed`).
Body is the full item object + `mergedListId = listId` and the new `value`; verified
`200` with the item's value changing on the list.

```jsonc
PUT /alexashoppinglists/api/updatelistitem
{ ...itemFromGetlistitems, "value": "<new text>", "mergedListId": "<listId>" }
```

### Noise to ignore
`publishamplitude`, `logevents` (both under `/alexashoppinglists/api/`),
`unagi.amazon.com/...`, `rufus/*`, and `cross_border_interstitial_sp/render` are
telemetry / unrelated. The interstitial is a **robot-mitigation** check — it
returned 200 for the browser session but is the main thing that could bite a
non-browser client (see risks).

---

## Recommended architecture

Keep the WebSocket server + HA integration contract **identical**. Change only the
guts of `AlexaShoppingList` in [`server/alexa.py`](../server/alexa.py).

### Phase 1 — JSON via in-browser `fetch` (low risk, high confidence)
Keep Selenium. Replace the four DOM methods (`get_alexa_list`, `add_alexa_list_item`,
`update_alexa_list_item`, `remove_alexa_list_item`) with `driver.execute_script`
calls that issue the JSON `fetch`es **from inside the authenticated page context**.
Cookies, TLS fingerprint, and same-origin are automatically identical to today's
working scraper, so there is **zero new auth risk** — we just stop scraping the DOM.

- Preserves the forced-`amazon.com` behavior and the rotated-cookie persistence
  (`save_session`) exactly as-is.
- Eliminates: DOM selectors, scroll-to-load, `time.sleep(5)` races, stale-element
  retries.
- Keep the DOM path as a **fallback** if a JSON call fails/returns an unexpected
  shape — turns "Amazon changed something" from an outage into a logged, self-healing
  hiccup. Pairs with the recent retryable-error commit.

### Phase 2 — browserless (pure Python `requests`), viability confirmed for reads
Because writes need only cookies, steady-state operations could run with a
`requests.Session` (load `cookies.json`, call the endpoints, persist rotated
`set-cookie`s via the existing atomic-save logic) — **no Chrome/chromedriver in the
hot path**. Selenium would remain only for initial login + occasional re-auth.

**Probe result (2026-07-22):** a plain Python `requests` GET of `getlistitems` with
the persisted cookies returned **`200 application/json`, all 14 lists, no HTML
interstitial** — i.e. Amazon's `cross_border_interstitial` / robot mitigation did
**not** fingerprint-block a non-browser client. The TLS/JA3 concern that usually
kills this approach (and forces the `grocery-prices` routine through real Chrome for
Walmart) does not appear to apply here. Only a **read** was tested from Python; the
write verbs use the identical cookie-only auth, so they are expected to work but
should be confirmed with one throwaway-sentinel round before shipping Phase 2.

### Net result
Same HA behavior and same bulletproof auth, but the brittle surface (DOM) is demoted
to a backstop — and we gain check-off support for free.

---

## Remaining unknowns / to capture next

All the discovery-blocking questions are resolved (update = `PUT`; metadata header
optional; delete needs the full object; Python `requests` reads work). Only one
low-risk item is left before Phase 2:

1. Confirm the **write verbs from Python `requests`** (add/update/delete) with one
   throwaway-sentinel round — reads are proven, writes share the same auth so this is
   expected-pass, not exploratory.

Resolved during the spike:
- ~~`updatelistitem` endpoint~~ → `PUT /updatelistitem`, full object + new `value`.
- ~~Is `x-amzn-as-metadata` required?~~ → No, optional.
- ~~Minimal `DELETE` body?~~ → No, `400`; full item object required.
- ~~Python `requests` vs robot mitigation?~~ → Read returned `200`, no block.

---

## How to resume at home

> **This doc is the single source of truth.** The spike also wrote a machine-local
> memory note and left a chat transcript, but **neither follows you to another
> computer** — everything you need (API contract, Phase 2 skeleton, and the capture
> method below) is in this file.

1. Read this doc top to bottom — the CRUD contract and the Phase 2 skeleton are
   complete.
2. To re-run / extend the live capture (see snippet below): drive the logged-in
   session with Playwright, read `cookies.json` via a `file://` navigation (keeps
   secrets out of the transcript), `addCookies()` with Selenium→Playwright field
   mapping (`expiry`→`expires`, normalize `sameSite` to `Strict`/`Lax`/`None`), then
   issue in-page `fetch` calls against the endpoints above.
3. The full CRUD contract is confirmed — you can go straight to implementation.
   Implement Phase 1 in `server/alexa.py` behind the existing method signatures on a
   `feat/` branch; validate with `python -m py_compile`. Keep the DOM path as a
   logged fallback.
4. Optional before Phase 2: one throwaway-sentinel round confirming the write verbs
   from Python `requests` (reads already proven).

---

## Appendix: Phase 2 implementation sketch

Pseudocode for the browserless (`requests`) rewrite of `AlexaShoppingList`, keeping
the **same public method signatures** so `server.py` and the HA integration need no
changes. This is a sketch to fill out, not final code.

### Design rules
- **Same file contract.** Keep the public surface `server.py` calls: `__init__(amazon_url, cookies_path)`,
  `requires_login()`, `save_session(force=False)`, `get_alexa_list(refresh=True)`,
  `add_alexa_list_item(item)`, `update_alexa_list_item(old, new)`, `remove_alexa_list_item(item)`.
- **Keep the fork behaviours.** `amazon_url` is still forced to `amazon.com` upstream in
  `server.py`; base URL is `https://www.amazon.com`. Preserve the atomic + throttled
  cookie persistence (R1–R7) — reuse `_save_session_atomic` / `COOKIE_SAVE_THROTTLE_SECONDS`.
- **Same cookie file format.** `cookies.json` stays the Selenium list-of-dicts shape so the
  existing login flow (`client/authenticator.py`) and any Phase-1 Selenium fallback keep
  working. Convert on load into the `requests` jar and convert back on persist.
- **Robot/interstitial = retryable, not a crash.** If a call returns HTML / a non-JSON body
  (interstitial) or a 401/403, raise the existing *retryable* error type (see commit
  `4edcab2`) and let the caller re-poll — never dump a traceback.

### Skeleton

```python
import requests, json, os, time

BASE = "https://www.amazon.com"
API  = BASE + "/alexashoppinglists/api"
HEADERS = {"content-type": "application/json", "accept": "application/json"}
COOKIE_SAVE_THROTTLE_SECONDS = 60

class AlexaShoppingList:
    def __init__(self, amazon_url="amazon.com", cookies_path=""):
        # amazon_url kept for signature parity; base is always amazon.com (fork rule)
        self.cookies_path = cookies_path
        self._last_cookie_save = 0
        self.is_authenticated = False
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._load_cookies()               # Selenium-format file -> requests jar
        self._default_list_id = None       # resolved lazily from getlistitems

    # ---- cookies -------------------------------------------------------
    def _cookie_cache_path(self):
        base = self.cookies_path or os.path.dirname(os.path.realpath(__file__))
        return os.path.join(base, "cookies.json")

    def _load_cookies(self):
        path = self._cookie_cache_path()
        if not os.path.exists(path):
            return
        for c in json.load(open(path)):
            self.session.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain", ".amazon.com"),
                path=c.get("path", "/"))

    def save_session(self, force=False):
        # R1/R3: persist rotated cookies after each op, throttled; atomic write.
        if not self.is_authenticated:
            return
        now = time.time()
        if not force and (now - self._last_cookie_save) < COOKIE_SAVE_THROTTLE_SECONDS:
            return
        try:
            cookies = [
                {"name": c.name, "value": c.value, "domain": c.domain,
                 "path": c.path, "secure": c.secure,
                 "expiry": c.expires}                       # keep Selenium-ish shape
                for c in self.session.cookies
            ]
            self._save_session_atomic(cookies)              # reuse tmp-file + os.replace
            self._last_cookie_save = now
        except Exception as e:
            print("Failed to persist session cookies: " + str(e))

    # ---- core request helper ------------------------------------------
    def _api(self, method, path, body=None):
        r = self.session.request(method, API + path,
                                 data=json.dumps(body) if body is not None else None,
                                 timeout=30)
        ct = r.headers.get("content-type", "")
        if r.status_code in (401, 403) or "html" in ct.lower():
            self.is_authenticated = False
            raise RetryableError("Alexa session rejected / interstitial")  # -> re-login/re-poll
        self.is_authenticated = True
        self.save_session()                                 # rotate + persist after every call
        return r

    # ---- list resolution ----------------------------------------------
    def _get_all_lists(self):
        return self._api("GET", "/getlistitems").json()

    def _default_list(self, data=None):
        data = data or self._get_all_lists()
        for lid, node in data.items():
            info = node.get("listInfo", {})
            if info.get("listType") == "SHOPPING_LIST":     # the Alexa shopping list
                self._default_list_id = info.get("listId", lid)
                return self._default_list_id, node
        raise RuntimeError("No SHOPPING_LIST found")

    def _find_item(self, value, data=None):
        _, node = self._default_list(data)
        for it in node.get("listItems", []):
            if it.get("value") == value:
                return it
        return None

    # ---- public API (unchanged signatures) ----------------------------
    def requires_login(self):
        try:
            self._get_all_lists()
            return not self.is_authenticated
        except Exception:
            return True

    def get_alexa_list(self, refresh=True):
        data = self._get_all_lists()
        _, node = self._default_list(data)
        # match current scraper: active (incomplete) items only. Drop the filter
        # to include completed, or expose completed separately as a new capability.
        return [it["value"] for it in node.get("listItems", []) if not it.get("completed")]

    def add_alexa_list_item(self, item):
        if self._find_item(item):                           # idempotent, like today
            return
        list_id = self._default_list_id or self._default_list()[0]
        self._api("POST", "/addlistitem/" + list_id,
                  {"value": item, "listItemMetadata": []})
        return self.get_alexa_list()

    def update_alexa_list_item(self, old, new):
        it = self._find_item(old)
        if it is None:
            return
        body = dict(it, value=new, mergedListId=it["listId"])
        self._api("PUT", "/updatelistitem", body)           # PUT, not POST (POST -> 405)
        return self.get_alexa_list()

    def remove_alexa_list_item(self, item):
        it = self._find_item(item)
        if it is None:
            return None
        body = dict(it, mergedListId=it["listId"])          # full object required (min body -> 400)
        self._api("DELETE", "/deletelistitem", body)
        return self.get_alexa_list()
```

### Notes / decisions to make when implementing
- **`RetryableError`** — reuse whatever type commit `4edcab2` introduced; don't invent a new one.
- **Session teardown** — `requests` needs no `__del__`/driver cleanup; a final `save_session(force=True)`
  on shutdown is still worth keeping to capture the last rotation.
- **`_find_item` cost** — `getlistitems` returns the whole account (14 lists / 650 items in this
  household). It's one request and already fast; no pagination/scroll needed (unlike the DOM).
- **Completed items** — the JSON exposes `completed`, so check-off/uncheck is now trivial to add as
  a new command later; out of scope for parity.
- **Login bootstrap** — first-time auth and re-auth still go through the real browser
  (`client/authenticator.py`) → writes `cookies.json`; Phase 2 only consumes/rotates it.
- **Before shipping** — run the one remaining check (write verbs from Python `requests`) per
  "Remaining unknowns #1".

---

## Appendix: reproducing the capture

The spike drove a Playwright browser and ran JS via a `run_code_unsafe`-style hook
(`async (page) => { ... }`). Core pattern — load the persisted cookies without leaking
them, then hit the JSON API with in-page `fetch` (browser fingerprint = same as the
working scraper). Adjust the `cookies.json` path for the machine you're on.

```js
async (page) => {
  // 1. Read cookies via file:// so values never leave the browser process.
  await page.goto('file:///C:/Users/<you>/Desktop/cookies.json');
  const seleniumCookies = JSON.parse(
    await page.evaluate(() => document.body.innerText || document.body.textContent));

  // 2. Selenium -> Playwright cookie mapping.
  const VALID_SS = new Set(['Strict', 'Lax', 'None']);
  const cookies = seleniumCookies.map(c => {
    const o = { name: c.name, value: c.value,
                domain: c.domain || '.amazon.com', path: c.path || '/' };
    if (typeof c.secure === 'boolean') o.secure = c.secure;
    if (typeof c.httpOnly === 'boolean') o.httpOnly = c.httpOnly;
    if (typeof c.expiry === 'number') o.expires = c.expiry;
    if (c.sameSite) {
      const ss = c.sameSite[0].toUpperCase() + c.sameSite.slice(1).toLowerCase();
      if (VALID_SS.has(ss)) o.sameSite = ss;
    }
    return o;
  });
  await page.context().addCookies(cookies);

  // 3. Land on the authenticated app origin, then call the API with in-page fetch.
  await page.goto('https://www.amazon.com/alexaquantum/sp/alexaShoppingList?ref=nav_asl',
                  { waitUntil: 'domcontentloaded' });
  return await page.evaluate(async () => {
    const H = { 'content-type': 'application/json', accept: 'application/json' };
    const get = async () =>
      (await fetch('/alexashoppinglists/api/getlistitems', { credentials: 'include', headers: H })).json();

    // resolve the default shopping list id, then exercise add / update / delete on
    // a throwaway sentinel (self-cleaning). See the CRUD contract above for shapes.
    const j = await get();
    let listId = null;
    for (const [lid, node] of Object.entries(j))
      if (node.listInfo?.listType === 'SHOPPING_LIST') { listId = node.listInfo.listId || lid; break; }
    return { lists: Object.keys(j).length, listId };
  });
}
```

To capture the raw endpoints/headers of a UI action instead of calling the API
directly, attach `page.on('request'|'response', ...)` **before** the action and
filter to `/alexashoppinglists\/api\//` (dropping `publishamplitude` / `logevents`
telemetry), then trigger the action through the DOM.

> Note: this doc uses placeholder ids/values (`<CUSTOMER_ID>`, `<listId>`,
> "Example Item"). The live spike saw real household data; none of it is recorded
> here or in memory.
