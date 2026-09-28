"""Hermes plugin: Gemini Web — image & video generation via Google AI Pro subscription.

Uses the reverse-engineered gemini-webapi library (HanaokaYuzu/Gemini-API)
to call the Gemini web app backend. Authenticates via browser cookies,
supports multi-account round-robin.

Tools:
  gemini_web_generate       — text-to-image
  gemini_web_edit           — image-to-image (preserves design from reference)
  gemini_web_generate_video — text-to-video or image-to-video (with reference file)
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import time
import traceback
from typing import Any

try:
    from PIL import Image as _PILImage
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_COOKIES_PATH = pathlib.Path("~/.hermes/gemini_cookies.json").expanduser()
_OUTPUT_DIR = pathlib.Path("~/.hermes/cache/images").expanduser()
_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
_VIDEO_DIR = pathlib.Path("~/.hermes/cache/videos").expanduser()
_VIDEO_DIR.mkdir(parents=True, exist_ok=True)

_MAX_RETRIES_PER_ACCOUNT = 1
_INIT_TIMEOUT = 60
_GEN_TIMEOUT = 300  # generous — Gemini web can be slow

# Default model for all generation calls.
# Gemini 3.5 Flash Lite supports image gen + video gen via AI Pro subscription.
_DEFAULT_MODEL = "gemini-3.5-flash-lite"

# Watermark crop: Gemini web bakes a logo into bottom-right corner.
# Strategy: prompt instructs model to leave empty margin there, then we crop it off.
# CROP_RATIO = fraction of width/height to KEEP (crop from right + bottom).
_CROP_RATIO = 0.88  # keep 88% → discard 12% right + 12% bottom

# Appended to every gen/edit prompt automatically to push content away from crop zone.
_WATERMARK_PROMPT_SUFFIX = (
    " Leave a plain, empty margin of at least 12% on the RIGHT edge and BOTTOM edge of the frame "
    "(solid background color, no subject or text in that strip). "
    "Main subject must be fully contained in the upper-left 88% of the frame."
)


def _crop_watermark(path: str) -> str:
    """Crop the bottom-right watermark strip from a Gemini-generated image.

    Gemini bakes its logo into the bottom-right corner. We instruct the model
    to leave a blank margin there, then crop it away here.

    Returns the same path (file is overwritten in-place).
    """
    if not _PIL_AVAILABLE:
        return path  # PIL not installed — skip silently
    try:
        img = _PILImage.open(path)
        w, h = img.size
        new_w = int(w * _CROP_RATIO)
        new_h = int(h * _CROP_RATIO)
        # Crop from top-left: keep [0:new_w, 0:new_h]
        cropped = img.crop((0, 0, new_w, new_h))
        # Convert to RGB before saving as JPEG/PNG (handle RGBA)
        if cropped.mode == "RGBA" and path.lower().endswith((".jpg", ".jpeg")):
            cropped = cropped.convert("RGB")
        cropped.save(path)
        return path
    except Exception:
        return path  # non-fatal — return original on any error


def _load_accounts() -> list[dict]:
    """Return list of account dicts from the cookies JSON file."""
    if not _COOKIES_PATH.exists():
        raise FileNotFoundError(
            f"Cookies file not found: {_COOKIES_PATH}. "
            "Create it with: [{\"label\":\"acct1\",\"__Secure-1PSID\":\"...\",\"__Secure-1PSIDTS\":\"...\"}]"
        )
    data = json.loads(_COOKIES_PATH.read_text())
    if not isinstance(data, list) or not data:
        raise ValueError("gemini_cookies.json must be a non-empty JSON array")
    accounts = []
    for i, entry in enumerate(data):
        psid = entry.get("__Secure-1PSID", "").strip()
        psidts = entry.get("__Secure-1PSIDTS", "").strip()
        if not psid:
            raise ValueError(f"Account {i}: __Secure-1PSID is required")
        accounts.append({
            "label": entry.get("label", f"acct{i}"),
            "psid": psid,
            "psidts": psidts,
        })
    return accounts


# ---------------------------------------------------------------------------
# Round-robin state (in-memory, resets on process restart)
# ---------------------------------------------------------------------------
_rr_index = 0
_cooldowns: dict[str, float] = {}  # label -> earliest_retry_ts
_COOLDOWN_SECS = 120  # back off an account for 2 min after error


def _next_account(accounts: list[dict]) -> dict | None:
    """Pick next healthy account via round-robin. Returns None if all on cooldown."""
    global _rr_index
    n = len(accounts)
    now = time.time()
    for _ in range(n):
        acct = accounts[_rr_index % n]
        _rr_index += 1
        cd = _cooldowns.get(acct["label"], 0)
        if now >= cd:
            return acct
    return None


def _mark_cooldown(label: str):
    _cooldowns[label] = time.time() + _COOLDOWN_SECS


def _clear_cooldown(label: str):
    _cooldowns.pop(label, None)


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------
async def _make_client(acct: dict):
    from gemini_webapi import GeminiClient, set_log_level
    set_log_level("WARNING")
    client = GeminiClient(acct["psid"], acct["psidts"])
    await client.init(timeout=_INIT_TIMEOUT)
    return client


async def _generate_async(prompt: str, aspect_ratio: str | None = None) -> dict:
    """Text-to-image via Gemini web."""
    full_prompt = prompt + _WATERMARK_PROMPT_SUFFIX
    accounts = _load_accounts()
    last_err = None
    for attempt in range(len(accounts)):
        acct = _next_account(accounts)
        if acct is None:
            break
        try:
            client = await _make_client(acct)
            resp = await client.generate_content(full_prompt, model=_DEFAULT_MODEL)
            if not resp.images:
                raise RuntimeError("Gemini returned no images")
            _clear_cooldown(acct["label"])
            paths = []
            for i, img in enumerate(resp.images):
                ts = time.strftime("%Y%m%d_%H%M%S")
                fname = f"gemweb_{ts}_{i}.png"
                await img.save(path=str(_OUTPUT_DIR), filename=fname, verbose=False)
                saved = _crop_watermark(str(_OUTPUT_DIR / fname))
                paths.append(saved)
            return {
                "success": True,
                "account": acct["label"],
                "images": paths,
                "text": (resp.text or "")[:500],
            }
        except Exception as e:
            last_err = f"[{acct['label']}] {type(e).__name__}: {str(e)[:300]}"
            _mark_cooldown(acct["label"])
    return {"success": False, "error": last_err or "All accounts on cooldown"}


async def _edit_async(prompt: str, reference_path: str) -> dict:
    """Image-to-image via Gemini web — sends reference file."""
    full_prompt = prompt + _WATERMARK_PROMPT_SUFFIX
    ref = pathlib.Path(reference_path)
    if not ref.exists():
        return {"success": False, "error": f"Reference file not found: {reference_path}"}

    accounts = _load_accounts()
    last_err = None
    for attempt in range(len(accounts)):
        acct = _next_account(accounts)
        if acct is None:
            break
        try:
            client = await _make_client(acct)
            resp = await client.generate_content(full_prompt, files=[str(ref)], model=_DEFAULT_MODEL)
            if not resp.images:
                raise RuntimeError("Gemini returned no images (edit mode)")
            _clear_cooldown(acct["label"])
            paths = []
            for i, img in enumerate(resp.images):
                ts = time.strftime("%Y%m%d_%H%M%S")
                fname = f"gemweb_edit_{ts}_{i}.png"
                await img.save(path=str(_OUTPUT_DIR), filename=fname, verbose=False)
                saved = _crop_watermark(str(_OUTPUT_DIR / fname))
                paths.append(saved)
            return {
                "success": True,
                "account": acct["label"],
                "images": paths,
                "text": (resp.text or "")[:500],
            }
        except Exception as e:
            last_err = f"[{acct['label']}] {type(e).__name__}: {str(e)[:300]}"
            _mark_cooldown(acct["label"])
    return {"success": False, "error": last_err or "All accounts on cooldown"}


async def _generate_video_async(prompt: str, reference_path: str | None = None) -> dict:
    """Text-to-video or image-to-video via Gemini web (AI Pro).

    Gemini may return video in response.videos (GeneratedVideo) or response.media
    (GeneratedMedia, which also includes audio). Both are saved.
    The save() method polls until the video is ready (206 → retry every 10s).
    """
    accounts = _load_accounts()
    last_err = None
    for attempt in range(len(accounts)):
        acct = _next_account(accounts)
        if acct is None:
            break
        try:
            client = await _make_client(acct)
            files = [str(reference_path)] if reference_path else None
            resp = await client.generate_content(prompt, files=files, model=_DEFAULT_MODEL)

            ts = time.strftime("%Y%m%d_%H%M%S")
            result_files: dict[str, str | None] = {}

            # GeneratedVideo objects (pure video, no audio track)
            for i, vid in enumerate(resp.videos or []):
                fname = f"gemweb_video_{ts}_{i}"
                saved = await vid.save(path=str(_VIDEO_DIR), filename=fname, verbose=False)
                result_files[f"video_{i}"] = saved.get("video") if isinstance(saved, dict) else saved
                if isinstance(saved, dict) and saved.get("video_thumbnail"):
                    result_files[f"video_{i}_thumb"] = saved["video_thumbnail"]

            # GeneratedMedia objects (video+audio or audio-only)
            for i, media in enumerate(resp.media or []):
                fname = f"gemweb_media_{ts}_{i}"
                saved = await media.save(
                    path=str(_VIDEO_DIR), filename=fname, verbose=False, download_type="both"
                )
                if isinstance(saved, dict):
                    for k, v in saved.items():
                        result_files[f"media_{i}_{k}"] = v
                else:
                    result_files[f"media_{i}_video"] = saved

            if not result_files:
                # Gemini returned text only — video gen not available for this account/region
                raise RuntimeError(
                    f"No video/media in response. Text: {(resp.text or '')[:200]}. "
                    "Video generation requires AI Pro subscription and may not be available in all regions."
                )

            _clear_cooldown(acct["label"])
            return {
                "success": True,
                "account": acct["label"],
                "files": result_files,
                "text": (resp.text or "")[:500],
            }
        except Exception as e:
            last_err = f"[{acct['label']}] {type(e).__name__}: {str(e)[:300]}"
            _mark_cooldown(acct["label"])
    return {"success": False, "error": last_err or "All accounts on cooldown"}

# ---------------------------------------------------------------------------
# Sync wrappers (Hermes handlers are sync)
# ---------------------------------------------------------------------------
def _run_async(coro):
    """Run an async coroutine from sync context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # Already in an event loop — use a new thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=_GEN_TIMEOUT)
    return asyncio.run(coro)


def gemini_web_generate(prompt: str, aspect_ratio: str = "square") -> dict:
    """Generate image from text prompt via Gemini web (AI Pro)."""
    return _run_async(_generate_async(prompt, aspect_ratio))


def gemini_web_edit(prompt: str, reference_path: str) -> dict:
    """Edit/transform an image via Gemini web (AI Pro). Sends reference_path as input."""
    return _run_async(_edit_async(prompt, reference_path))


def gemini_web_generate_video(prompt: str, reference_path: str | None = None) -> dict:
    """Generate video from text or image reference via Gemini web (AI Pro).

    Text-to-video: pass prompt only.
    Image-to-video: pass prompt + reference_path (local file path to the reference image).
    Returns {success, files: {video_0: path, ...}, account, text}.
    Video is saved to ~/.hermes/cache/videos/. Polls until render complete.
    """
    return _run_async(_generate_video_async(prompt, reference_path))


# ---------------------------------------------------------------------------
# Hermes tool registration
# ---------------------------------------------------------------------------
SCHEMAS = {
    "gemini_web_generate": {
        "name": "gemini_web_generate",
        "description": (
            "Generate an image from a text prompt using Google Gemini web (AI Pro subscription). "
            "Multi-account round-robin. Returns {success, images: [path,...], account, text}. "
            "Example: gemini_web_generate(prompt='A cozy coffee shop interior, warm lighting, watercolor style')"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Text prompt for image generation. Be specific about style, lighting, composition.",
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "Aspect ratio hint (informational — Gemini web chooses final size). Default: square",
                    "enum": ["square", "landscape", "portrait"],
                },
            },
            "required": ["prompt"],
        },
    },
    "gemini_web_edit": {
        "name": "gemini_web_edit",
        "description": (
            "Edit/transform an existing image using Google Gemini web (AI Pro). "
            "Sends a reference image file + text prompt. The model preserves the design from the reference. "
            "Returns {success, images: [path,...], account, text}. "
            "Example: gemini_web_edit(prompt='Place this product on a wooden table in a bright kitchen', "
            "reference_path='/tmp/product.jpg')"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Edit instruction. Describe the desired changes/scene, NOT the original design.",
                },
                "reference_path": {
                    "type": "string",
                    "description": "Absolute path to the reference image file (local file only, not URL).",
                },
            },
            "required": ["prompt", "reference_path"],
        },
    },
    "gemini_web_generate_video": {
        "name": "gemini_web_generate_video",
        "description": (
            "Generate a short video using Google Gemini web (AI Pro subscription). "
            "Text-to-video: pass prompt only. "
            "Image-to-video: pass prompt + reference_path (local image file — Gemini animates it). "
            "Requires AI Pro subscription. Polls until render complete (may take 1-3 min). "
            "Returns {success, files: {video_0: '/path/to/video.mp4', ...}, account, text}. "
            "Example: gemini_web_generate_video(prompt='Animate this product tote bag gently swaying', "
            "reference_path='/tmp/product.jpg')"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Video generation prompt. Describe motion, scene, style.",
                },
                "reference_path": {
                    "type": "string",
                    "description": "Optional: absolute path to a reference image for image-to-video mode.",
                },
            },
            "required": ["prompt"],
        },
    },
}


def _check() -> bool:
    return _COOKIES_PATH.exists()


def _h_generate(args: dict, **_) -> str:
    try:
        result = gemini_web_generate(
            prompt=args["prompt"],
            aspect_ratio=args.get("aspect_ratio", "square"),
        )
        return json.dumps(result, default=str)
    except Exception as exc:
        return json.dumps({
            "error": "handler_exception",
            "type": type(exc).__name__,
            "message": str(exc)[:500],
            "traceback": traceback.format_exc()[-500:],
        })


def _h_edit(args: dict, **_) -> str:
    try:
        result = gemini_web_edit(
            prompt=args["prompt"],
            reference_path=args["reference_path"],
        )
        return json.dumps(result, default=str)
    except Exception as exc:
        return json.dumps({
            "error": "handler_exception",
            "type": type(exc).__name__,
            "message": str(exc)[:500],
            "traceback": traceback.format_exc()[-500:],
        })


def _h_generate_video(args: dict, **_) -> str:
    try:
        result = gemini_web_generate_video(
            prompt=args["prompt"],
            reference_path=args.get("reference_path"),
        )
        return json.dumps(result, default=str)
    except Exception as exc:
        return json.dumps({
            "error": "handler_exception",
            "type": type(exc).__name__,
            "message": str(exc)[:500],
            "traceback": traceback.format_exc()[-500:],
        })


def register(ctx: Any) -> None:
    for name, handler in [
        ("gemini_web_generate", _h_generate),
        ("gemini_web_edit", _h_edit),
        ("gemini_web_generate_video", _h_generate_video),
    ]:
        ctx.register_tool(
            name=name,
            toolset="gemini_web",
            schema=SCHEMAS[name],
            handler=handler,
            check_fn=_check,
            description=SCHEMAS[name]["description"],
            emoji="🎨",
        )
