#!/usr/bin/env python3

# Phase 2 of docs/json-api-hardening.md: this driver talks to Amazon's private
# Alexa-list JSON API (https://www.amazon.com/alexashoppinglists/api/) with a
# plain requests.Session instead of scraping the DOM with Selenium. The API
# authorizes purely on the session cookies we already persist — no CSRF token,
# no bearer token — so the fork's crown-jewel auth model is untouched:
#
#   * The *client* (client/authenticator.py) still does the one-time real-browser
#     login and ships cookies.json to the server. Nothing here logs in.
#   * We consume cookies.json, and re-persist the cookies Amazon rotates on every
#     response (R1-R7: atomic write + os.replace, throttled), keeping the session
#     alive across restarts exactly as before.
#
# The WebSocket server (server.py) and the HA integration are unchanged: the
# public method signatures below are identical to the old Selenium driver, and
# any failure (rejected session / interstitial / bad response) raises, which
# server.py catches and turns into the clean "please retry" error (commit
# 4edcab2) — never a traceback.

import requests
import time
import json
import logging
import os

logger = logging.getLogger("asl.alexa")

# Per-request network timeout (seconds).
REQUEST_TIMEOUT = 30

# Persist rotated session cookies at most once per this many seconds (R3).
# The worker polls ~every 90s, so this is naturally ~1 write per cycle.
COOKIE_SAVE_THROTTLE_SECONDS = 60

# Present a browser-like UA rather than the default python-requests one, matching
# the User-Agent the old Selenium scraper used, to stay clear of robot mitigation.
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


class NotAuthenticatedError(RuntimeError):
    # The session cookies were rejected (401/403) or an interstitial was served.
    # A human re-login through the client is required; retrying won't help.
    pass


class AlexaShoppingList:

    def __init__(self, amazon_url: str = "amazon.com", cookies_path: str = ""):
        # amazon_url is kept for signature parity. server.py forces "amazon.com"
        # (the fork rule), so the base is https://www.amazon.com in practice.
        self.amazon_url = amazon_url
        self.cookies_path = cookies_path
        self.base_url = "https://www." + amazon_url
        self.api_url = self.base_url + "/alexashoppinglists/api"

        self._last_cookie_save = 0
        self.is_authenticated = False
        self._default_list_id = None

        self.session = requests.Session()
        self.session.headers.update({
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": USER_AGENT,
        })
        self._load_cookies()


    def __del__(self):
        # requests needs no driver teardown, but force a final persist so the
        # freshest cookie rotation is never lost on a clean shutdown.
        try:
            self.save_session(force=True)
        except Exception:
            pass

    # ============================================================
    # Helpers


    def _get_file_location(self):
        return os.path.dirname(os.path.realpath(__file__))

    def _is_debug_mode(self):
        return os.environ.get("ALEXA_SHOPPING_LIST_DEBUG", "0") == "1"

    # ============================================================
    # Cookies


    def _cookie_cache_path(self):
        if self.cookies_path != "":
            return os.path.join(self.cookies_path, "cookies.json")
        return os.path.join(self._get_file_location(), "cookies.json")


    def _load_cookies(self):
        # Load the Selenium-format cookies.json (list of dicts, as produced by the
        # client login flow) into the requests cookie jar.
        path = self._cookie_cache_path()
        if not os.path.exists(path):
            return

        with open(path, 'r') as file:
            cookies = json.load(file)

        logger.info("Loaded %d cookies from %s", len(cookies), path)

        for cookie in cookies:
            kwargs = {
                "domain": cookie.get("domain", ".amazon.com"),
                "path": cookie.get("path", "/"),
            }
            if "secure" in cookie:
                kwargs["secure"] = bool(cookie["secure"])
            if isinstance(cookie.get("expiry"), (int, float)):
                kwargs["expires"] = int(cookie["expiry"])

            rest = {}
            if cookie.get("httpOnly"):
                rest["HttpOnly"] = ""
            if cookie.get("sameSite"):
                rest["SameSite"] = cookie["sameSite"]
            if rest:
                kwargs["rest"] = rest

            self.session.cookies.set(cookie["name"], cookie["value"], **kwargs)


    def _serialize_cookies(self):
        # Convert the requests jar back to the Selenium list-of-dicts shape so the
        # file format stays compatible with the client login flow.
        out = []
        for c in self.session.cookies:
            cookie = {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "secure": bool(c.secure),
            }
            if c.expires is not None:
                cookie["expiry"] = int(c.expires)
            if c.has_nonstandard_attr("HttpOnly"):
                cookie["httpOnly"] = True
            same_site = c.get_nonstandard_attr("SameSite")
            if same_site:
                cookie["sameSite"] = same_site
            out.append(cookie)
        return out


    def _save_session_atomic(self, cookies):
        # R2 — write to a temp file then os.replace() onto cookies.json, so a
        # crash mid-write can never corrupt the persisted session.
        path = self._cookie_cache_path()
        tmp_path = path + ".tmp"
        with open(tmp_path, 'w') as file:
            json.dump(cookies, file)
        os.replace(tmp_path, path)


    def save_session(self, force: bool = False):
        # Persist the session cookies Amazon rotates on each authenticated
        # request. Called after every list operation (R1) so the freshest
        # rotation survives an unclean container/Chrome restart — not just a
        # clean shutdown.
        if not getattr(self, "is_authenticated", False):
            return

        now = time.time()
        # R3 — throttle: at most one write per COOKIE_SAVE_THROTTLE_SECONDS.
        if not force and (now - self._last_cookie_save) < COOKIE_SAVE_THROTTLE_SECONDS:
            return

        # R4 — a read/write failure must never abort the in-flight command;
        # catch and log it.
        try:
            cookies = self._serialize_cookies()
            self._save_session_atomic(cookies)
            self._last_cookie_save = now
            logger.info("Saved %d cookies to %s", len(cookies), self._cookie_cache_path())
        except Exception as e:
            logger.error("Failed to persist session cookies: %s", e)

    # ============================================================
    # API request helper


    def _api(self, method: str, path: str, body=None):
        url = self.api_url + path
        data = json.dumps(body) if body is not None else None

        try:
            response = self.session.request(
                method, url, data=data, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as e:
            self.is_authenticated = False
            logger.warning("Alexa API %s %s failed: %s", method, path, e)
            raise RuntimeError("Alexa API request failed: " + str(e))

        logger.debug("Alexa API %s %s -> %d", method, path, response.status_code)

        content_type = response.headers.get("content-type", "").lower()

        # A 401/403, or an HTML body served in place of the API, means the session
        # was rejected or a robot-mitigation interstitial was returned. Treat it as
        # not-authenticated and raise — server.py turns this into the clean
        # "please retry" signal and the worker re-polls / prompts a re-login.
        if response.status_code in (401, 403) or "html" in content_type:
            self.is_authenticated = False
            logger.warning(
                "Alexa API %s %s rejected (status %d, content-type %s)",
                method, path, response.status_code, content_type
            )
            raise NotAuthenticatedError(
                "Alexa session rejected or interstitial served (status "
                + str(response.status_code) + ")"
            )

        if response.status_code >= 400:
            raise RuntimeError(
                "Alexa API " + method + " " + path
                + " returned " + str(response.status_code)
            )

        self.is_authenticated = True
        # Persist the cookies Amazon rotated on this response (R1, throttled).
        self.save_session()
        return response

    # ============================================================
    # List resolution


    def _get_all_lists(self):
        # getlistitems returns every list on the account keyed by list id.
        return self._api("GET", "/getlistitems").json()


    def _default_list(self, data=None):
        # Pick the Alexa shopping list by listType == SHOPPING_LIST, preferring the
        # default-flagged one. The id is account-specific, so derive it each run
        # rather than hardcoding (see the API contract in the hardening doc).
        data = data if data is not None else self._get_all_lists()

        shopping_lists = []
        for lid, node in data.items():
            info = node.get("listInfo", {})
            if info.get("listType") == "SHOPPING_LIST":
                shopping_lists.append((info.get("listId", lid), node, info.get("defaultList", False)))

        if not shopping_lists:
            raise RuntimeError("No Alexa SHOPPING_LIST found in account lists")

        for lid, node, is_default in shopping_lists:
            if is_default:
                self._default_list_id = lid
                return lid, node

        lid, node, _ = shopping_lists[0]
        self._default_list_id = lid
        return lid, node


    def _find_item(self, value: str, data=None):
        # Return the full item object as getlistitems returned it (delete/update
        # need to echo it back verbatim), or None if not present.
        _, node = self._default_list(data)
        for item in node.get("listItems", []):
            if item.get("value") == value:
                return item
        return None


    def _active_items(self, node):
        # Match the old scraper: active (incomplete) items only. The JSON also
        # exposes `completed`, so check-off support could be added later.
        return [
            item["value"]
            for item in node.get("listItems", [])
            if not item.get("completed")
        ]

    # ============================================================
    # Authentication


    def requires_login(self):
        # A single getlistitems call doubles as the auth probe: success => the
        # cookies are still good; any failure => re-login required.
        try:
            self._get_all_lists()
            return not self.is_authenticated
        except Exception:
            return True

    # ============================================================
    # Alexa lists


    def get_alexa_list(self, refresh: bool = True):
        # refresh is kept for signature parity; the JSON read is always live.
        data = self._get_all_lists()
        _, node = self._default_list(data)
        return self._active_items(node)


    def add_alexa_list_item(self, item: str):
        # One getlistitems serves both the idempotency check and the no-op return;
        # only a real change costs a second read (which also verifies the write).
        data = self._get_all_lists()
        list_id, node = self._default_list(data)

        # Idempotent, like the old scraper: if it's already on the list, do nothing.
        if self._find_item(item, data) is not None:
            return self._active_items(node)

        self._api(
            "POST", "/addlistitem/" + list_id,
            {"value": item, "listItemMetadata": []}
        )
        return self.get_alexa_list()


    def update_alexa_list_item(self, old: str, new: str):
        data = self._get_all_lists()
        _, node = self._default_list(data)

        existing = self._find_item(old, data)
        if existing is None:
            return self._active_items(node)

        # PUT, not POST (POST -> 405). Full item object + new value + mergedListId.
        body = dict(existing, value=new, mergedListId=existing["listId"])
        self._api("PUT", "/updatelistitem", body)
        return self.get_alexa_list()


    def remove_alexa_list_item(self, item: str):
        data = self._get_all_lists()
        _, node = self._default_list(data)

        existing = self._find_item(item, data)
        if existing is None:
            return self._active_items(node)

        # The full item object is required (a minimal body -> 400); echo it back
        # exactly as getlistitems returned it, plus mergedListId.
        body = dict(existing, mergedListId=existing["listId"])
        self._api("DELETE", "/deletelistitem", body)
        return self.get_alexa_list()

    # ============================================================
