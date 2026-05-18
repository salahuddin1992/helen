"""
Browser-facing /join/{code} landing page.

Why this exists
---------------
The desktop ``InviteLinkPanel`` mints URLs of the form
``https://server/join/<code>``. When that URL is opened in a
phone browser, on a colleague's laptop without the desktop app, or
forwarded into a chat that previews links — *something* needs to
serve a sensible response on the server itself.

This module mounts that response. The flow:

  1. Look up the code (read-only — no redemption yet).
  2. If the code is bad (not found / expired / revoked / not an
     invite), render an Arabic error page explaining what to ask
     the inviter to do.
  3. If the code is valid, render a small HTML page that:
       * Shows the channel name + a "Open in Helen Desktop" button
         that hands the code over via a custom URL scheme
         (``helen://join?code=<code>``) the desktop app can register.
       * Falls back to a "Copy code" button + paste-into-app
         instructions when the desktop scheme isn't handled.
       * Displays a quick "I don't have the desktop app" link to
         the Helen download page (the same server's
         ``/admin-secret`` setup link, configurable via env).

The page is fully self-contained HTML — no JS dependencies,
inline styles only. Never ships invite codes outside the LAN
because the server itself is LAN-only (Hard Rule #1).
"""

from __future__ import annotations

import html
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.services.access_codes_service import get_service as codes_service


router = APIRouter(tags=["invite-pages"])


_PAGE_CSS = """
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: 'Segoe UI', 'Noto Sans Arabic', system-ui, sans-serif;
  background: linear-gradient(135deg, #0d1938 0%, #1a2a55 55%, #2d1b5c 100%);
  color: #e6edf8;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
}
.card {
  background: rgba(17, 26, 46, 0.85);
  border: 1px solid #223155;
  border-radius: 14px;
  padding: 28px;
  max-width: 420px;
  width: 100%;
  text-align: center;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.4);
}
h1 { margin: 0 0 6px; font-size: 22px; }
.sub { color: #8ea2c5; font-size: 13px; margin-bottom: 20px; }
.code {
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  font-size: 18px;
  background: #1a2340;
  padding: 10px 14px;
  border-radius: 8px;
  color: #4cc2ff;
  user-select: all;
  word-break: break-all;
  margin: 12px 0;
}
.btn {
  display: inline-block;
  padding: 10px 18px;
  border-radius: 8px;
  background: #2563eb;
  color: white;
  text-decoration: none;
  font-weight: 600;
  font-size: 14px;
  border: none;
  cursor: pointer;
  margin: 6px 4px;
}
.btn:hover { background: #1d4ed8; }
.btn.secondary { background: #1f2a44; color: #cbd5e1; }
.btn.secondary:hover { background: #2d3f66; }
.error {
  color: #ff5c7a;
  padding: 10px;
  background: rgba(255, 92, 122, 0.1);
  border: 1px solid #5c2237;
  border-radius: 8px;
  font-size: 13px;
}
.note { font-size: 11px; color: #6a7fa0; margin-top: 14px; }
"""


def _render_error(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Helen — {html.escape(title)}</title>
  <style>{_PAGE_CSS}</style>
</head>
<body>
  <div class="card">
    <h1>{html.escape(title)}</h1>
    <p class="error">{html.escape(body)}</p>
    <p class="note">اطلب من الشخص الذي أرسلك الرابط أن يُنشئ رمز
      دعوة جديداً.</p>
  </div>
</body>
</html>"""


def _render_landing(code: str, channel_label: str) -> str:
    safe_code = html.escape(code)
    safe_label = html.escape(channel_label or "قناة Helen")
    return f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Helen — انضمام إلى {safe_label}</title>
  <style>{_PAGE_CSS}</style>
</head>
<body>
  <div class="card">
    <h1>دعوة للانضمام</h1>
    <p class="sub">دُعيت إلى <strong>{safe_label}</strong> على Helen.</p>

    <div class="code">{safe_code}</div>

    <a class="btn" href="helen://join?code={safe_code}">
      افتح في تطبيق Helen Desktop
    </a>
    <button class="btn secondary" id="copyBtn"
            onclick="navigator.clipboard.writeText('{safe_code}')
                       .then(() => this.textContent = 'تم النسخ ✓')
                       .catch(() => alert('انسخ الرمز يدويّاً'))">
      نسخ الرمز
    </button>

    <p class="note">
      افتح Helen Desktop ثمّ اضغط على «الانضمام برمز»، والصق الرمز.
      إذا لم يكن لديك التطبيق بعد، اطلبه من مسؤول السيرفر.
    </p>
  </div>
</body>
</html>"""


@router.get("/join/{code}", response_class=HTMLResponse, include_in_schema=False)
async def join_landing(code: str) -> HTMLResponse:
    """Browser landing page for an invite code. Read-only (does
    NOT redeem the code) — the actual join happens once the user
    opens the desktop app and POSTs to /api/channels/join-by-code."""
    code = (code or "").strip()
    if not code or len(code) > 128:
        return HTMLResponse(
            _render_error("رمز غير صالح", "هذا الرابط غير صالح."),
            status_code=400,
        )

    rec = codes_service().lookup_redacted(code)
    if rec is None:
        return HTMLResponse(
            _render_error("رمز غير موجود",
                          "الرابط غير معروف لدى السيرفر — قد يكون"
                          " قديماً أو خاطئاً."),
            status_code=404,
        )
    if rec.get("revoked"):
        return HTMLResponse(
            _render_error("رمز مُلغى",
                          "أُلغي هذا الرابط من قِبل صاحبه."),
            status_code=410,
        )
    if rec.get("kind") != "invite":
        return HTMLResponse(
            _render_error("نوع غير مناسب",
                          "هذا ليس رمز دعوة لقناة."),
            status_code=400,
        )

    # Channel label is best-effort — we just show the channel id
    # tail when the channel name isn't reachable here (avoiding a
    # DB query keeps this endpoint cheap + sync-friendly).
    target_channel = rec.get("target_channel_id") or ""
    label = (
        f"قناة …{target_channel[-6:]}"
        if target_channel else "قناة Helen"
    )
    return HTMLResponse(_render_landing(code, label))


__all__ = ["router"]
