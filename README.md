# gemini-web (Hermes plugin)

Generate and edit images using **Google Gemini web** (AI Pro subscription) via the [`gemini-webapi`](https://github.com/HanaokaYuzu/Gemini-API) reverse-engineered library.

Supports **text-to-image** and **image-to-image** (design-preserving edit). Multi-account round-robin with automatic cooldown on errors.

## Tools

| Tool | Description |
|---|---|
| `gemini_web_generate` | Text → image via Gemini web |
| `gemini_web_edit` | Image → image (send reference file, preserve design) |

## Auth

Uses browser cookies — no billing required, uses your Google AI Pro subscription.

Config file: `~/.hermes/gemini_cookies.json` (chmod 600)

```json
[
  {"label": "acct1", "__Secure-1PSID": "...", "__Secure-1PSIDTS": "..."},
  {"label": "acct2", "__Secure-1PSID": "...", "__Secure-1PSIDTS": "..."}
]
```

Export cookies from `gemini.google.com` using browser DevTools (Network tab → any request → Cookie header) or Cookie-Editor extension.

> **Tip:** Use Firefox to export cookies. Chromium-based browsers use Device Bound Session Credentials, which causes cookies to expire within hours.

## Watermark handling

Gemini web bakes its logo into the bottom-right corner. The plugin handles this automatically:
1. **Prompt suffix** — every call appends an instruction to leave a 12% blank margin on the right and bottom edges
2. **Auto-crop** — after saving, PIL crops the image to 88% × 88% (removes the margin + watermark)

Output size: 1802×1802px (from 2048×2048 source).

## Setup

```bash
# 1. Install dependency
pip install gemini-webapi

# 2. Create cookies file
cat > ~/.hermes/gemini_cookies.json << 'JSON'
[{"label": "acct1", "__Secure-1PSID": "YOUR_PSID", "__Secure-1PSIDTS": "YOUR_PSIDTS"}]
JSON
chmod 600 ~/.hermes/gemini_cookies.json

# 3. Enable plugin
hermes plugins enable gemini-web
hermes tools enable gemini_web   # for CLI

# 4. Restart gateway
hermes gateway restart
```

## Rate limits

~5-10 req/min per account on Gemini web. With 3 accounts: effectively ~15-30 req/min with round-robin.

Each image takes ~30-40s.

## Security

- Cookies are stored locally (chmod 600), never logged or echoed
- Round-robin rotates accounts; failed accounts go on 2-min cooldown
- `_crop_watermark()` is non-fatal — returns original image on any PIL error

## Library

Built on top of [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) — reverse-engineered async Python wrapper for the Gemini web app.
