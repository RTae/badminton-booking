"""A local web page where *you* solve the site's slider captcha.

The booking site asks for a slider puzzle before every reservation. This
module does not try to solve it — it fetches the challenge from the site,
shows it to you on ``http://127.0.0.1:<port>/`` with the same slider the site
uses, and forwards your answer. The page also shows a countdown and a live
log so you can leave it open next to the terminal.

With ``autosolve=True`` (the ``--auto-captcha`` flag) each challenge is also
offered to :mod:`autosolve` in the background; if the
computer vision is confident, the answer is submitted automatically, and you
only see the slider if it isn't.
"""

from __future__ import annotations

import json
import queue
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Protocol

from client import Captcha

# The site's token for an unsolved challenge expires after ~2 minutes.
CHALLENGE_TTL = 100.0


class CaptchaBackend(Protocol):
    def get_captcha(self) -> Captcha: ...
    def check_captcha(self, captcha: Captcha, x: int) -> str | None: ...


class CaptchaDesk:
    def __init__(self, backend: CaptchaBackend, *, open_browser: bool = True,
                 autosolve: bool = False, notify: Callable[[str], None] | None = None):
        self.backend = backend
        self.open_browser = open_browser
        self.autosolve = autosolve
        self.notify = notify
        self._lock = threading.Lock()
        self._challenge: dict[str, str] | None = None
        self._answers: queue.Queue[tuple[str, int]] = queue.Queue()
        self._log: list[str] = []
        self._fire_at_ms: int | None = None
        self._headline = "Waiting…"
        self._key = secrets.token_urlsafe(12)
        self._server: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> str:
        desk = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the terminal quiet
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == f"/{desk._key}":
                    self._send(200, _PAGE.replace("__KEY__", desk._key).encode(), "text/html; charset=utf-8")
                elif self.path == f"/{desk._key}/state":
                    self._send(200, json.dumps(desk._state()).encode())
                else:
                    self._send(404, b"{}")

            def do_POST(self):
                if self.path != f"/{desk._key}/answer":
                    return self._send(404, b"{}")
                length = int(self.headers.get("Content-Length", 0))
                try:
                    data = json.loads(self.rfile.read(length))
                    desk._answers.put((str(data["id"]), int(round(float(data["x"])))))
                except (ValueError, KeyError):
                    return self._send(400, b"{}")
                self._send(200, b"{}")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        if self.open_browser:
            webbrowser.open(self.url)
        return self.url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None

    @property
    def url(self) -> str:
        assert self._server, "call start() first"
        return f"http://127.0.0.1:{self._server.server_address[1]}/{self._key}"

    # --------------------------------------------------------------- status
    def log(self, line: str) -> None:
        with self._lock:
            self._log.append(line)
            del self._log[:-200]

    def set_headline(self, text: str, fire_at_epoch: float | None = None) -> None:
        with self._lock:
            self._headline = text
            self._fire_at_ms = int(fire_at_epoch * 1000) if fire_at_epoch else None

    def _state(self) -> dict[str, object]:
        with self._lock:
            return {
                "headline": self._headline,
                "fireAt": self._fire_at_ms,
                "log": self._log[-40:],
                "challenge": self._challenge,
            }

    # ---------------------------------------------------------------- solve
    def _try_autosolve(self, snapshot: dict[str, str]) -> None:
        """Try computer vision on one challenge; submit the answer if confident."""
        try:
            from autosolve import solve as autosolve
            x = autosolve(snapshot["bg"], snapshot["piece"])
        except Exception as e:  # a solver problem must never break the run
            self.log(f"auto-solve failed: {e}")
            return
        with self._lock:
            current_id = (self._challenge or {}).get("id")
        if current_id != snapshot["id"]:
            return  # the challenge has moved on
        if x is None:
            self.log("auto-solve is unsure of the position — solve it by hand")
            return
        self.log(f"auto-solve: x={x}")
        self._answers.put((snapshot["id"], x))

    def _publish_challenge(self, captcha: Captcha, attempt: int, reason: str) -> str:
        cid = secrets.token_hex(6)
        prompt = "Drag the slider to fit the piece" + (f" — {reason}" if reason else "")
        if attempt > 1:
            prompt = "Not quite — try again. " + prompt
        with self._lock:
            self._challenge = {
                "id": cid,
                "bg": captcha.background_b64,
                "piece": captcha.piece_b64,
                "prompt": prompt,
            }
            snapshot = dict(self._challenge) if self.autosolve else None
        if snapshot:
            threading.Thread(target=self._try_autosolve, args=(snapshot,), daemon=True).start()
        if attempt == 1 and self.notify and not self.autosolve:
            self.notify("Captcha needed — solve the slider in your browser")
        return cid

    def _wait_for_answer(self, cid: str, expires: float, deadline: float | None) -> int | None:
        while True:
            limit = min(expires, deadline) if deadline else expires
            remaining = limit - time.time()
            if remaining <= 0:
                return None
            try:
                got_id, x = self._answers.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if got_id == cid:
                return x

    def solve(self, *, deadline: float | None = None, reason: str = "") -> str | None:
        """Show challenges until you get one right. Returns ``captchaVerification``.

        Returns ``None`` if ``deadline`` (``time.time()`` epoch) passes first.
        """
        attempt = 0
        while deadline is None or time.time() < deadline:
            captcha = self.backend.get_captcha()
            attempt += 1
            cid = self._publish_challenge(captcha, attempt, reason)
            answer_x = self._wait_for_answer(cid, time.time() + CHALLENGE_TTL, deadline)
            if answer_x is None:
                continue  # challenge expired (or deadline hit): loop re-checks deadline

            verification = self.backend.check_captcha(captcha, answer_x)
            if verification:
                with self._lock:
                    self._challenge = None
                self.log("✓ captcha solved")
                return verification
        with self._lock:
            self._challenge = None
        return None


_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Court booking</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --ink:#1d2330; --muted:#6b7280; --accent:#2f6fde; --ok:#1f9d55; --line:#e3e6eb; }
  @media (prefers-color-scheme: dark) { :root { --bg:#14171c; --card:#1d2128; --ink:#e7eaf0; --muted:#9aa3b2; --accent:#6ea0ff; --ok:#4cc38a; --line:#2c323c; } }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { max-width: 420px; margin: 32px auto; padding: 0 16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:18px; margin-bottom:14px; }
  h1 { font-size:17px; margin:0 0 4px; }
  #countdown { font: 600 34px/1.1 ui-monospace, SFMono-Regular, Menlo, monospace; margin: 6px 0 2px; }
  .muted { color: var(--muted); font-size: 13px; }
  #puzzle { position:relative; width:310px; height:155px; margin: 12px auto 10px; user-select:none; }
  #puzzle img { position:absolute; top:0; left:0; pointer-events:none; }
  #piece { width:47px; height:155px; }
  #bar { position:relative; width:310px; height:40px; margin:0 auto; background:var(--bg); border:1px solid var(--line); border-radius:6px; touch-action:none; }
  #bar span { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:var(--muted); font-size:13px; pointer-events:none; }
  #handle { position:absolute; top:-1px; left:0; width:47px; height:40px; background:var(--accent); border-radius:6px; cursor:grab; color:#fff; display:flex; align-items:center; justify-content:center; font-size:18px; }
  #prompt { text-align:center; margin: 4px 0 0; }
  #idle { text-align:center; color:var(--muted); padding: 24px 0; }
  pre { margin:0; max-height: 260px; overflow:auto; font: 12px/1.5 ui-monospace, Menlo, monospace; white-space: pre-wrap; }
</style></head>
<body><main>
  <div class="card">
    <h1 id="headline">Waiting…</h1>
    <div id="countdown"></div>
    <div class="muted" id="sub"></div>
  </div>
  <div class="card">
    <div id="idle">No captcha needed right now.<br>Keep this tab open — it will beep when one appears.</div>
    <div id="solve" hidden>
      <div id="prompt"></div>
      <div id="puzzle"><img id="bg" width="310" height="155" alt=""><img id="piece" alt=""></div>
      <div id="bar"><span>drag →  (or ← → keys, Enter)</span><div id="handle" tabindex="0">➜</div></div>
    </div>
  </div>
  <div class="card"><pre id="log"></pre></div>
</main>
<script>
const KEY = "__KEY__";
const MAXX = 310 - 47;
let current = null, x = 0, dragging = false, startX = 0, fireAt = null, submitted = false;
const $ = id => document.getElementById(id);

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.frequency.value = 880; o.connect(g); g.connect(ctx.destination);
    g.gain.setValueAtTime(0.15, ctx.currentTime); o.start(); o.stop(ctx.currentTime + 0.25);
  } catch (e) {}
}
function setX(v) { x = Math.max(0, Math.min(MAXX, v)); $("piece").style.left = x + "px"; $("handle").style.left = x + "px"; }
async function submit() {
  if (!current || submitted) return;
  submitted = true;
  await fetch(`/${KEY}/answer`, {method: "POST", body: JSON.stringify({id: current, x})});
}
$("handle").addEventListener("pointerdown", e => { if (submitted) return; dragging = true; startX = e.clientX - x; $("handle").setPointerCapture(e.pointerId); });
$("handle").addEventListener("pointermove", e => { if (dragging) setX(e.clientX - startX); });
$("handle").addEventListener("pointerup", () => { if (dragging) { dragging = false; submit(); } });
document.addEventListener("keydown", e => {
  if (!current || submitted) return;
  if (e.key === "ArrowRight") setX(x + (e.shiftKey ? 10 : 1));
  else if (e.key === "ArrowLeft") setX(x - (e.shiftKey ? 10 : 1));
  else if (e.key === "Enter") submit();
});

function fmt(ms) {
  const neg = ms < 0; ms = Math.abs(ms);
  const s = Math.floor(ms / 1000), h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
  return (neg ? "+" : "") + (h ? h + ":" : "") + String(m).padStart(2, "0") + ":" + String(ss).padStart(2, "0");
}
setInterval(() => { $("countdown").textContent = fireAt ? fmt(fireAt - Date.now()) : ""; }, 200);

async function poll() {
  try {
    const s = await (await fetch(`/${KEY}/state`)).json();
    $("headline").textContent = s.headline;
    fireAt = s.fireAt;
    $("sub").textContent = fireAt ? "until booking opens (" + new Date(fireAt).toLocaleTimeString() + ")" : "";
    $("log").textContent = s.log.join("\n");
    const c = s.challenge;
    if (c && c.id !== current) {
      current = c.id; submitted = false; setX(0);
      $("bg").src = "data:image/png;base64," + c.bg;
      $("piece").src = "data:image/png;base64," + c.piece;
      $("prompt").textContent = c.prompt;
      $("solve").hidden = false; $("idle").hidden = true;
      $("handle").focus(); beep(); document.title = "⚠ Captcha needed";
    } else if (!c && current) {
      current = null; $("solve").hidden = true; $("idle").hidden = false; document.title = "Court booking";
    }
  } catch (e) {}
  setTimeout(poll, 400);
}
poll();
</script>
</body></html>
"""
