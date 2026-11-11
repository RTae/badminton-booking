"""Tests for the automatic captcha solver and its integration."""

import base64
import time

import numpy as np
import pytest

from autosolve import solve
from booker import Booker, Plan
from captcha_desk import CaptchaDesk
from client import Captcha

BG_W, BG_H, PIECE_W = 310, 155, 47


# ------------------------------------------------------------- synthetic art
def _texture(seed: int) -> np.ndarray:
    """A distinctive 155x310 RGB background: gradient + coloured noise blobs."""
    rng = np.random.default_rng(seed)
    x = np.linspace(0, 255, BG_W, dtype=np.float32)
    y = np.linspace(0, 255, BG_H, dtype=np.float32)
    bg = np.zeros((BG_H, BG_W, 3), np.float32)
    bg[..., 0] = x[None, :]
    bg[..., 1] = y[:, None]
    bg[..., 2] = 100
    for _ in range(60):
        y, x0 = rng.integers(0, BG_H), rng.integers(0, BG_W)
        r = rng.integers(4, 18)
        col = rng.integers(0, 255, 3).astype(np.float32)
        y0, y1 = max(0, y - r), min(BG_H, y + r)
        x1 = min(BG_W, x0 + r)
        x2 = max(0, x0 - r)
        bg[y0:y1, x2:x1] += col
    return np.clip(bg, 0, 255).astype(np.uint8)


def make_challenge(x: int = 100, seed: int = 7) -> tuple[str, str]:
    """A blockPuzzle pair like the real site's: the piece is a cutout of the
    background (wavy edges, alpha channel) and the hole is shown darkened."""
    bg = _texture(seed)
    piece_bgr = bg[:, x:x + PIECE_W].copy()
    alpha = np.full((BG_H, PIECE_W), 255, np.uint8)
    for y in range(BG_H):
        wiggle = int(3 * np.sin(y / 9.0) + 4 * np.sin(y / 3.7))
        alpha[y, :max(0, 9 + wiggle)] = 0
        alpha[y, min(PIECE_W, 38 + wiggle):] = 0

    hole = (bg[:, x:x + PIECE_W] * 0.5).astype(np.uint8)
    bg[:, x:x + PIECE_W][alpha > 0] = hole[alpha > 0]
    piece = np.dstack([piece_bgr, alpha])

    ok, buf = __import__("cv2").imencode(".png", bg)
    assert ok
    bg_b64 = base64.b64encode(buf.tobytes()).decode()
    ok, buf = __import__("cv2").imencode(".png", piece)
    assert ok
    piece_b64 = base64.b64encode(buf.tobytes()).decode()
    return bg_b64, piece_b64


# --------------------------------------------------------------------- solver
def test_solve_finds_the_cut_position():
    for x in (40, 100, 137, 220, 255):
        bg_b64, piece_b64 = make_challenge(x=x)
        found = solve(bg_b64, piece_b64)
        assert found is not None and abs(found - x) <= 2, f"x={x} -> {found}"


def test_solve_returns_none_for_an_unrelated_piece():
    bg_b64, _ = make_challenge(x=100, seed=1)
    _, piece_b64 = make_challenge(x=100, seed=99)      # a different background
    assert solve(bg_b64, piece_b64) is None


def test_solve_returns_none_for_garbage():
    assert solve("not base64", "also not") is None
    assert solve("", "") is None


# --------------------------------------------------------- desk and booker
class AutoBackend:
    """Issues a synthetic challenge whose answer the site accepts at x == 100."""

    def __init__(self, x=100):
        self.x = x
        self.bg_b64, self.piece_b64 = make_challenge(x=x)

    def get_captcha(self):
        return Captcha(token="t", secret_key="k" * 16,
                       background_b64=self.bg_b64, piece_b64=self.piece_b64)

    def check_captcha(self, captcha, x):
        return f"verified:{x}" if x == self.x else None


def test_desk_autosolves_without_human_input():
    desk = CaptchaDesk(AutoBackend(), open_browser=False, autosolve=True)
    try:
        value = desk.solve(deadline=time.time() + 10)
        assert value == "verified:100"
    finally:
        desk.stop()


def test_desk_autosolve_leaves_it_to_the_human_when_unsure():
    class Unsure(AutoBackend):
        def __init__(self):
            self.x = 100
            self.bg_b64, self.piece_b64 = make_challenge(x=100, seed=1)
            self.bg_b64, _ = make_challenge(x=40, seed=99)   # piece from another bg

    desk = CaptchaDesk(Unsure(), open_browser=False, autosolve=True)
    try:
        assert desk.solve(deadline=time.time() + 1.5) is None  # no auto answer, deadline hits
        assert any("unsure" in line for line in desk._log)
    finally:
        desk.stop()


def test_booker_autosolve_returns_without_starting_the_alarm():
    client = type("Client", (), {})()
    desk = CaptchaDesk(AutoBackend(), open_browser=False, autosolve=True)
    plan = Plan(venue=None, date="2026-11-03", times=[("19:00", None)], courts=[5],
                alarm=True)
    booker = Booker(client, desk, plan)
    try:
        result = booker._get_verification("test", deadline=time.time() + 10)
    finally:
        desk.stop()
    assert result and result.value == "verified:100"
    assert booker.alarm._thread is None          # never rang
    assert not booker._page_raised               # never popped the browser


def test_booker_human_path_waits_when_nobody_solves():
    client = type("Client", (), {})()
    desk = CaptchaDesk(AutoBackend(), open_browser=False)   # autosolve off
    desk.start()                                            # desk.url needs it
    plan = Plan(venue=None, date="2026-11-03", times=[("19:00", None)], courts=[5], alarm=False)
    booker = Booker(client, desk, plan)
    try:
        result = booker._get_verification("test", deadline=time.time() + 1.0)
    finally:
        desk.stop()
    assert result is None   # deadline passed with no answer and no crash


