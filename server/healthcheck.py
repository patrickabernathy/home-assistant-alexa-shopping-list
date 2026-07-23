#!/usr/bin/env python3

# Docker HEALTHCHECK probe: a real `ping` command through the WebSocket router,
# exercising the event loop and command dispatch — a bare TCP connect (the old
# probe) only proved the listener existed, and the websockets server logged each
# one as a rejected 400 handshake every interval.

import asyncio
import json
import sys

import websockets


async def main():
    async with websockets.connect(
        "ws://127.0.0.1:4000", open_timeout=5, close_timeout=5
    ) as ws:
        await ws.send(json.dumps({"command": "ping"}))
        response = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))

    if response.get("result") != "pong":
        sys.exit(1)


asyncio.run(main())
