#!/usr/bin/env python3
"""Keep-alive script to auto-rotate and refresh Gemini Web cookies.

Runs silently via cron every 20-30 minutes with randomized sleep jitter
to avoid detection by Google bot-protection systems.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import random
import sys
import time

# Ensure gemini-web plugin is importable
PLUGIN_DIR = pathlib.Path("~/.hermes/plugins/gemini-web").expanduser()
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import gemini_web_plugin as gwp
from gemini_webapi import GeminiClient
from gemini_webapi.utils.rotate_1psidts import rotate_1psidts


async def main() -> int:
    # 1. Anti-fingerprinting random jitter (0 to 180 seconds sleep)
    # Skipped only if run with --now
    if "--now" not in sys.argv:
        jitter = random.uniform(10.0, 180.0)
        time.sleep(jitter)

    cookies_path = pathlib.Path("~/.hermes/gemini_cookies.json").expanduser()
    if not cookies_path.exists():
        return 0

    try:
        data = json.loads(cookies_path.read_text())
    except Exception:
        return 1

    updated_any = False
    for acct in data:
        psid = acct.get("__Secure-1PSID", "").strip()
        psidts = acct.get("__Secure-1PSIDTS", "").strip()
        label = acct.get("label", "unnamed")
        if not psid:
            continue

        client = None
        try:
            # Small jitter between multiple accounts
            await asyncio.sleep(random.uniform(2.0, 6.0))
            client = GeminiClient(secure_1psid=psid, secure_1psidts=psidts)
            await client.init(timeout=15)

            live_session = getattr(client, "_live_client", None)
            if live_session:
                new_ts = await rotate_1psidts(live_session, verbose=False)
                if new_ts and new_ts != psidts:
                    acct["__Secure-1PSIDTS"] = new_ts
                    updated_any = True
        except Exception:
            pass
        finally:
            if client and hasattr(client, "_live_client") and client._live_client:
                try:
                    await client._live_client.close()
                except Exception:
                    pass

    if updated_any:
        try:
            tmp = cookies_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.chmod(0o600)
            tmp.replace(cookies_path)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
