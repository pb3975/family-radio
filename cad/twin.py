"""Concept D, "Twin": a symmetric boombox. Two 3-inch drivers behind orthogonal
dot grilles flank an M5Stack Dial (round screen in the knob), under a flat bar
handle. Teenage Engineering-style: one neutral body, one orange accent.

Audio: Raspberry Pi Zero 2 W, Adafruit 5770 I2S amp, two Adafruit 1314 4 ohm
drivers wired in series (8 ohm, mono; the amp supports 4 or 8 ohm). The second
driver is a look and loudness choice, not stereo.
"""
import os

import lib
from lib import add, box, cut, cyl, label, screen_art
from parts import (DIAL_THREAD_D, WALL, dot_grid, eased_slot, pi_back_panel, ref_dial, ref_pi_stack, ref_speaker,
                   rr_prism, speaker_seat, ybar)

W, H, D = 236.0, 124.0, 72.0
R = 8.0                   # profile corner radius: crisp, not pillowy
FRONT_T = 3.0             # front wall; also the Dial's clamping panel
PANEL_T = 3.0
Y0, Y1 = -D / 2, D / 2
_C = WALL + 4.1           # corner posts merge into the corner wall
BACK_POSTS = [(sx * (W / 2 - _C), z) for sx in (-1, 1) for z in (_C, H - _C)]

SPK_XS, SPK_Z = (-70.0, 70.0), H / 2   # 78 mm frames span 31..109 mm from centre
DIAL_X, DIAL_Z = 0.0, H / 2            # 50.8 mm locking ring clears the frames by 5.6 mm
PI_XZ, AMP_XZ = (0.0, 34.0), (-58.0, 34.0)

HANDLE_SPAN = 150.0       # centre-to-centre of the handle feet
HANDLE_RISE, HANDLE_BAR, HANDLE_T = 44.0, 14.0, 18.0


def body():
    b = rr_prism(W, H, Y0, Y1, R, front=1.2, back=1.0, bed="front", name="twin-body")
    cut(b, rr_prism(W - 2 * WALL, H - 2 * WALL, Y0 + FRONT_T, Y1 + 1, R - WALL, z0=WALL, front=0, back=0))
    fi = Y0 + FRONT_T
    for x in SPK_XS:
        cut(b, dot_grid(x, SPK_Z, 66, 66, Y0 - 1, fi + 1, pitch=5.0, d=3.0,
                        inside=lambda u, v: u * u + v * v <= 31.5 ** 2))
        speaker_seat(b, x, SPK_Z, fi)
    cut(b, cyl(DIAL_THREAD_D / 2 + 0.3, FRONT_T + 4, (DIAL_X, Y0 + FRONT_T / 2, DIAL_Z), axis="Y", n=128))
    for x, z in BACK_POSTS:
        add(b, ybar(x, fi - 0.5, Y1 - PANEL_T, z, r=4.5))
    for x in (-HANDLE_SPAN / 2, HANDLE_SPAN / 2):
        cut(b, cyl(2.05, 12, (x, 0, H - WALL / 2), n=4))   # diamond: horizontal in the print
    return b


def back_panel():
    p = rr_prism(W - 2 * WALL - 0.6, H - 2 * WALL - 0.6, Y1 - PANEL_T, Y1, R - WALL - 0.3,
                 z0=WALL + 0.3, front=0, back=0.8, bed="back", name="twin-back")
    return pi_back_panel(p, Y1, BACK_POSTS, PI_XZ, AMP_XZ)


def handle():
    """Flat bar handle, printed lying on its side so layers run along the bar.
    Screws come up through the top wall into the feet (adult-only, from inside)."""
    w = HANDLE_SPAN + 18.0
    h = rr_prism(w, 2 * HANDLE_RISE, -HANDLE_T / 2, HANDLE_T / 2, 9.0, z0=H - HANDLE_RISE, front=2.0, back=3.0,
                 bed="front", name="twin-handle")
    # Opening with 1.5 mm 45 deg chamfers on both faces, so no sharp edge meets the fingers.
    t = HANDLE_T / 2
    cut(h, eased_slot(w - 36.0, 2 * (HANDLE_RISE - HANDLE_BAR), 4.0, 0, H - HANDLE_RISE + HANDLE_BAR,
                      [(-t - 0.01, 1.5), (-t + 1.5, 0), (t - 1.5, 0), (t + 0.01, 1.5)]))
    cut(h, box((w + 10, HANDLE_T + 10, HANDLE_RISE + 1), (0, 0, H - (HANDLE_RISE + 1) / 2), r=0))
    for x in (-HANDLE_SPAN / 2, HANDLE_SPAN / 2):
        cut(h, cyl(1.5, 14, (x, 0, H + 6.0), n=4))   # diamond pilot: horizontal in the print
    return h


def build():
    b, p, hd = body(), back_panel(), handle()
    printed = [(b, (90, "X")), (p, (-90, "X")), (hd, (90, "X"))]
    ref = [*ref_dial(DIAL_X, DIAL_Z, Y0), *[ref_speaker(x, SPK_Z, Y0 + FRONT_T) for x in SPK_XS],
           *ref_pi_stack(Y1, PI_XZ, AMP_XZ)]
    return printed, ref


PALETTE = {"twin-body": (0.80, 0.80, 0.78), "twin-back": (0.42, 0.43, 0.44), "twin-handle": (0.98, 0.33, 0.04)}


def paint(printed, ref):
    lib.paint(printed, ref, PALETTE, (0.80, 0.80, 0.78))
    ink = (0.08, 0.08, 0.08)
    ref += [label("FAMILY RADIO", (-W / 2 + 12, Y0 - 0.05, H - 11), size=3.6, align="LEFT", rgb=ink),
            label("D / TWIN", (W / 2 - 12, Y0 - 0.05, 11), size=2.8, align="RIGHT", rgb=ink),
            label("VOL  •  PLAY", (DIAL_X, Y0 - 0.05, DIAL_Z - 33), size=2.6, rgb=ink),
            *screen_art((DIAL_X, Y0 - 13.25, DIAL_Z), 24, 24)]


def renders(printed, ref, out):
    paint(printed, ref)
    lib.render(os.path.join(out, "twin-front.jpg"), printed + ref, cam_dir=(0.55, -1.8, 0.55), lens=70)
    lib.render_section(os.path.join(out, "twin-section.jpg"), lib.solid(printed + ref), x=SPK_XS[1],
                       cam_dir=(1.0, -0.45, 0.35))
