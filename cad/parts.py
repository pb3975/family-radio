"""Reusable printable features and hardware seats: shells, screw bosses, grilles,
the knob, speaker, Pi back panel, and AMOLED screen mounts."""
import math

import bpy

from lib import _apply, add, box, cut, cyl, frustum, hull, join, keep, move, rotate, round_edges

M25_PILOT = 2.2   # M2.5 screw self-tapping into PLA/PETG (inventory: Adafruit 3299 nylon set)
M25_CLEAR = 2.9   # clearance hole for M2.5
WALL = 2.4        # six 0.4 mm perimeters


def ybar(x, y0, y1, z, r=3.6, hole=M25_PILOT, name="post"):
    """Screw post running along Y (front face to back panel) with a pilot hole at y1."""
    p = cyl(r, y1 - y0, (x, (y0 + y1) / 2, z), axis="Y", name=name)
    if hole:
        depth = min(12.0, y1 - y0 - 1.0)
        cut(p, cyl(hole / 2, depth + 0.02, (x, y1 - depth / 2 + 0.01, z), axis="Y", n=24))
    return p


def ring_grille(cx, cz, radius, y0, y1, d=3.2, pitch=5.2, name="grille"):
    """Concentric rings of round holes on an XZ face (cutter). Ribs stay >= 2 mm."""
    holes = [cyl(d / 2, y1 - y0, (cx, (y0 + y1) / 2, cz), axis="Y", n=12)]
    r = pitch
    while r <= radius - d / 2:
        n = int(2 * math.pi * r / pitch)
        for i in range(n):
            a = 2 * math.pi * i / n + (r / pitch) * 0.3
            holes.append(cyl(d / 2, y1 - y0, (cx + r * math.cos(a), (y0 + y1) / 2, cz + r * math.sin(a)),
                             axis="Y", n=12))
        r += pitch
    first = holes[0]
    add(first, *holes[1:])
    first.name = name
    return first


def arc_holes(cx, cz, radius, deg0, deg1, n, y0, y1, d=3.2):
    """Round holes spaced along an arc in the XZ plane (cutters, already unioned)."""
    holes = []
    for i in range(n):
        a = math.radians(deg0 + (deg1 - deg0) * i / (n - 1))
        holes.append(cyl(d / 2, y1 - y0, (cx + radius * math.cos(a), (y0 + y1) / 2, cz + radius * math.sin(a)),
                         axis="Y", n=12))
    first = holes[0]
    add(first, *holes[1:])
    return first


# Encoder: Adafruit 5880 breakout with a PEC11-type encoder (Bourns PEC11 datasheet).
# The breakout itself is 25.4 mm square with 2.5 mm holes on a 20.32 mm pattern.
ENC_BOARD = 25.4
ENC_BODY = 6.5          # encoder body height above the breakout PCB
ENC_BUSHING = 7.0       # M7 x 0.75 threaded bushing, 5 mm long, with nut
SHAFT_D, SHAFT_FLAT = 6.0, 4.5   # D shaft, 4.5 mm across the flat, flat 7 mm long
SHAFT_LEN = 15.0        # shaft length above the encoder body (mounting surface)
PANEL_AT_ENC = 2.0      # wall thickness under the nut: 5 mm bushing = 2 wall + washer + nut
ENC_POCKET = 14.0       # square pocket for the 12.5 x 13.2 mm encoder body


def encoder_cutter(wall_t):
    """Encoder panel-mount cutter along +Z, for a wall spanning z = -wall_t..0.

    A 7.4 mm bushing hole, plus a pocket on the inside that seats the encoder body
    and thins the wall to PANEL_AT_ENC so washer and nut still fit outside. (An
    outside counterbore would print as an unsupported ceiling on face-down parts.)
    """
    c = cyl(ENC_BUSHING / 2 + 0.2, wall_t + 2, (0, 0, -wall_t / 2), n=48)
    t = wall_t - PANEL_AT_ENC
    if t > 0.05:
        add(c, box((ENC_POCKET, ENC_POCKET, t + 1), (0, 0, -wall_t + (t - 1) / 2), r=0))
    return c


def knob(d=52.0, h=20.0, name="knob", flutes=18, flute_r=2.6, flute_out=0.9, full=False):
    """Chunky fluted knob for small hands, modelled top-up with its bottom at z=0.

    The diameter stays above 31.7 mm (the small-parts test cylinder), so the knob
    itself is not a choking hazard if pulled off. It sits 1.5 mm above the panel;
    the hollow skirt clears the encoder nut. The bore is round where the shaft is
    round and D-shaped over the top 7 mm where the shaft is flatted. A radial
    pilot accepts an optional M3 grub screw against the flat.
    """
    k = cyl(d / 2, h, (0, 0, h / 2), n=128, name=name)
    round_edges(k, 2.0, seg=1)          # 45 deg chamfers: the top prints against the bed
    cutters = []
    for i in range(flutes):
        a = 2 * math.pi * (i + 0.5) / flutes
        cx, cy = math.cos(a) * (d / 2 + flute_out), math.sin(a) * (d / 2 + flute_out)
        if full:     # grooves run the full height, like a machined knurl
            f = cyl(flute_r, h + 2, (cx, cy, h / 2), n=24)
        else:
            f = cyl(flute_r, h - 5, (cx, cy, (h - 5) / 2 - 1), n=24)
            add(f, cyl(flute_r, flute_r, (cx, cy, h - 6 + flute_r / 2 - 0.01), n=24, r2=0.0))   # cone end, no overhang
        cutters.append(f)
    cut(k, *cutters)
    # The V-groove shows the knob position by sight and touch. Its 45 degree
    # walls print cleanly with the top face on the bed.
    vg = box((3.0, d / 2 - 6, 3.0), (0, 0, 0), r=0)
    rotate(vg, 45, "Y")
    cut(k, move(vg, (0, (d / 2 - 6) / 2 + 6, h)))
    skirt_h = 4.0
    cut(k, cyl(d / 2 - 4.5, skirt_h * 2, (0, 0, 0), n=96))     # clears nut and washer
    # The encoder body seats PANEL_AT_ENC below the outer face. The bore ends exactly at
    # the shaft tip, a positive stop that leaves the knob 1.5 mm above the panel, more
    # than the push switch's travel (0.5 +/- 0.2 mm on the PEC11).
    tip = SHAFT_LEN - PANEL_AT_ENC - 1.5
    bore = cyl(SHAFT_D / 2 - 0.05, tip, (0, 0, tip / 2 - 0.01), n=48)
    dcut = box((SHAFT_D, SHAFT_D, 7.5), (SHAFT_FLAT, 0, tip - 3.75))
    flat_part = cyl(SHAFT_D / 2, 7.5, (0, 0, tip - 3.75), n=48)
    keep(dcut, flat_part)              # the sliver beyond the flat
    cut(bore, dcut)
    cut(k, bore)
    cut(k, cyl(1.6, d / 2, (d / 4, 0, tip - 3.0), axis="X", n=4))   # M3 grub pilot, diamond prints clean
    return k


def rr_prism(w, h, y0, y1, r, z0=0.0, front=3.0, back=3.0, bed=None, name="prism"):
    """Rounded-rectangle profile in the XZ plane, extruded along Y from y0 to y1.

    Profile corners have radius r. The front (y0) and back (y1) rims get their own
    fillets; keep a rim fillet smaller than the wall where the shell is open. The rim
    named by `bed` ("front" or "back") is printed against the bed, so it gets a 45 deg
    chamfer instead of a fillet (a fillet there is an unsupported overhang).
    Centre in X is 0 and the profile spans z0..z0+h.
    """
    d = y1 - y0
    p = box((w, h, d), (0, 0, 0), r=r, seg=12, vertical_only=True, name=name)
    for side, radius, zcap in (("front", front, d / 2), ("back", back, -d / 2)):   # box +z becomes -y
        if radius:
            fillet_cap(p, zcap, radius, seg=1 if bed == side else 4)
    rotate(p, 90, "X")             # profile now in XZ, extrusion along Y centred on 0
    return move(p, (0, (y0 + y1) / 2, z0 + h / 2))


def fillet_cap(o, zcap, radius, seg=4):
    """Fillet the rim edges of the flat cap lying in the plane z = zcap."""
    bw = o.data.attributes.get("bevel_weight_edge") or o.data.attributes.new("bevel_weight_edge", "FLOAT", "EDGE")
    for e in o.data.edges:
        a, b = (o.data.vertices[i].co for i in e.vertices)
        bw.data[e.index].value = 1.0 if abs(a.z - zcap) < 1e-4 and abs(b.z - zcap) < 1e-4 else 0.0
    m = o.modifiers.new("cap", "BEVEL")
    m.width, m.segments, m.limit_method = radius, seg, "WEIGHT"
    _apply(o)


# Speaker: Adafruit 1314, 3" 4 ohm 3 W (datasheet): 78 mm square frame, 25 mm deep,
# four 5 x 4 mm slots on an 84 mm circle at the corners (60 mm square pattern).
SPK_FRAME, SPK_DEPTH, SPK_SLOT_R = 78.0, 25.0, 42.0
SPK_STANDOFF = 4.0        # air gap between the grille and the cone

# Raspberry Pi Zero 2 W (mechanical drawing): 65 x 30, holes 3.5 in from each edge
# (58 x 23 pattern); micro-USB power centred 54 mm from the left edge.
PI_W, PI_H, PI_HX, PI_HZ = 65.0, 30.0, 58.0, 23.0

# Adafruit I2S Amp BFF 5770 (PCB files): 17.78 x 20.70, no mounting holes.
AMP_W, AMP_H = 17.78, 20.70

# Waveshare ESP32-S3-Touch-AMOLED-1.8 in its case (Waveshare dimension drawing).
CASE_W, CASE_H, CASE_T = 37.6, 45.2, 15.0
WIN_W, WIN_H = 28.7, 34.94      # visible screen; assumed centred (vertical offset UNVERIFIED)
BTN_DZ = 10.5                   # USB-C centred on the right edge, PWR/BOOT at +/-10.5
SD_Z0, SD_Z1 = -15.7, 0.4       # microSD opening on the left edge
CLAMP_X = CASE_W / 2 + 7.0      # clamp-bar posts either side of the case
CLAMP_SQUEEZE = 0.4             # posts stop short of the case back so the bar presses it


def speaker_seat(b, x, z, fi, seal="round"):
    """Seat a 1314 speaker behind a front wall whose inside face is at y = fi.

    A sealing ring meets the speaker frame so sound goes out of the grille rather
    than into the box, and four screw posts take the frame's corner slots. A
    "square" seal meets the square frame, so a square hole field can sit in front
    of it: corner holes land on the frame flange and none vents the box.
    """
    if seal == "round":
        ring = cyl(37.5, SPK_STANDOFF + 0.5, (x, fi + SPK_STANDOFF / 2 - 0.25, z), axis="Y", n=96)
        cut(ring, cyl(35.5, SPK_STANDOFF + 2, (x, fi + SPK_STANDOFF / 2, z), axis="Y", n=96))
    else:
        ring = box((SPK_FRAME - 1, SPK_STANDOFF + 0.5, SPK_FRAME - 1), (x, fi + SPK_STANDOFF / 2 - 0.25, z), r=0)
        cut(ring, box((SPK_FRAME - 5, SPK_STANDOFF + 2, SPK_FRAME - 5), (x, fi + SPK_STANDOFF / 2, z), r=0))
    add(b, ring)
    for i in range(4):
        a = math.radians(45 + 90 * i)
        add(b, ybar(x + SPK_SLOT_R * math.cos(a), fi - 0.5, fi + SPK_STANDOFF, z + SPK_SLOT_R * math.sin(a), r=3.6))


def square_field(u, v):
    """Mask for a 66 mm square dot field over a square-sealed speaker: the corner holes
    are left out because the speaker's screw posts stand behind them."""
    return not (abs(u) > 25 and abs(v) > 25)


def ref_speaker(x, z, fi, name="ref-speaker"):
    return box((SPK_FRAME, SPK_DEPTH, SPK_FRAME), (x, fi + SPK_STANDOFF + SPK_DEPTH / 2, z), r=3, name=name)


def pi_back_panel(p, y1, posts, pi_xz, amp_xz, vents=6):
    """Finish a back panel (outer face at y = y1, printed face-down) for the Pi audio
    stack: screw holes, Pi Zero 2 W standoffs, an amp pocket, vents above the Pi, and
    a micro-USB exit with a zip-tie anchor so a tug never reaches the Pi's port."""
    pin = y1 - 3.0            # inside face of the 3 mm panel
    for x, z in posts:
        cut(p, cyl(M25_CLEAR / 2, 10, (x, y1, z), axis="Y", n=24))
    px, pz = pi_xz
    # Pi Zero 2 W on four 6 mm standoffs that grow from the panel (printed face-down).
    for sx in (-PI_HX / 2, PI_HX / 2):
        for sz in (-PI_HZ / 2, PI_HZ / 2):
            s = cyl(3.0, 6.5, (px + sx, pin - 3.0, pz + sz), axis="Y", n=32)
            cut(s, cyl(M25_PILOT / 2, 6.0, (px + sx, pin - 3.5, pz + sz), axis="Y", n=16))
            add(p, s)
    # Amp: a shallow pocket; the board is held with double-sided foam tape.
    ax, az = amp_xz
    frame = box((AMP_W + 0.6 + 3.2, 3.5, AMP_H + 0.6 + 3.2), (ax, pin - 1.5, az), r=0)
    cut(frame, box((AMP_W + 0.6, 6, AMP_H + 0.6), (ax, pin - 1.5, az), r=0))
    add(p, frame)
    for i in range(vents):
        cut(p, box((44, 10, 2.4), (px, y1, pz + 24 + i * 5), r=1.0, seg=2, vertical_only=True))
    cx = px - PI_W / 2 + 54.0
    cut(p, box((14, 10, 9), (cx, y1, pz - 20.0), r=0))
    for dx in (-7.0, 7.0):
        cut(p, box((3.5, 10, 2.0), (cx + dx, y1, pz - 8.0), r=0))
    return p


def ref_pi_stack(y1, pi_xz, amp_xz):
    pin = y1 - 3.0
    return [box((PI_W, 1.6, PI_H), (pi_xz[0], pin - 6.8, pi_xz[1]), r=0.5, name="ref-pi"),
            box((AMP_W, 1.6, AMP_H), (amp_xz[0], pin - 1.0, amp_xz[1]), name="ref-amp")]


def _place(o, x, z, landscape):
    """Turn a feature built around the XZ origin a quarter turn (landscape), then place it."""
    if landscape:
        rotate(o, 90, "Y")
    return move(o, (x, 0, z))


def amoled_mount(b, x, z, y0, front_t, landscape=False):
    """Window and pocket for the Waveshare AMOLED 1.8 in its case, on a front wall
    whose outer face is y0. Window 0.5 mm larger than the visible area with a 1.5 mm
    45 deg chamfer; a 6 mm frame behind it with 0.4 mm clearance and side windows
    for USB-C and PWR/BOOT (right) and microSD (left); two clamp posts."""
    cut(b, _place(box((WIN_W + 1, front_t + 2, WIN_H + 1), (0, y0 + front_t / 2, 0), r=0), x, z, landscape))
    cut(b, _place(frustum(WIN_W + 4, WIN_H + 4, WIN_W + 1, WIN_H + 1, y0 - 0.01, y0 + 1.5, 0), x, z, landscape))
    fy = y0 + front_t + 3.0
    frame = box((CASE_W + 0.8 + 4.0, 6.0, CASE_H + 0.8 + 4.0), (0, fy - 0.3, 0), r=0)
    cut(frame, box((CASE_W + 0.8, 8.0, CASE_H + 0.8), (0, fy, 0), r=0))
    cut(frame, box((6, 8.0, 2 * BTN_DZ + 8), (CASE_W / 2 + 1.5, fy, 0), r=0))
    cut(frame, box((6, 8.0, SD_Z1 - SD_Z0 + 4), (-CASE_W / 2 - 1.5, fy, (SD_Z0 + SD_Z1) / 2), r=0))
    add(b, _place(frame, x, z, landscape))
    for cx in (-CLAMP_X, CLAMP_X):
        add(b, _place(ybar(cx, y0 + front_t - 0.5, y0 + front_t + CASE_T - CLAMP_SQUEEZE, 0), x, z, landscape))


def amoled_clamp(name, x, z, y0, front_t, landscape=False):
    """Bar across the back of the AMOLED case, screwed to the two posts."""
    y = y0 + front_t + CASE_T - CLAMP_SQUEEZE + 2.5
    c = box((2 * CLAMP_X + 10, 5.0, 12.0), (0, y, 0), r=1.5, seg=1, name=name)  # chamfered: prints flat
    for cx in (-CLAMP_X, CLAMP_X):
        cut(c, cyl(M25_CLEAR / 2, 10, (cx, y, 0), axis="Y", n=24))
    return _place(c, x, z, landscape)


def ref_amoled(x, z, y0, front_t, landscape=False):
    case = box((CASE_W, CASE_T, CASE_H), (0, y0 + front_t + CASE_T / 2, 0), r=2, name="ref-amoled")
    scr = box((WIN_W, 0.6, WIN_H), (0, y0 + front_t - 0.4, 0), name="ref-amoled-screen")
    return [_place(case, x, z, landscape), _place(scr, x, z, landscape)]


def dot_grid(cx, cz, w, h, y0, y1, pitch=5.0, d=3.0, inside=None, name="grid"):
    """Orthogonal field of round holes (TE-style), w x h centred on (cx, cz), through
    the wall from y0 to y1. Optional inside(x, z) keeps only some holes. Returns one
    cutter: the holes are disjoint, so they are joined rather than unioned (fast)."""
    nx, nz = int(w // pitch) + 1, int(h // pitch) + 1
    holes = []
    for i in range(nx):
        for j in range(nz):
            x, z = cx + (i - (nx - 1) / 2) * pitch, cz + (j - (nz - 1) / 2) * pitch
            if inside is None or inside(x - cx, z - cz):
                holes.append(cyl(d / 2, y1 - y0, (x, (y0 + y1) / 2, z), axis="Y", n=12))
    return join(holes, name)


# M5Stack Dial (M5Stack docs and official STL): knob ring dia 49.83 x 12.57 in front
# of the panel; threaded rear body (about 44 dia, UNVERIFIED) clamps through the
# panel with its 50.82 dia locking ring.
DIAL_KNOB_D = 49.83
DIAL_THREAD_D = 44.0


def ref_dial(x, z, y0):
    """The Dial on a front wall whose outer face is y0."""
    return [cyl(DIAL_KNOB_D / 2, 12.57, (x, y0 - 12.57 / 2 - 0.3, z), axis="Y", n=96, name="ref-dial"),
            cyl(DIAL_THREAD_D / 2, 17.7, (x, y0 + 17.7 / 2, z), axis="Y", n=96, name="ref-dial-rear"),
            cyl(16.3, 0.6, (x, y0 - 12.9, z), axis="Y", n=64, name="ref-dial-screen")]


def eased_slot(w, h, r, x, z0, stations):
    """Rounded-rectangle opening along Y (a handle slot), w x h with corner radius r,
    from z0. `stations` lists (y, grow): the profile is grown by `grow` at that y and
    the cutter is the hull of all stations, so the edges where fingers wrap get a
    chamfer. 45 deg or gentler flares print without support in any orientation where
    the opening runs vertically."""
    slices = [rr_prism(w + 2 * g, h + 2 * g, y - 0.005, y + 0.005, r + g, z0=z0 - g, front=0, back=0)
              for y, g in stations]
    pts = [(v.co.x + x, v.co.y, v.co.z) for o in slices for v in o.data.vertices]
    for o in slices:
        bpy.data.objects.remove(o, do_unlink=True)
    return hull(pts, "slot")
