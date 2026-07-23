# Alexa Shopping List to Home Assistant Synchroniser

[![Build](https://github.com/patrickabernathy/home-assistant-alexa-shopping-list/actions/workflows/build-release.yml/badge.svg)](https://github.com/patrickabernathy/home-assistant-alexa-shopping-list/actions/workflows/build-release.yml)
[![License](https://img.shields.io/github/license/patrickabernathy/home-assistant-alexa-shopping-list.svg)](LICENSE)

![Python](https://img.shields.io/badge/python-3-3776ab?logo=python)
![Selenium](https://img.shields.io/badge/Selenium-4-43b02a?logo=selenium)
![Home Assistant](https://img.shields.io/badge/Home_Assistant-custom_component-41bdf5?logo=home-assistant)
![Docker](https://img.shields.io/badge/Docker-2496ed?logo=docker)

This is a custom component for Home Assistant, which allows you to synchronise your Alexa Shopping List with the Home Assistant shopping list.

**This works even though they cut off third party access to the shopping lists in Summer 2024**

This fork combines two approaches to that problem (see [About this fork](#about-this-fork)): the upstream project's browser-based login, and the private JSON API that Amazon's own list page uses. A human logs in once through a real browser (robust against CAPTCHA / device-approval), and from then on the server talks to Alexa's list over that JSON API with the persisted session cookies — no DOM scraping in the steady state.

There are three parts:

**The Server**

This is a small python application that accesses your Alexa shopping list. It can read what is on the list, add things to it, update items and remove them.

In this fork it drives Amazon's private list JSON API directly with `requests` (the same endpoints the Alexa list web page calls), authenticated by the persisted session cookies. Selenium/Chromium is kept only for the initial login and occasional re-auth, not for day-to-day list operations — see [docs/json-api-hardening.md](docs/json-api-hardening.md) for the full contract and rationale.

The server runs on your home assistant device, or a different server on your network.

**The Client**

In theory, you should rarely need to use the client. You need it to get the server set up. The client is like the remote control for the server.

The client runs on your desktop computer or laptop, so you can talk to the server more easily.

**The Custom Component**

This is the part you add to your Home Assistant installation. It talks with the server and the two work together to make sure your shopping lists on both HA and Alexa are kept in sync.


## About this fork

This is a personal fork of [madmachinations/home-assistant-alexa-shopping-list](https://github.com/madmachinations/home-assistant-alexa-shopping-list), maintained for a self-hosted (US) deployment. All credit for the original project goes to the upstream author.

### A merge of two approaches

There are two known ways to reach the Alexa list after Amazon closed the official API in Summer 2024, and each is strong exactly where the other is weak:

- The **upstream project** logs in through a real browser and *scrapes the list web page with Selenium*. Its **auth is robust** (a genuine browser login survives CAPTCHA / device-approval and persists cookies), but its **operations are fragile** — the DOM selectors, scroll-to-load and stale-element retries break whenever Amazon reshuffles the page.
- **Pure unofficial-API projects** (e.g. [lonlazer/ha-alexa-todo-lists](https://github.com/lonlazer/ha-alexa-todo-lists)) call Amazon's private list JSON API directly. Their **operations are robust** (clean JSON CRUD), but their **auth is fragile** — programmatic login gets challenged aggressively.

This fork keeps each half where it is strong: **browser-established cookies for auth, the JSON API for operations.** A human logs in once through the real browser (as upstream does), and the server then drives the list over the same private JSON API the web page uses — authorized purely by the persisted session cookies, with no CSRF or bearer token needed. Selenium is demoted to a login/re-auth tool and a backstop; it is out of the day-to-day hot path. The full spike, API contract and migration plan are in [docs/json-api-hardening.md](docs/json-api-hardening.md).

### Other differences from upstream

- **JSON API instead of DOM scraping** — as above: list reads/adds/updates/removes go through Amazon's `alexashoppinglists/api` endpoints via `requests`, not Selenium DOM automation. This also makes item **check-off** available for free, since the JSON exposes each item's `completed` flag.
- **Persistent session cookies** — the server re-saves the Amazon session cookies it rotates after every list operation (atomically, throttled to ~once a minute), so the login survives container/browser restarts instead of dying every couple of days.
- **Forced `amazon.com`** — the server always uses `amazon.com`, ignoring any stored `amazon_url` config value. A regional URL (e.g. `amazon.co.uk`) silently breaks the shopping-list flow for this deployment.
- **Image published to GHCR** — the build workflow pushes to `ghcr.io/patrickabernathy/ha-alexa-shopping-list-sync` instead of Docker Hub. Point your Docker/compose deployment at that image rather than the upstream `madmachinations/...` one.

The upstream wiki guides linked below still apply for general installation and setup.

## Installation steps

You can find the installation guide on the wiki here:

https://github.com/madmachinations/home-assistant-alexa-shopping-list/wiki/Installation

## Setting up a development environment

You can find the development environment setup guide on the wiki here:

https://github.com/madmachinations/home-assistant-alexa-shopping-list/wiki/Development-environment

## Troubleshooting and help

If you get stuck or hit a problem, please read the troubleshooting steps first:

https://github.com/madmachinations/home-assistant-alexa-shopping-list/wiki/Troubleshooting-and-help


## Help out

I would appreciate any help from anyone for testing and further development on various fixes and improvements.

If you are not technical, there are other ways to help. Such as identifying duplicate issues, or helping other people in the community support discussion board here:

https://github.com/madmachinations/home-assistant-alexa-shopping-list/discussions/categories/community-support

---

<sub>Last updated: 2026-07-23 · [commit history](https://github.com/patrickabernathy/home-assistant-alexa-shopping-list/commits/main)</sub>
