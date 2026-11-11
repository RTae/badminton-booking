"""Try to solve the site's slider captcha automatically (computer vision).

The challenge images arrive at display size: the background is 310x155 RGB
and the jigsaw piece is 47x155 RGBA. The piece is a cutout of the background,
and the hole it came from is visible in the background (usually darkened).
We locate the hole with normalized cross-correlation of the piece against
the background (using the piece's alpha channel as a mask) and cross-check
with an edge-alignment test: the boundary of the hole in the background must
match the outline of the piece regardless of any brightness change.

If the two methods disagree or the match is weak, we return ``None`` and let
a human solve it. The solver only ever computes the slider position; it does
not simulate mouse behaviour, because the site's check endpoint receives just
the final point.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

BG_W, BG_H = 310, 155
PIECE_W = 47

# How certain the match must be before we submit it ourselves.
MIN_CONFIDENCE = 0.8    # peak correlation score
MIN_MARGIN = 0.05       # peak must beat the runner-up by at least this
AGREEMENT_PX = 3        # the two methods must agree within this many pixels


def _decode_image(b64: str) -> np.ndarray | None:
    import cv2
    import numpy as np

    try:
        arr = np.frombuffer(base64.b64decode(b64), np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    except (ValueError, TypeError, cv2.error):  # not base64 / not an image
        return None
    return img if img is not None and img.ndim in (2, 3) else None


def solve(bg_b64: str, piece_b64: str) -> int | None:
    """Return the slider x for a blockPuzzle challenge, or ``None`` if unsure."""
    import cv2
    import numpy as np

    bg = _decode_image(bg_b64)
    piece = _decode_image(piece_b64)
    if bg is None or piece is None:
        return None

    # Normalise to the display size, in case the server ever sends bigger images.
    if (bg.shape[0], bg.shape[1]) != (BG_H, BG_W):
        bg = cv2.resize(bg, (BG_W, BG_H), interpolation=cv2.INTER_AREA)
    scale = BG_H / piece.shape[0]
    if scale != 1.0:
        piece = cv2.resize(piece, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    piece = piece[:BG_H, :max(PIECE_W, piece.shape[1])]

    bg_gray = cv2.cvtColor(bg[:, :, :3], cv2.COLOR_BGR2GRAY).astype(np.float32)
    piece_bgr = piece[:, :, :3]
    alpha = piece[:, :, 3] if piece.ndim == 3 and piece.shape[2] == 4 else \
        np.full(piece.shape[:2], 255, np.uint8)
    mask = (alpha > 10).astype(np.uint8) * 255
    piece_gray = cv2.cvtColor(piece_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    if piece_gray.shape != (BG_H, PIECE_W):
        return None

    # 1. Correlation: where in the background does the piece content appear?
    res = cv2.matchTemplate(bg_gray, piece_gray, cv2.TM_CCOEFF_NORMED, mask=mask)
    _, conf, _, loc = cv2.minMaxLoc(res)
    x_corr = loc[0]
    margin = conf - _runner_up(res, x_corr)

    # 2. Geometry: the hole's outline in the background must match the piece's
    #    outline, whatever the colours do.
    x_edge = _edge_align_x(bg, alpha)

    if x_edge is None or abs(x_corr - x_edge) > AGREEMENT_PX:
        return None
    if conf < MIN_CONFIDENCE or margin < MIN_MARGIN:
        return None
    return int(round((x_corr + x_edge) / 2))


def _runner_up(res: np.ndarray, peak_x: int) -> float:
    """The best score outside a small neighbourhood around the peak."""
    import numpy as np

    flat = res.ravel()
    lo, hi = max(0, peak_x - 6), min(res.shape[1], peak_x + 7)
    mask = np.ones(res.shape, dtype=bool)
    mask[:, lo:hi] = False
    rest = flat[mask.ravel()]
    return float(rest.max()) if rest.size else 0.0


def _edge_align_x(bg: np.ndarray, alpha: np.ndarray) -> int | None:
    """Where does the piece's outline best align with edges in the background?"""
    import cv2
    import numpy as np

    bg_gray = cv2.cvtColor(bg[:, :, :3], cv2.COLOR_BGR2GRAY)
    bg_edges = cv2.magnitude(cv2.Sobel(bg_gray, cv2.CV_32F, 1, 0),
                             cv2.Sobel(bg_gray, cv2.CV_32F, 0, 1))
    a = alpha.astype(np.float32)
    outline = cv2.magnitude(cv2.Sobel(a, cv2.CV_32F, 1, 0), cv2.Sobel(a, cv2.CV_32F, 0, 1))
    total = outline.sum()
    if total <= 0:
        return None
    outline /= total
    scores = np.array([(bg_edges[:, x:x + PIECE_W] * outline).sum()
                       for x in range(bg_gray.shape[1] - PIECE_W + 1)])
    return int(np.argmax(scores))
