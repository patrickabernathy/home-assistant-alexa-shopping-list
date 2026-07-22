# Hardening the fork: from DOM scraping to the Alexa list JSON API

**Status:** discovery spike complete — full CRUD contract confirmed · implementation not started
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

1. Read the memory note `alexa-shopping-list-json-api.md` (auto-loads via
   `MEMORY.md`) and this doc.
2. If iterating on the live API, the spike method is: drive the logged-in session
   with Playwright, read `cookies.json` via a `file://` navigation (keeps secrets out
   of the transcript), `context.addCookies()` with Selenium→Playwright field mapping
   (`expiry`→`expires`, normalize `sameSite`), then use in-page `fetch` against the
   endpoints above. The full working snippets are in the chat transcript for this
   spike.
3. The full CRUD contract is confirmed — you can go straight to implementation.
   Implement Phase 1 in `server/alexa.py` behind the existing method signatures on a
   `feat/` branch; validate with `python -m py_compile`. Keep the DOM path as a
   logged fallback.
4. Optional before Phase 2: one throwaway-sentinel round confirming the write verbs
   from Python `requests` (reads already proven).

> Note: this doc uses placeholder ids/values (`<CUSTOMER_ID>`, `<listId>`,
> "Example Item"). The live spike saw real household data; none of it is recorded
> here or in memory.
