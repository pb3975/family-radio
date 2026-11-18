"""Concept F, "Frame": exposed construction. A light core body is held between two
graphite side cheeks that rise above it and carry a bar handle. Every visible screw
is a real joint (Teenage Engineering OP-1 field / TX-6 spirit).

Audio: Raspberry Pi Zero 2 W, Adafruit 5770 I2S amp, one Adafruit 1314 3" driver
behind a square dot field. UI: Waveshare ESP32-S3-Touch-AMOLED-1.8 (in its case)
above an Adafruit 5880 encoder, both on the front.
"""
import math
import os

import lib
from lib import add, box, cut, cyl, hull, label, move, rotate, screen_art
from parts import (ENC_BOARD, ENC_BODY, M25_CLEAR, PANEL_AT_ENC, WALL, amoled_clamp, amoled_mount, dot_grid,
                   encoder_cutter, knob, pi_back_panel, ref_amoled, ref_pi_stack, ref_speaker, rr_prism, speaker_seat,
                   square_field, ybar)

W, H, D = 180.0, 120.0, 80.0        # core body; the cheeks add 2 x CHEEK_T
R = 4.0
FRONT_T = 3.0
PANEL_T = 3.0
Y0, Y1 = -D / 2, D / 2
CHEEK_T, CHEEK_RISE = 8.0, 48.0
BAR_W, BAR_H, BAR_Z = 22.0, 14.0, H + 38.0  # 31 mm of hand room under the bar
# Four posts run front to back inside the side walls. They take the back-panel screws
# at their back ends and the cheek screws through the side walls.
_C = WALL + 3.5
POSTS = [(sx * (W / 2 - _C), z) for sx in (-1, 1) for z in (24.0, 96.0)]
CHEEK_SCREW_Y = (-18.0, 12.0)
BAR_SCREW_Y = (-5.0, 5.0)

SPK_X, SPK_Z = -38.0, 60.0
SCREEN_X, SCREEN_Z = 46.0, 88.0
KNOB_X, KNOB_Z, KNOB_D = 46.0, 35.0, 46.0
PI_XZ, AMP_XZ = (10.0, 36.0), (-50.0, 36.0)


def body():
    b = rr_prism(W, H, Y0, Y1, R, front=1.2, back=1.0, bed="front", name="frame-body")
    cut(b, rr_prism(W - 2 * WALL, H - 2 * WALL, Y0 + FRONT_T, Y1 + 1, R - WALL, z0=WALL, front=0, back=0))
    fi = Y0 + FRONT_T
    cut(b, dot_grid(SPK_X, SPK_Z, 66, 66, Y0 - 1, fi + 1, pitch=5.0, d=3.0, inside=square_field))
    speaker_seat(b, SPK_X, SPK_Z, fi, seal="square")
    amoled_mount(b, SCREEN_X, SCREEN_Z, Y0, FRONT_T)

    # Encoder on the front wall (vertical in the print); ribs key the breakout board.
    enc = encoder_cutter(FRONT_T)
    rotate(enc, 90, "X")
    cut(b, move(enc, (KNOB_X, Y0, KNOB_Z)))
    for dx in (-(ENC_BOARD / 2 + 1.5), ENC_BOARD / 2 + 1.5):
        add(b, box((2.4, ENC_BODY + 2.5, 16), (KNOB_X + dx, fi + (ENC_BODY + 2.5) / 2 - 0.3, KNOB_Z), r=0))

    for x, z in POSTS:
        add(b, ybar(x, fi - 0.5, Y1 - PANEL_T, z, r=4.5))
        for y in CHEEK_SCREW_Y:   # diamond pilots: horizontal in the print
            cut(b, cyl(1.1, 20, (math.copysign(W / 2, x), y, z), axis="X", n=4))
    return b


def back_panel():
    p = rr_prism(W - 2 * WALL - 0.6, H - 2 * WALL - 0.6, Y1 - PANEL_T, Y1, R - WALL - 0.3,
                 z0=WALL + 0.3, front=0, back=0, bed="back", name="frame-back")
    return pi_back_panel(p, Y1, POSTS, PI_XZ, AMP_XZ)


def cheek(side):
    """Side cheek, printed outer face down."""
    name = f"frame-cheek-{'l' if side < 0 else 'r'}"
    hh = H + CHEEK_RISE
    c = rr_prism(D, hh, -CHEEK_T / 2, CHEEK_T / 2, 10.0, front=1.5, back=0.8, bed="front", name=name)
    rotate(c, 90 * side, "Z")                     # outer (bed) face now faces outward along X
    move(c, (side * (W / 2 + CHEEK_T / 2), 0, 0))
    holes = [(y, z) for _, z in POSTS[:2] for y in CHEEK_SCREW_Y] + [(y, BAR_Z) for y in BAR_SCREW_Y]
    for y, z in holes:
        cut(c, cyl(M25_CLEAR / 2, CHEEK_T + 2, (side * (W / 2 + CHEEK_T / 2), y, z), axis="X", n=24))
    return c


def bar():
    """Handle bar between the cheeks. Printed top face down: chamfered top edges on
    the bed, round underside where the fingers wrap."""
    r = 6.0
    prof = [(sy * (BAR_W / 2 - 3), BAR_H / 2) for sy in (-1, 1)] + [(sy * BAR_W / 2, BAR_H / 2 - 3) for sy in (-1, 1)]
    for cy in (-1, 1):
        for i in range(9):
            a = math.radians(-90 + cy * 90 * i / 8)
            prof.append((cy * (BAR_W / 2 - r) + r * math.cos(a), -BAR_H / 2 + r + r * math.sin(a)))
    b = hull([(x, y, z + BAR_Z) for x in (-W / 2, W / 2) for y, z in prof])
    b.name = "frame-bar"
    for x in (-W / 2, W / 2):
        for y in BAR_SCREW_Y:
            cut(b, cyl(1.1, 24, (x, y, BAR_Z), axis="X", n=4))   # diamond pilot: horizontal in the print
    return b


def build():
    b, p, br = body(), back_panel(), bar()
    cl, cr = cheek(-1), cheek(1)
    sc = amoled_clamp("frame-clamp", SCREEN_X, SCREEN_Z, Y0, FRONT_T)
    k = knob(d=KNOB_D, h=18.0, name="frame-knob", flutes=24, flute_r=1.6, flute_out=0.5, full=True)
    rotate(k, 90, "X")
    move(k, (KNOB_X, Y0 - 1.5, KNOB_Z))
    printed = [(b, (90, "X")), (p, (-90, "X")), (cl, (-90, "Y")), (cr, (90, "Y")), (br, (180, "X")),
               (sc, (90, "X")), (k, (90, "X"))]
    enc = box((ENC_BOARD, 1.6, ENC_BOARD), (KNOB_X, Y0 + PANEL_AT_ENC + ENC_BODY + 0.8, KNOB_Z), name="ref-encoder")
    screws = [cyl(2.3, 1.6, (sx * (W / 2 + CHEEK_T + 0.8), y, z), axis="X", n=32, name="ref-screw")
              for sx in (-1, 1) for y, z in [(y, z) for _, z in POSTS[:2] for y in CHEEK_SCREW_Y]
              + [(y, BAR_Z) for y in BAR_SCREW_Y]]
    ref = [*ref_amoled(SCREEN_X, SCREEN_Z, Y0, FRONT_T), ref_speaker(SPK_X, SPK_Z, Y0 + FRONT_T), enc,
           *ref_pi_stack(Y1, PI_XZ, AMP_XZ), *screws]
    return printed, ref


GRAPHITE = (0.16, 0.17, 0.18)
PALETTE = {"frame-body": (0.82, 0.82, 0.80), "frame-back": (0.42, 0.43, 0.44), "frame-clamp": (0.42, 0.43, 0.44),
           "frame-cheek-l": GRAPHITE, "frame-cheek-r": GRAPHITE, "frame-bar": GRAPHITE,
           "frame-knob": (0.98, 0.33, 0.04)}


def paint(printed, ref):
    lib.paint(printed, ref, PALETTE, (0.82, 0.82, 0.80))
    ink = (0.08, 0.08, 0.08)
    ref += [label("FAMILY RADIO", (SPK_X - 33, Y0 - 0.05, H - 11), size=3.4, align="LEFT", rgb=ink),
            label("F / FRAME", (SPK_X - 33, Y0 - 0.05, 10), size=2.8, align="LEFT", rgb=ink),
            label("VOL", (KNOB_X + KNOB_D / 2 + 9, Y0 - 0.05, KNOB_Z), size=2.6, rgb=ink),
            *screen_art((SCREEN_X, Y0 + FRONT_T - 0.75, SCREEN_Z), 28.7, 34.94)]


def renders(printed, ref, out):
    paint(printed, ref)
    lib.render(os.path.join(out, "frame-front.jpg"), printed + ref, cam_dir=(0.6, -1.8, 0.6), lens=70)
    lib.render_section(os.path.join(out, "frame-section.jpg"), lib.solid(printed + ref), x=KNOB_X,
                       cam_dir=(1.0, -0.45, 0.35))
