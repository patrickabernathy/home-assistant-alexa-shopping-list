#!/usr/bin/env python3

import asyncio
import websockets
import json
import logging
import signal
import os
from alexa import AlexaShoppingList, NotAuthenticatedError
import time

logger = logging.getLogger("asl.server")

clients = set()

alexa = None
# All commands that touch the shared Alexa instance / cookies.json serialize on
# this lock, so concurrent clients can't interleave operations.
alexa_lock = asyncio.Lock()

# ============================================================
# Helpers


def _time_now():
    return int(time.time())

# ============================================================
# Config


def _config_path():
    return os.environ.get(
        "ASL_CONFIG_PATH", 
        os.path.dirname(os.path.realpath(__file__))
    )


def _load_config():
    global config
    if os.path.exists(os.path.join(_config_path(), 'config.json')):
        with open(os.path.join(_config_path(), 'config.json'), 'r') as file:
            config = json.load(file)
            return
    config = {}


def _save_config():
    with open(os.path.join(_config_path(), 'config.json'), 'w') as file:
        json.dump(config, file)


def _get_config_value(key, default=None):
    if key in config.keys():
        return config[key]
    return default


def _set_config_value(key, new_value=None):
    logger.info("Set config value `%s` = %s", key, new_value)
    global config
    if new_value != None:
        config[key] = new_value
    else:
        config.pop(key, None)
    _save_config()


async def _cmd_config_valid():
    return os.path.exists(
        os.path.join(_config_path(), 'config.json')
    ), None


async def _cmd_config_set(args):
    _set_config_value(args['key'], args['value'])
    return True, None


async def _cmd_config_get(args):
    return _get_config_value(args['key']), None

# ============================================================
# Alexa


def _start_alexa():
    # The instance is long-lived: with requests (unlike the old crash-prone
    # Chrome) there is no reason to tear it down per command, and reusing it
    # avoids re-reading cookies.json on every operation.
    global alexa

    if alexa is None:
        # Force amazon.com regardless of any stored config value. This is a
        # personal US fork: a co.uk (or other regional) URL silently breaks the
        # Alexa shopping-list flow, and honoring the configurable value cost real
        # debugging time. Hardcoding removes that whole class of misconfiguration.
        alexa = AlexaShoppingList(
            "amazon.com",
            _config_path()
        )

    return alexa


def _stop_alexa():
    # Drop the instance. Deleting it triggers a final forced cookie save
    # (alexa.__del__); the next _start_alexa() re-reads cookies.json, so this is
    # also how new cookies (login) or a cleared session (reset) get picked up.
    global alexa

    if alexa is not None:
        del alexa

    alexa = None

# ============================================================
# API


async def _cmd_reset():
    # Stop first: the instance's teardown force-saves cookies, which would
    # otherwise resurrect cookies.json right after we delete it.
    _stop_alexa()

    purge_files = ['config.json', 'cookies.json']
    for filename in purge_files:
        file_path = os.path.join(_config_path(), filename)
        if os.path.exists(file_path):
            os.remove(file_path)

    _load_config()
    return True, None


async def _cmd_is_authenticated():
    recent = _get_config_value('auth_checked_time', 0)
    time_diff = _time_now() - recent

    if time_diff < 86400:
        logger.debug("Authenticated: Yes (cached, checked %ds ago)", time_diff)
        return True, None

    instance = _start_alexa()

    if await asyncio.to_thread(instance.requires_login) == True:
        logger.warning("Authenticated: No (live probe)")
        # Drop the instance so a subsequent login's cookies.json is re-read.
        _stop_alexa()
        return False, None

    logger.info("Authenticated: Yes (live probe)")
    _set_config_value("auth_checked_time", _time_now())
    return True, None


async def _cmd_login(args):
    logger.info("Attempting login with %d received cookies", len(args.get('session') or []))

    # Drop the current instance (and its in-memory jar) before writing the new
    # cookies, then invalidate the 24h cache so the check below probes live.
    _stop_alexa()

    with open(os.path.join(_config_path(), 'cookies.json'), 'w') as file:
        json.dump(args['session'], file)

    _set_config_value('auth_checked_time', None)
    return await _cmd_is_authenticated()


# The list commands run without a pre-flight requires_login() probe: that probe
# was a full getlistitems round-trip before every operation. A dead session now
# surfaces as NotAuthenticatedError from the operation itself, which the message
# handler turns into the same "Not authenticated" error the probe produced.
# The blocking requests calls run in a thread so the event loop (ping, other
# clients) stays responsive.

async def _cmd_get_shopping_list():
    instance = _start_alexa()
    return await asyncio.to_thread(instance.get_alexa_list), None


async def _cmd_get_add_shopping_list_item(args):
    instance = _start_alexa()
    return await asyncio.to_thread(instance.add_alexa_list_item, args['item']), None


async def _cmd_get_update_shopping_list_item(args):
    instance = _start_alexa()
    return await asyncio.to_thread(instance.update_alexa_list_item, args['old'], args['new']), None


async def _cmd_get_remove_shopping_list_item(args):
    instance = _start_alexa()
    return await asyncio.to_thread(instance.remove_alexa_list_item, args['item']), None

# ============================================================
# Main handler


async def _route_command(command, arguments={}):

    # Config
    if command == "config_valid":
        return await _cmd_config_valid()
    if command == "config_set":
        return await _cmd_config_set(arguments)
    if command == "config_get":
        return await _cmd_config_get(arguments)

    # Misc — kept outside the lock so ping stays responsive mid-operation.
    if command == "ping":
        return "pong", None
    if command == "shutdown":
        # Schedule the shutdown so this response still reaches the client.
        asyncio.create_task(_shutdown_server())
        return True, None

    # Everything below touches the shared Alexa instance / cookies.json.
    async with alexa_lock:
        # Authentication
        if command == "authenticated":
            return await _cmd_is_authenticated()
        if command == "login":
            return await _cmd_login(arguments)
        if command == "reset":
            return await _cmd_reset()

        # Shopping list
        if command == "get_list":
            return await _cmd_get_shopping_list()
        if command == "add_item":
            return await _cmd_get_add_shopping_list_item(arguments)
        if command == "update_item":
            return await _cmd_get_update_shopping_list_item(arguments)
        if command == "remove_item":
            return await _cmd_get_remove_shopping_list_item(arguments)


def _peer_name(websocket):
    addr = getattr(websocket, "remote_address", None)
    if isinstance(addr, tuple) and len(addr) >= 2:
        return str(addr[0]) + ":" + str(addr[1])
    return "unknown"


def _describe_args(command, arguments):
    # One-line argument summary. Never dump login args — they are the session
    # cookies.
    if not arguments:
        return ""
    if command == "login":
        return " (session redacted)"
    return " " + json.dumps(arguments)


def _describe_result(result):
    # Lists get logged in full so what each caller (grocery-sync worker, HA
    # integration, CLI) was actually served can be compared across log lines.
    if isinstance(result, list):
        return str(len(result)) + " items " + json.dumps(result)
    return json.dumps(result)


def _log_command(peer, command, arguments, response):
    if command == "ping":
        logger.debug("%s ping", peer)
        return

    summary = command + _describe_args(command, arguments)
    if response.get("error"):
        logger.warning("%s %s -> error: %s", peer, summary, response["error"])
    else:
        logger.info("%s %s -> %s", peer, summary, _describe_result(response.get("result")))


async def _process_command(websocket, path):
    peer = _peer_name(websocket)
    clients.add(websocket)
    logger.debug("%s connected", peer)
    try:
        async for message in websocket:
            response = {"result": None, "error": None}
            command = None
            arguments = None

            try:
                data = json.loads(message)
                command = data.get('command')
                arguments = data.get('args')

                results = await _route_command(command, arguments)

                if results is not None and len(results) == 2:
                    response = {
                        "result": results[0],
                        "error": results[1]
                    }
                else:
                    response['error'] = 'Unknown command'

            except json.JSONDecodeError:
                response = {"result": None, "error": "Invalid JSON"}
            except NotAuthenticatedError as e:
                # The session cookies were rejected mid-operation. Return the same
                # "Not authenticated" error the old pre-flight probe produced (the
                # grocery-sync worker keys off it), and invalidate the 24h auth
                # cache so the worker's next `authenticated` poll goes false
                # immediately instead of reading the stale cached yes.
                logger.warning("Command '%s' rejected by Amazon: %r", command, e)
                _set_config_value('auth_checked_time', None)
                _stop_alexa()
                response = {"result": None, "error": "Not authenticated"}
            except Exception as e:
                # A transient failure — e.g. a network blip against the Amazon
                # API — must not take down the connection with a giant traceback.
                # Log one concise line, reset the instance, and return a clean
                # "busy" error so the worker just retries on its next pulse.
                logger.warning("Command '%s' failed, returning retryable error: %r", command, e)
                _stop_alexa()
                response = {
                    "result": None,
                    "error": "Bridge starting or busy, please retry"
                }

            _log_command(peer, command, arguments, response)
            await websocket.send(json.dumps(response))
    finally:
        clients.discard(websocket)
        logger.debug("%s disconnected", peer)

# ============================================================
# Start/Stop


async def _shutdown_server():
    # Force a final cookie save so the freshest rotation survives the restart.
    _stop_alexa()
    for ws in list(clients):
        await ws.close()
    server.close()
    await server.wait_closed()


def _handle_stop_signal():
    logger.info("Shutting down server...")
    asyncio.create_task(_shutdown_server())


def _setup_logging():
    # INFO gives one line per command (heartbeat + served payloads); set
    # ASL_LOG_LEVEL=DEBUG for pings, connections and raw Amazon API calls.
    # The env level is applied to the app's `asl` namespace only — putting the
    # root logger at DEBUG would also unleash frame-level spam from the
    # websockets/urllib3 libraries.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("asl").setLevel(
        os.environ.get("ASL_LOG_LEVEL", "INFO").upper()
    )


async def main():
    _setup_logging()
    _load_config()

    global server
    listen_addr = None
    listen_port = int(_get_config_value('listen_port', 4000))
    server = await websockets.serve(_process_command, listen_addr, listen_port)

    logger.info("Alexa Shopping List server started on port %d", listen_port)

    # SIGTERM is what `docker stop` sends; SIGINT covers Ctrl-C. Registered on
    # the running loop (the old sync handler called asyncio.run() inside the
    # running loop, which raises RuntimeError and never shut anything down).
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_stop_signal)
        except NotImplementedError:
            # Windows (dev only): no loop signal handlers; the sync fallback
            # still runs on the main thread, where the loop is running.
            signal.signal(sig, lambda s, f: _handle_stop_signal())

    await server.wait_closed()

# ============================================================


if __name__ == "__main__":
    asyncio.run(main())