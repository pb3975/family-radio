"""Concept C, "Wedge": a sloped desk console. The screen tilts up toward a child at a
table, and a big knob sits to its right where a hand won't cover the screen.

Hardware direction P-01 variant: M5Stack CoreS3 (screen, 1 W speaker and amp in
one) plus an Adafruit 5880 I2C encoder panel-mounted through the faceplate.
"""
import math
import os

import lib
from lib import add, box, cut, cyl, keep, move, rotate
from parts import ENC_BOARD, ENC_BODY, M25_CLEAR, M25_PILOT, PANEL_AT_ENC, WALL, encoder_cutter, knob

W, D = 168.0, 108.0
H_FRONT, H_BACK = 22.0, 95.0
R = 22.0                       # plan corner radius
PLATE_T = 3.0
SLOPE = math.degrees(math.atan2(H_BACK - H_FRONT, D))
PLATE_POSTS = [(x, v) for x in (-(W / 2 - 13), W / 2 - 13) for v in (-40.0, 40.0)]

# M5Stack CoreS3 main unit without DinBase (M5Stack docs): 54 x 54 x 15.5. Speaker
# grille on its right side face; POWER on the left side (official STL, inferred).
CORE, CORE_DEPTH = 54.0, 15.5
CORE_WIN = 48.0                # faceplate window; a 3 mm lip holds the CoreS3 front
CORE_U, CORE_V = -30.0, 4.0    # across the plate (x) and up the slope from its centre
CLAMP_DV = CORE / 2 + 7.0      # clamp-bar bosses above and below the CoreS3
CLAMP_SQUEEZE = 0.4            # bar flexes this much when tightened; add 1 mm foam if loose

KNOB_U, KNOB_V = 46.0, 4.0
KNOB_D = 60.0


def plate_xyz(u, v, depth=0.0):
    """World point at (u across, v up the slope from its centre), `depth` mm inward
    from the faceplate's outer surface."""
    a = math.radians(SLOPE)
    return (u, v * math.cos(a) + depth * math.sin(a), (H_FRONT + H_BACK) / 2 + v * math.sin(a) - depth * math.cos(a))


def on_plate(o, u, v, depth=0.0):
    """Tilt an object built around the origin (+Z = outward) onto the slope and place it."""
    rotate(o, SLOPE, "X")
    return move(o, plate_xyz(u, v, depth))


def slab(depth0, depth1):
    """Region between two planes parallel to the slope, depth0..depth1 inward."""
    return on_plate(box((600, 600, depth1 - depth0), (0, 0, 0)), 0, 0, (depth0 + depth1) / 2)


def outer_block():
    b = box((W, D, H_BACK + 10), (0, 0, (H_BACK + 10) / 2), r=R, seg=12, vertical_only=True)
    return lib.round_edges(b, 1.5, seg=1, angle=60)   # 45 deg bottom chamfer (on the bed); top is cut away


def body():
    b = outer_block()
    b.name = "wedge-body"
    cut(b, slab(-200, PLATE_T))                  # everything above the faceplate's underside
    cut(b, box((W - 2 * WALL, D - 2 * WALL, H_BACK + 20), (0, 0, WALL + (H_BACK + 20) / 2),
               r=R - WALL, seg=12, vertical_only=True))
    # Four posts from the floor up to the faceplate, printed straight up with no supports.
    # The pilot runs along the plate normal (the screw's axis), which drifts back as it
    # goes down, so each post is fat and offset back to keep the pilot inside it.
    pilot = 9.0
    drift = pilot * math.sin(math.radians(SLOPE))
    for u, v in PLATE_POSTS:
        px, py, pz = plate_xyz(u, v, PLATE_T)
        post = cyl(7.0, pz + 20, (px, py + drift / 2, (pz + 20) / 2))
        cut(post, slab(-200, PLATE_T))
        cut(post, on_plate(cyl(M25_PILOT / 2, pilot + 1, (0, 0, -(pilot + 1) / 2 + 0.5), n=24), u, v, PLATE_T))
        add(b, post)
    # Power cable exit at the back with a 45 deg gable top (prints without support),
    # and a zip-tie anchor on the floor so a tug never reaches the CoreS3's port.
    cut(b, box((12, 20, 7), (40, D / 2, WALL + 3.5), r=0))
    gable = box((8.5, 20, 8.5), (0, 0, 0), r=0)
    rotate(gable, 45, "Y")
    cut(b, move(gable, (40, D / 2, WALL + 7)))
    anchor = box((12, 7, 6), (40, D / 2 - 16, WALL + 3 - 0.3), r=0)
    tunnel = box((14, 3.0, 3.0), (0, 0, 0), r=0)
    rotate(tunnel, 45, "X")                       # diamond section: self-supporting roof
    cut(anchor, move(tunnel, (40, D / 2 - 16, WALL + 2.0)))
    add(b, anchor)
    return b


def faceplate():
    p = outer_block()
    p.name = "wedge-plate"
    keep(p, slab(0, PLATE_T))
    # Chamfer the sides and the acute front rim; the obtuse back rim needs nothing and a
    # chamfer there would overhang in the face-down print.
    lib.round_edges(p, 1.2, seg=1, angle=60)

    cut(p, on_plate(box((CORE_WIN, CORE_WIN, 20), (0, 0, 0), r=5, seg=6, vertical_only=True), CORE_U, CORE_V))
    cut(p, on_plate(encoder_cutter(PLATE_T), KNOB_U, KNOB_V))
    for u, v in PLATE_POSTS:
        cut(p, on_plate(cyl(M25_CLEAR / 2, 20, (0, 0, 0), n=24), u, v))

    # CoreS3 cradle: frame behind the window (0.4 mm clearance) with a notch on the
    # left for the POWER button and a window on the right for the speaker.
    fd = 8.0
    frame = box((CORE + 0.8 + 4.8, CORE + 0.8 + 4.8, fd), (0, 0, -fd / 2 + 0.3), r=3, seg=4, vertical_only=True)
    cut(frame, box((CORE + 0.8, CORE + 0.8, fd + 2), (0, 0, -fd / 2), r=1, seg=3, vertical_only=True))
    cut(frame, box((6, 22, fd + 2), (-CORE / 2 - 2, 0, -fd / 2), r=0))
    cut(frame, box((6, 36, fd + 2), (CORE / 2 + 2, 0, -fd / 2), r=0))
    # Speaker duct: three walls that, with the CoreS3's right side, channel its
    # speaker out through slots in the plate instead of into the box.
    duct_w = 9.0
    x0 = CORE / 2 + 0.4 + 2.4
    duct = box((duct_w + 2.4, 40 + 4.8, CORE_DEPTH), (x0 + (duct_w + 2.4) / 2, 0, -CORE_DEPTH / 2 + 0.3), r=0)
    cut(duct, box((duct_w, 40, CORE_DEPTH + 2), (x0 + duct_w / 2 - 0.01, 0, -CORE_DEPTH / 2), r=0))
    cut(duct, box((4, 40, CORE_DEPTH + 2), (x0 - 1, 0, -CORE_DEPTH / 2), r=0))
    add(frame, duct)
    add(p, on_plate(frame, CORE_U, CORE_V, PLATE_T))
    for i in range(3):
        cut(p, on_plate(box((2.0, 34, 20), (0, 0, 0), r=0.9, seg=2, vertical_only=True),
                        CORE_U + x0 + 1.8 + i * 3.0, CORE_V))
    # Clamp-bar bosses above and below the CoreS3, CLAMP_SQUEEZE short of its back so
    # tightening the bar presses the CoreS3 against the window lip.
    boss_len = CORE_DEPTH - CLAMP_SQUEEZE + 0.3
    for dv in (-CLAMP_DV, CLAMP_DV):
        bs = cyl(4.0, boss_len, (0, 0, -boss_len / 2 + 0.3), n=32)
        cut(bs, cyl(M25_PILOT / 2, 12, (0, 0, -CORE_DEPTH + 5), n=16))
        add(p, on_plate(bs, CORE_U, CORE_V + dv, PLATE_T))
    # Encoder anti-rotation: two ribs either side of the breakout board.
    for du in (-(ENC_BOARD / 2 + 1.5), ENC_BOARD / 2 + 1.5):
        rib = box((2.4, 16, ENC_BODY + 2.5), (0, 0, -(ENC_BODY + 2.5) / 2 + 0.3), r=0)
        add(p, on_plate(rib, KNOB_U + du, KNOB_V, PLATE_T))
    return p


def clamp():
    """Bar across the CoreS3's back, screwed to the two bosses."""
    c = box((14, 2 * CLAMP_DV + 12, 5.0), (0, 0, -2.5), r=1.5, seg=1, name="wedge-clamp")  # chamfered: prints flat
    for dv in (-CLAMP_DV, CLAMP_DV):
        cut(c, cyl(M25_CLEAR / 2, 10, (0, dv, -2.5), n=24))
    return on_plate(c, CORE_U, CORE_V, PLATE_T + CORE_DEPTH - CLAMP_SQUEEZE)


def coupon(p):
    """Faceplate fit test around the CoreS3: window, cradle, duct and clamp bosses."""
    c = lib.dup(p, "wedge-coupon-cores3")
    return keep(c, on_plate(box((CORE + 34, 2 * CLAMP_DV + 14, 60), (0, 0, 0)), CORE_U + 6, CORE_V))


def build():
    b, p, cl = body(), faceplate(), clamp()
    k = knob(d=KNOB_D, name="wedge-knob")
    on_plate(k, KNOB_U, KNOB_V, -1.5)
    face_down = (180 - SLOPE, "X")
    printed = [(b, None), (p, face_down), (cl, face_down), (k, face_down), (coupon(p), face_down)]
    core = on_plate(box((CORE, CORE, CORE_DEPTH), (0, 0, -CORE_DEPTH / 2), r=4, seg=4, name="ref-cores3"),
                    CORE_U, CORE_V, PLATE_T)
    scr = on_plate(box((40.8, 30.6, 0.6), (0, 0, 0), name="ref-cores3-screen"), CORE_U, CORE_V, PLATE_T - 0.2)
    enc = on_plate(box((ENC_BOARD, ENC_BOARD, 1.6), (0, 0, 0), name="ref-encoder"), KNOB_U, KNOB_V,
                   PANEL_AT_ENC + ENC_BODY + 0.8)
    return printed, [core, scr, enc]


PALETTE = {"wedge-body": (0.95, 0.5, 0.4), "wedge-plate": (0.96, 0.92, 0.84),
           "wedge-clamp": (0.96, 0.92, 0.84), "wedge-knob": (0.25, 0.55, 0.85)}


def paint(printed, ref):
    lib.paint(printed, ref, PALETTE, (0.96, 0.92, 0.84))


def renders(printed, ref, out):
    shown = [o for o in printed if "coupon" not in o.name]
    paint(printed, ref)
    lib.render(os.path.join(out, "wedge-front.jpg"), shown + ref, cam_dir=(0.9, -1.8, 1.2))
    lib.render_section(os.path.join(out, "wedge-section.jpg"), shown + ref, x=CORE_U + 20,
                       cam_dir=(1.0, -0.12, 0.3))
