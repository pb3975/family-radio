"""Concept E, "Deck": a boombox with a top control deck and a slot handle in a
raised rear spine (Braun / Teenage Engineering). The knob sits on the
deck; the screen is a small landscape window in the front face.

Audio: Raspberry Pi Zero 2 W, Adafruit 5770 I2S amp, one Adafruit 1314 3" driver
behind a square dot field. UI: Waveshare ESP32-S3-Touch-AMOLED-1.8 (in its case,
turned landscape) plus an Adafruit 5880 encoder through the deck.
"""
import os

import lib
from lib import add, box, cut, cyl, hull, label, move, screen_art
from parts import (ENC_BOARD, ENC_BODY, ENC_BUSHING, PANEL_AT_ENC, WALL, amoled_clamp, amoled_mount, dot_grid,
                   encoder_cutter, knob, pi_back_panel, ref_amoled, ref_pi_stack, ref_speaker, rr_prism, speaker_seat,
                   square_field, eased_slot, ybar)

W, H, D = 210.0, 120.0, 100.0
R = 6.0
FRONT_T = 3.0
PANEL_T = 3.0
Y0, Y1 = -D / 2, D / 2
SPINE_D, RISE = 20.0, 28.0          # spine depth at the top; height above the deck
RAMP_Y = Y1 - SPINE_D - RISE        # where the 45 deg ramp leaves the deck
SPINE_INSET = 6.0                   # spine sides step in from the body sides
SLOT_X, SLOT_W, SLOT_Z0, SLOT_H = -8.0, 92.0, H + 4.0, 18.0
SPINE_SCREW_X, SPINE_SCREW_Y = (-86.0, -68.0, 52.0, 70.0), 30.0   # clear of the slot (x -54..38)
_C = WALL + 4.1
BACK_POSTS = [(sx * (W / 2 - _C), z) for sx in (-1, 1) for z in (_C, H - _C)]

SPK_X, SPK_Z = -50.0, 56.0
SCREEN_X, SCREEN_Z = 60.0, 72.0     # landscape; the clamp bar runs vertically below the deck encoder
KNOB_X, KNOB_Y, KNOB_D = 68.0, -24.0, 46.0   # clear of the slot (x -54..38) and the ramp
PI_XZ, AMP_XZ = (30.0, 34.0), (-40.0, 34.0)


def body():
    b = rr_prism(W, H, Y0, Y1, R, front=1.2, back=1.0, bed="front", name="deck-body")
    cut(b, rr_prism(W - 2 * WALL, H - 2 * WALL, Y0 + FRONT_T, Y1 + 1, R - WALL, z0=WALL, front=0, back=0))
    for x in SPINE_SCREW_X:
        cut(b, cyl(2.05, 12, (x, SPINE_SCREW_Y, H - WALL / 2), n=4))   # diamond: horizontal in the print

    fi = Y0 + FRONT_T
    cut(b, dot_grid(SPK_X, SPK_Z, 66, 66, Y0 - 1, fi + 1, pitch=5.0, d=3.0, inside=square_field))
    speaker_seat(b, SPK_X, SPK_Z, fi, seal="square")
    amoled_mount(b, SCREEN_X, SCREEN_Z, Y0, FRONT_T, landscape=True)

    # Encoder through the deck. Teardrop tip toward +Y: this wall is vertical in the
    # face-down print. Two ribs hang from the deck either side of the breakout so
    # turning the knob cannot spin it; their front ends slope 45 deg (no overhang).
    enc = encoder_cutter(WALL)
    tip = box((ENC_BUSHING + 0.4, ENC_BUSHING + 0.4, WALL + 2), (0, 0, -WALL / 2), r=0)
    lib.rotate(tip, 45, "Z")
    cut(tip, box((20, 20, 20), (0, -10, 0), r=0))
    add(enc, tip)
    cut(b, move(enc, (KNOB_X, KNOB_Y, H)))
    top, h = H - WALL + 0.3, ENC_BODY + 1.6
    ya, yb = KNOB_Y - ENC_BOARD / 2, KNOB_Y + ENC_BOARD / 2
    for dx in (-(ENC_BOARD / 2 + 1.5), ENC_BOARD / 2 + 1.5):
        x = KNOB_X + dx
        add(b, hull([(x + s, y, z) for s in (-1.2, 1.2) for y, z in
                     ((ya - h, top), (yb, top), (yb, top - h), (ya, top - h))]))

    for x, z in BACK_POSTS:
        add(b, ybar(x, fi - 0.5, Y1 - PANEL_T, z, r=4.5))
    return b


def spine():
    """Handle spine, a separate part so it prints back face down: the ramp faces up,
    the slot is vertical, and the bar over the slot grows from the bed (an integral
    spine would start the bar as a 92 mm unsupported bridge). Four screws come up
    through the deck into it from inside. The slot's back edge is flared 1.5 mm
    where the fingers wrap."""
    sw = W - 2 * SPINE_INSET
    s = rr_prism(sw, 2 * RISE, RAMP_Y, Y1, R, z0=H - RISE, front=0, back=1.2, bed="back", name="deck-spine")
    x0, x1 = -W / 2, W / 2
    cut(s, box((W, D + 2, RISE + 1), (0, 0, H - (RISE + 1) / 2), r=0))
    cut(s, hull([(x, y, z) for x in (x0, x1) for y, z in
                 ((RAMP_Y - 1, H - 1), (RAMP_Y - 1, H), (Y1 - SPINE_D, H + RISE), (Y1 - SPINE_D, H + RISE + 1),
                  (RAMP_Y - 1, H + RISE + 1))]))
    # Finger edges eased: 1.5 mm at 45 deg on the back face; a long gentle taper at the
    # ramp, where the slot ceiling meets the ramp at y = RAMP_Y + 22.
    lip = RAMP_Y + SLOT_Z0 + SLOT_H - H
    cut(s, eased_slot(SLOT_W, SLOT_H, SLOT_H / 2 - 0.5, SLOT_X, SLOT_Z0,
                      [(RAMP_Y - 1, 1.5), (lip + 1.5, 0), (Y1 - 1.5, 0), (Y1 + 0.01, 1.5)]))
    for x in SPINE_SCREW_X:
        cut(s, cyl(1.5, 14, (x, SPINE_SCREW_Y, H + 6.0), n=4))   # diamond pilot: horizontal in the print
    return s


def back_panel():
    p = rr_prism(W - 2 * WALL - 0.6, H - 2 * WALL - 0.6, Y1 - PANEL_T, Y1, R - WALL - 0.3,
                 z0=WALL + 0.3, front=0, back=0, bed="back", name="deck-back")
    return pi_back_panel(p, Y1, BACK_POSTS, PI_XZ, AMP_XZ)


def build():
    b, sp, p = body(), spine(), back_panel()
    cl = amoled_clamp("deck-clamp", SCREEN_X, SCREEN_Z, Y0, FRONT_T, landscape=True)
    k = knob(d=KNOB_D, h=18.0, name="deck-knob", flutes=24, flute_r=1.6, flute_out=0.5, full=True)
    move(k, (KNOB_X, KNOB_Y, H + 1.5))
    printed = [(b, (90, "X")), (sp, (-90, "X")), (p, (-90, "X")), (cl, (90, "X")), (k, (180, "X"))]
    enc = box((ENC_BOARD, ENC_BOARD, 1.6), (KNOB_X, KNOB_Y, H - PANEL_AT_ENC - ENC_BODY - 0.8), name="ref-encoder")
    ref = [*ref_amoled(SCREEN_X, SCREEN_Z, Y0, FRONT_T, landscape=True), ref_speaker(SPK_X, SPK_Z, Y0 + FRONT_T),
           enc, *ref_pi_stack(Y1, PI_XZ, AMP_XZ)]
    return printed, ref


GRAPHITE, GREY = (0.16, 0.17, 0.18), (0.42, 0.43, 0.44)
PALETTE = {"deck-body": GRAPHITE, "deck-spine": GRAPHITE, "deck-back": GREY, "deck-clamp": GREY,
           "deck-knob": (0.98, 0.33, 0.04)}


def paint(printed, ref):
    lib.paint(printed, ref, PALETTE, GRAPHITE)
    cream = (0.86, 0.84, 0.78)
    ref += [label("FAMILY RADIO", (SCREEN_X - 17, Y0 - 0.05, H - 12), size=3.4, align="LEFT", rgb=cream),
            label("E / DECK", (W / 2 - 12, Y0 - 0.05, 11), size=2.8, align="RIGHT", rgb=cream),
            label("VOL", (KNOB_X - KNOB_D / 2 - 10, KNOB_Y, H + 0.05), size=3.4, face="top", rgb=cream),
            *screen_art((SCREEN_X, Y0 + FRONT_T - 0.75, SCREEN_Z), 34.94, 28.7)]


def renders(printed, ref, out):
    paint(printed, ref)
    lib.render(os.path.join(out, "deck-front.jpg"), printed + ref, cam_dir=(0.6, -1.7, 0.95), lens=70)
    lib.render_section(os.path.join(out, "deck-section.jpg"), lib.solid(printed + ref), x=KNOB_X,
                       cam_dir=(1.0, -0.35, 0.4))
