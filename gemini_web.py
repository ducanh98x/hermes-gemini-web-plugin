"""Gemini Web plugin — multi-account image gen & multimodal chat via web cookies.

Config: env `GEMINI_WEB_COOKIES` = JSON array, or file `~/.hermes/gemini_cookies.json`.
Each entry: {"label": "acct1", "__Secure-1PSID": "...", "__Secure-1PSIDTS": "..."}

Tools:
  gemini_web_generate_image(prompt, aspect_ratio="landscape",
                            reference_images=None, output_dir=None,
                            timeout=180)
  gemini_web_chat(prompt, files=None, timeout=180)
  gemini_web_accounts()
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import pathlib
import re
import tempfile
import threading
import time
import urllib.request
import uuid
from typing import Any

from gemini_webapi import GeminiClient, set_log_level  # type: ignore

set_log_level("WARNING")

_DEFAULT_OUTPUT_DIR = pathlib.Path("~/.hermes/cache/images").expanduser()
_COOKIE_FILE_DEFAULT = pathlib.Path("~/.hermes/gemini_cookies.json").expanduser()
_UA = "Mozilla/5.0 (X11; Linux x86_64) HermesAgent-GeminiWeb/0.1"
_COOLDOWN_SEC = 300  # 5 min after a failure
_ASPECT_HINTS = {
    "square": "Aspect ratio: 1:1 (square).",
    "landscape": "Aspect ratio: 16:9 (landscape).",
    "portrait": "Aspect ratio: 9:16 (portrait).",
    "wide": "Aspect ratio: 21:9 (ultra-wide).",
}


# ---- Config ----

def load_accounts() -> list[dict[str, str]]:
    raw = os.environ.get("GEMINI_WEB_COOKIES", "").strip()
    if not raw and _COOKIE_FILE_DEFAULT.exists():
        raw = _COOKIE_FILE_DEFAULT.read_text().strip()
    if not raw:
        raise RuntimeError(
            "No Gemini Web cookies found. Set GEMINI_WEB_COOKIES env or create "
            f"{_COOKIE_FILE_DEFAULT}"
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"GEMINI_WEB_COOKIES is not valid JSON: {e}") from e
    if not isinstance(data, list) or not data:
        raise RuntimeError("GEMINI_WEB_COOKIES must be a non-empty JSON array")
    accounts = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise RuntimeError(f"account[{i}] must be an object")
        psid = entry.get("__Secure-1PSID") or entry.get("psid")
        psidts = entry.get("__Secure-1PSIDTS") or entry.get("psidts")
        if not psid or not psidts:
            raise RuntimeError(f"account[{i}] missing __Secure-1PSID or __Secure-1PSIDTS")
        accounts.append({
            "label": entry.get("label") or f"acct{i+1}",
            "psid": psid.strip(),
            "psidts": psidts.strip(),
        })
    return accounts


# ---- Account rotator (persistent clients) ----

class _AccountPool:
    def __init__(self):
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._clients: dict[str, GeminiClient] = {}
        self._state: dict[str, dict[str, Any]] = {}  # label -> {last_used, cooldown_until, error_count, ok_count}
        self._accounts: list[dict[str, str]] = []

    def _ensure_loop(self):
        if self._loop and self._loop.is_running():
            return
        loop = asyncio.new_event_loop()
        def run():
            asyncio.set_event_loop(loop)
            loop.run_forever()
        t = threading.Thread(target=run, daemon=True, name="gemini-web-loop")
        t.start()
        self._loop = loop
        self._loop_thread = t

    def _submit(self, coro):
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result()

    def refresh(self):
        with self._lock:
            self._accounts = load_accounts()
            for a in self._accounts:
                self._state.setdefault(a["label"], {
                    "last_used": 0.0, "cooldown_until": 0.0,
                    "error_count": 0, "ok_count": 0,
                })

    async def _init_client(self, acct: dict[str, str]) -> GeminiClient:
        c = GeminiClient(acct["psid"], acct["psidts"])
        await c.init(timeout=60)
        return c

    def _get_client(self, label: str, acct: dict[str, str]) -> GeminiClient:
        if label not in self._clients:
            self._clients[label] = self._submit(self._init_client(acct))
        return self._clients[label]

    def _pick_account(self) -> dict[str, str] | None:
        """Return the least-recently-used account not in cooldown."""
        now = time.time()
        candidates = []
        for a in self._accounts:
            s = self._state[a["label"]]
            if s["cooldown_until"] > now:
                continue
            candidates.append((s["last_used"], a))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    def _min_cooldown(self) -> float:
        now = time.time()
        return max(0.0, min((s["cooldown_until"] for s in self._state.values()), default=now) - now)

    def status(self) -> list[dict[str, Any]]:
        self.refresh()
        now = time.time()
        out = []
        for a in self._accounts:
            s = self._state[a["label"]]
            out.append({
                "label": a["label"],
                "psid_prefix": a["psid"][:10] + "...",
                "last_used_ago_s": round(now - s["last_used"], 1) if s["last_used"] else None,
                "cooldown_remaining_s": max(0, round(s["cooldown_until"] - now, 1)),
                "error_count": s["error_count"],
                "ok_count": s["ok_count"],
                "initialized": a["label"] in self._clients,
            })
        return out

    def run_with_rotation(self, task_fn, *, max_retries: int = 3):
        """task_fn is an async callable that receives a GeminiClient.
        Rotates over accounts on error; retries up to max_retries per attempt cycle."""
        self.refresh()
        attempts = 0
        last_err = None
        while attempts < max_retries * max(1, len(self._accounts)):
            acct = self._pick_account()
            if not acct:
                wait = self._min_cooldown()
                if wait > 30:
                    raise RuntimeError(f"All accounts on cooldown ({wait:.0f}s min). Aborting.")
                time.sleep(min(wait + 1, 30))
                continue
            label = acct["label"]
            try:
                client = self._get_client(label, acct)
                result = self._submit(task_fn(client))
                self._state[label]["ok_count"] += 1
                self._state[label]["last_used"] = time.time()
                return result, label
            except Exception as e:
                last_err = e
                self._state[label]["error_count"] += 1
                self._state[label]["cooldown_until"] = time.time() + _COOLDOWN_SEC
                self._state[label]["last_used"] = time.time()
                # If it's an auth/cookie error, evict the client so next use re-inits (or fails again cleanly)
                self._clients.pop(label, None)
                attempts += 1
        raise RuntimeError(f"All accounts failed after {attempts} attempts. Last error: {last_err}")


_POOL = _AccountPool()


# ---- Helpers ----

def _download(url: str) -> str:
    """Fetch a URL to a temp file, return local path."""
    p = pathlib.Path(tempfile.mkdtemp(prefix="gwebref_")) / "ref.bin"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        p.write_bytes(r.read())
    return str(p)


def _resolve_file(item: str) -> str:
    """Return a local path for a file argument (url/path/data-url)."""
    s = item.strip()
    if s.startswith(("http://", "https://")):
        return _download(s)
    if s.startswith("data:"):
        m = re.match(r"^data:(image/[^;]+);base64,(.+)$", s)
        if not m:
            raise ValueError("Invalid data URL")
        ext = m.group(1).split("/")[-1]
        p = pathlib.Path(tempfile.mkdtemp(prefix="gwebref_")) / f"ref.{ext}"
        p.write_bytes(base64.b64decode(m.group(2)))
        return str(p)
    if not pathlib.Path(s).exists():
        raise FileNotFoundError(f"Reference file not found: {s}")
    return s


def _out_dir(user_dir: str | os.PathLike[str] | None) -> pathlib.Path:
    d = pathlib.Path(user_dir).expanduser() if user_dir else _DEFAULT_OUTPUT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---- Public tools ----

def gemini_web_generate_image(
    prompt: str,
    aspect_ratio: str = "landscape",
    reference_images: list[str] | None = None,
    output_dir: str | None = None,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Generate one or more images. If reference_images provided, edits them."""
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")
    if aspect_ratio and aspect_ratio not in _ASPECT_HINTS:
        raise ValueError(f"aspect_ratio must be one of {list(_ASPECT_HINTS)}")

    files = []
    for item in (reference_images or []):
        files.append(_resolve_file(item))

    # Append aspect hint to prompt (gemini web has no direct AR arg — it obeys prompt)
    hint = _ASPECT_HINTS.get(aspect_ratio, "")
    full_prompt = f"{prompt.strip()}\n\n{hint}".strip()

    async def task(client: GeminiClient):
        return await asyncio.wait_for(
            client.generate_content(full_prompt, files=files if files else None),
            timeout=timeout,
        )

    resp, used_label = _POOL.run_with_rotation(task)

    out = _out_dir(output_dir)
    saved: list[str] = []
    for i, img in enumerate(getattr(resp, "images", []) or []):
        fname = f"gemweb_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}_{i}.png"
        target = out / fname
        # gemini_webapi's Image.save is async
        async def _save(img=img, target=target):
            await img.save(path=str(target.parent) + os.sep, filename=target.name, verbose=False)
        _POOL._submit(_save())
        saved.append(str(target))

    if not saved:
        return {
            "success": False,
            "error": "Gemini returned no images",
            "text": (resp.text or "")[:500] if hasattr(resp, "text") else "",
            "account": used_label,
        }
    return {
        "success": True,
        "images": saved,
        "image": saved[0],
        "count": len(saved),
        "text": (resp.text or "")[:500] if hasattr(resp, "text") else "",
        "account": used_label,
        "mode": "image_to_image" if files else "text_to_image",
    }


def gemini_web_chat(
    prompt: str,
    files: list[str] | None = None,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Send a multimodal chat request. Returns the text response.
    Supports vision (pass image URLs/paths in files) and general LLM tasks."""
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")

    resolved_files = [_resolve_file(f) for f in (files or [])]

    async def task(client: GeminiClient):
        return await asyncio.wait_for(
            client.generate_content(prompt, files=resolved_files if resolved_files else None),
            timeout=timeout,
        )

    resp, used_label = _POOL.run_with_rotation(task)
    return {
        "success": True,
        "text": resp.text or "",
        "account": used_label,
        "has_images": bool(getattr(resp, "images", None)),
    }


def gemini_web_accounts() -> dict[str, Any]:
    """Return status of all configured accounts."""
    return {"accounts": _POOL.status(), "count": len(_POOL._accounts)}


# ---- Hermes plugin registration ----

_SCHEMAS = {
    "gemini_web_generate_image": {
        "name": "gemini_web_generate_image",
        "description": "Generate images via Google Gemini web app (Nano Banana). "
                       "Supports text-to-image and image-to-image editing. "
                       "Rotates across multiple Google AI Pro accounts by cookie.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Image generation prompt"},
                "aspect_ratio": {"type": "string", "enum": list(_ASPECT_HINTS.keys()),
                                 "default": "landscape"},
                "reference_images": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Optional: file paths / URLs / data URLs. When provided, "
                                   "the model edits the reference (image-to-image mode).",
                },
                "output_dir": {"type": "string", "description": "Optional output directory"},
                "timeout": {"type": "number", "default": 180},
            },
            "required": ["prompt"],
        },
    },
    "gemini_web_chat": {
        "name": "gemini_web_chat",
        "description": "Multimodal chat / vision analysis via Google Gemini web app. "
                       "Attach image URLs or paths in `files` for vision. Returns text only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "URLs, local paths, or data URLs to attach"},
                "timeout": {"type": "number", "default": 180},
            },
            "required": ["prompt"],
        },
    },
    "gemini_web_accounts": {
        "name": "gemini_web_accounts",
        "description": "List configured Gemini web accounts + rotation state.",
        "input_schema": {"type": "object", "properties": {}},
    },
}


def register(ctx: Any) -> None:
    def _h_gen(args, **_):
        return gemini_web_generate_image(
            prompt=args["prompt"],
            aspect_ratio=args.get("aspect_ratio", "landscape"),
            reference_images=args.get("reference_images"),
            output_dir=args.get("output_dir"),
            timeout=float(args.get("timeout", 180)),
        )

    def _h_chat(args, **_):
        return gemini_web_chat(
            prompt=args["prompt"],
            files=args.get("files"),
            timeout=float(args.get("timeout", 180)),
        )

    def _h_accts(args, **_):
        return gemini_web_accounts()

    handlers = {
        "gemini_web_generate_image": _h_gen,
        "gemini_web_chat": _h_chat,
        "gemini_web_accounts": _h_accts,
    }
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(name=name, schema=schema, handler=handlers[name])
