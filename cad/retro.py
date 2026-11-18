"""Concept B, "Retro": a horizontal tabletop radio with a carry handle.

Hardware direction P-02: a Raspberry Pi Zero 2 W plays audio through an I2S amp
and a 3-inch speaker. An M5Stack Dial, panel-mounted through the front, is both
the knob and the round screen.
"""
import math
import os

import lib
from lib import add, box, cut, cyl, keep
from parts import (DIAL_THREAD_D, WALL, pi_back_panel, ref_dial, ref_pi_stack, ref_speaker, ring_grille, rr_prism,
                   speaker_seat, ybar)

W, H, D = 214.0, 138.0, 96.0
R = 30.0                  # profile corner radius
FRONT_T = 3.0             # front wall; also the Dial's clamping panel
PANEL_T = 3.0
Y0, Y1 = -D / 2, D / 2
# Back-panel posts sit in the four corners, merged into the curved wall (no thin gap).
_C = (R - WALL - 4.5 + 0.3) / math.sqrt(2)
BACK_POSTS = [(sx * (W / 2 - R + _C), z) for sx in (-1, 1) for z in (R - _C, H - R + _C)]

DIAL_X, DIAL_Z = 56.0, 70.0

SPK_X, SPK_Z = -50.0, 70.0
PI_X, PI_Z = 50.0, 34.0   # Pi board centre on the back panel (front view coordinates)
AMP_X, AMP_Z = 6.0, 34.0

HANDLE_SPAN = 128.0       # centre-to-centre of the handle feet


def body():
    b = rr_prism(W, H, Y0, Y1, R, front=2.0, back=1.2, bed="front", name="retro-body")
    cut(b, rr_prism(W - 2 * WALL, H - 2 * WALL, Y0 + FRONT_T, Y1 + 1, R - WALL, z0=WALL, front=0, back=0))
    fi = Y0 + FRONT_T         # inside face of the front wall

    # Speaker: concentric grille over the cone, a sealing ring that meets the frame so
    # sound goes out of the grille rather than into the box, and four screw posts.
    cut(b, ring_grille(SPK_X, SPK_Z, 31.0, Y0 - 1, fi + 1))
    speaker_seat(b, SPK_X, SPK_Z, fi)

    # Dial: clearance hole for the threaded body; the locking ring clamps from inside.
    cut(b, cyl(DIAL_THREAD_D / 2 + 0.3, FRONT_T + 4, (DIAL_X, Y0 + FRONT_T / 2, DIAL_Z), axis="Y", n=128))

    for x, z in BACK_POSTS:
        add(b, ybar(x, fi - 0.5, Y1 - PANEL_T, z, r=4.5))
    # Handle feet screw up through the top wall.
    for x in (-HANDLE_SPAN / 2, HANDLE_SPAN / 2):
        cut(b, cyl(2.05, 12, (x, 0, H - WALL / 2), n=4))   # diamond: horizontal in the print
    return b


def back_panel():
    p = rr_prism(W - 2 * WALL - 0.6, H - 2 * WALL - 0.6, Y1 - PANEL_T, Y1, R - WALL - 0.3,
                 z0=WALL + 0.3, front=0, back=1.0, bed="back", name="retro-back")
    return pi_back_panel(p, Y1, BACK_POSTS, (PI_X, PI_Z), (AMP_X, AMP_Z))


def handle():
    """Arch handle printed lying flat so layers run along the arch (strong)."""
    t, bar, rise = 22.0, 16.0, 44.0
    w = HANDLE_SPAN + bar
    h = rr_prism(w, 2 * rise, -t / 2, t / 2, 26.0, z0=H - rise, front=2.0, back=4.0, bed="front",
                 name="retro-handle")
    cut(h, rr_prism(w - 2 * bar, 2 * (rise - bar), -t, t, 12.0, z0=H - rise + bar, front=0, back=0))
    cut(h, box((w + 10, t + 10, rise + 1), (0, 0, H - (rise + 1) / 2), r=0))
    for x in (-HANDLE_SPAN / 2, HANDLE_SPAN / 2):
        cut(h, cyl(1.5, 14, (x, 0, H + 6.0), n=4))   # diamond pilot: horizontal in the print
    return h


def coupon(b):
    """Front-wall fit test around the Dial: check thread clearance and ring clamping."""
    c = lib.dup(b, "retro-coupon-dial")
    return keep(c, box((72, 20, 72), (DIAL_X, Y0 + 8, DIAL_Z), r=0))


def build():
    b, p, hd = body(), back_panel(), handle()
    printed = [(b, (90, "X")), (p, (-90, "X")), (hd, (90, "X")), (coupon(b), (90, "X"))]
    spk = ref_speaker(SPK_X, SPK_Z, Y0 + FRONT_T)
    return printed, [*ref_dial(DIAL_X, DIAL_Z, Y0), spk, *ref_pi_stack(Y1, (PI_X, PI_Z), (AMP_X, AMP_Z))]


PALETTE = {"retro-body": (0.16, 0.55, 0.56), "retro-back": (0.96, 0.92, 0.84),
           "retro-handle": (0.97, 0.66, 0.2)}


def paint(printed, ref):
    lib.paint(printed, ref, PALETTE, (0.16, 0.55, 0.56))


def renders(printed, ref, out):
    shown = [o for o in printed if "coupon" not in o.name]
    paint(printed, ref)
    lib.render(os.path.join(out, "retro-front.jpg"), shown + ref, cam_dir=(0.9, -1.8, 0.7))
    lib.render_section(os.path.join(out, "retro-section.jpg"), shown + ref, x=DIAL_X,
                       cam_dir=(1.0, -0.5, 0.35))
