"""Concept A, "Pebble": an upright, mug-sized radio. The screen is its face, a
smile of holes is its speaker grille, and the knob sits on top like a hat.

Hardware direction P-01: Waveshare ESP32-S3-Touch-AMOLED-1.8, kept in its own
black case (screen, ES8311 codec, onboard speaker), plus an Adafruit 5880 I2C
encoder panel-mounted through the top wall.
"""
import os

import lib
from lib import add, box, cut, cyl, keep, move
from parts import (CASE_H, CASE_W, ENC_BOARD, ENC_BODY, ENC_BUSHING, M25_CLEAR, PANEL_AT_ENC, WALL, amoled_clamp,
                   amoled_mount, arc_holes, encoder_cutter, knob, ref_amoled, rr_prism, ybar)

W, H, D = 110.0, 114.0, 70.0
R_TOP, R_BOT = 38.0, 16.0
FRONT_T = 3.0
PANEL_T = 3.0
Y0, Y1 = -D / 2, D / 2
BACK_POSTS = [(x, z) for x in (-(W / 2 - 12), W / 2 - 12) for z in (14.0, H - 40.0)]

SCREEN_Z = 74.0

KNOB_D = 58.0
SMILE_Z = 38.0


def profile(w, h, y0, y1, z0=0.0, inset=0.0, front=2.0, back=1.2, bed=None, name="p"):
    """Tombstone profile: generous top corners, tight bottom corners for a stable base."""
    top = rr_prism(w, h, y0, y1, R_TOP - inset, z0=z0, front=front, back=back, bed=bed, name=name)
    base = rr_prism(w, h * 0.5, y0, y1, R_BOT - inset, z0=z0, front=front, back=back, bed=bed)
    return add(top, base)


def body():
    b = profile(W, H, Y0, Y1, bed="front", name="pebble-body")
    cut(b, profile(W - 2 * WALL, H - 2 * WALL, Y0 + FRONT_T, Y1 + 1, z0=WALL, inset=WALL, front=0, back=0))

    amoled_mount(b, 0, SCREEN_Z, Y0, FRONT_T)

    # Speaker: three arcs of holes form a smile under the face. The onboard speaker's
    # sound port position is UNVERIFIED; see docs/enclosure-concepts.md.
    for rad, n in ((20.0, 9), (25.0, 11), (30.0, 13)):
        cut(b, arc_holes(0, SMILE_Z + 24, rad, 228, 312, n, Y0 - 1, Y0 + FRONT_T + 1))

    # Encoder through the top wall at mid-depth. Two ribs from the front wall key the
    # breakout so turning the knob cannot spin it on its nut.
    enc = encoder_cutter(WALL)
    # Teardrop tip toward +Y: this wall is vertical in the face-down print.
    tip = box((ENC_BUSHING + 0.4, ENC_BUSHING + 0.4, WALL + 2), (0, 0, -WALL / 2), r=0)
    lib.rotate(tip, 45, "Z")
    cut(tip, box((20, 20, 20), (0, -10, 0), r=0))
    add(enc, tip)
    cut(b, move(enc, (0, 0, H)))
    rib_y1 = -ENC_BOARD / 2 - 0.3
    for x in (-8.0, 8.0):
        rib_h = ENC_BODY + 1.6
        add(b, box((2.4, rib_y1 - (Y0 + FRONT_T) + 0.5, rib_h),
                   (x, (Y0 + FRONT_T + rib_y1) / 2 - 0.25, H - WALL - rib_h / 2 + 0.3), r=0))

    for x, z in BACK_POSTS:
        add(b, ybar(x, Y0 + FRONT_T - 0.5, Y1 - PANEL_T, z, r=4.0))
    return b


def clamp():
    return amoled_clamp("pebble-clamp", 0, SCREEN_Z, Y0, FRONT_T)


def back_panel():
    p = profile(W - 2 * WALL - 0.6, H - 2 * WALL - 0.6, Y1 - PANEL_T, Y1, z0=WALL + 0.3,
                inset=WALL + 0.3, front=0, back=1.0, bed="back", name="pebble-back")
    for x, z in BACK_POSTS:
        cut(p, cyl(M25_CLEAR / 2, 10, (x, Y1, z), axis="Y", n=24))
    # USB-C cable exits low on the back; a zip tie through two slots relieves strain
    # so a tug on the cable never reaches the board's connector.
    cut(p, box((12, 10, 7), (0, Y1, 14), r=0))
    for dx in (-6.0, 6.0):
        cut(p, box((3.5, 10, 2.0), (dx, Y1, 26), r=0))
    return p


def coupon(b):
    """Front-wall fit test around the screen: check case fit and window alignment."""
    c = lib.dup(b, "pebble-coupon-screen")
    return keep(c, box((CASE_W + 34, 30, CASE_H + 14), (0, Y0 + 10, SCREEN_Z), r=0))


def build():
    b, cl, p = body(), clamp(), back_panel()
    k = knob(d=KNOB_D, name="pebble-knob")
    move(k, (0, 0, H + 1.5))
    printed = [(b, (90, "X")), (cl, (90, "X")), (p, (-90, "X")), (k, (180, "X")),
               (coupon(b), (90, "X"))]
    enc = box((ENC_BOARD, ENC_BOARD, 1.6), (0, 0, H - PANEL_AT_ENC - ENC_BODY - 0.8), name="ref-encoder")
    return printed, [*ref_amoled(0, SCREEN_Z, Y0, FRONT_T), enc]


PALETTE = {"pebble-body": (0.98, 0.76, 0.22), "pebble-back": (0.96, 0.92, 0.84),
           "pebble-clamp": (0.96, 0.92, 0.84), "pebble-knob": (0.9, 0.34, 0.3)}


def paint(printed, ref):
    lib.paint(printed, ref, PALETTE, (0.98, 0.76, 0.22))


def renders(printed, ref, out):
    shown = [o for o in printed if "coupon" not in o.name]
    paint(printed, ref)
    lib.render(os.path.join(out, "pebble-front.jpg"), shown + ref, cam_dir=(0.9, -1.8, 0.8))
    lib.render_section(os.path.join(out, "pebble-section.jpg"), shown + ref, x=0.0)
