"""V1 enclosure for the plugged-in radio, adapted from the Twin concept.

The enclosure accommodates these parts:

- M5Stack Dial through the front wall: centre knob and round screen.
- Two 30 x 70 mm, 4 ohm, 3 W enclosed speakers (label HIBOX3070-K), standing
  upright behind pill-shaped dot grilles. Screw posts and a locating rim hold each one.
- Raspberry Pi Zero 2 W and Adafruit 6513 stereo amp (dual MAX98357A) on screw
  standoffs that grow from the back panel.
- Power enters the Pi's micro-B PWR IN. Its neighbour, the USB (OTG) port, holds
  the Dial's right-angle adapter. Both ports face down, with clear space below
  them. The power cable stays plugged in inside. A zip tie holds it to a saddle,
  so a tug pulls on the panel and not on the Pi's port. The cable then leaves
  through a hole in the back panel.

The enclosure has no battery or external USB-C inlet.
Dimensions tagged UNVERIFIED must be checked against the actual parts before printing.
"""
import os

import bpy
import bmesh

import lib
from lib import add, box, cut, cyl, keep, label, move, rotate, screen_art
from parts import DIAL_THREAD_D, M25_CLEAR, M25_PILOT, WALL, dot_grid, eased_slot, ref_dial, rr_prism, ybar

W, H, D = 210.0, 124.0, 72.0
R = 8.0                   # profile corner radius, as Twin
FRONT_T = 3.0             # front wall; also the Dial's clamping panel
PANEL_T = 3.0
Y0, Y1 = -D / 2, D / 2
FI = Y0 + FRONT_T         # inside face of the front wall
PIN = Y1 - PANEL_T        # inside face of the back panel
_C = WALL + 4.1           # corner posts merge into the corner wall
BACK_POSTS = [(sx * (W / 2 - _C), z) for sx in (-1, 1) for z in (_C, H - _C)]
DIAL_X, DIAL_Z = 0.0, H / 2

# Speaker, 30 x 70 x 17 mm enclosed box. Flange holes 3.4 mm on a 24 x 63 mm
# rectangle per the Adafruit 3351 listing for this speaker size. The enclosure
# targets HIBOX3070-K speakers. The outline and hole pattern are UNVERIFIED.
SPK_W, SPK_H, SPK_DEPTH = 30.0, 70.0, 17.0
SPK_HX, SPK_HZ = 24.0, 63.0
SPK_FLANGE = 2.0          # UNVERIFIED flange thickness, for the reference model only
SPK_SO = 5.0              # flange stands 5 mm behind the wall: cone gap and 4.5 mm screw pilot
SPK_RIM, SPK_FIT = 1.6, 0.4
SPK_XS, SPK_Z = (-62.0, 62.0), H / 2

# Raspberry Pi Zero 2 W (official mechanical drawing RP-008358-DS): 65 x 30 mm,
# holes 3.5 mm in from each edge (58 x 23 mm pattern). Ports along one long edge,
# measured from the SD-card end: USB (OTG) 41.4 mm, PWR IN 54 mm.
PI_W, PI_H, PI_HX, PI_HZ = 65.0, 30.0, 58.0, 23.0
PI_PCB = 1.0              # UNVERIFIED board thickness, reference model only
PI_SO = 8.0               # standoff height: room for solder joints and the adapter's body
PI_X, PI_Z = 0.0, 73.0    # mounted ports-down, SD card on the left seen from the front
PORT_Z = PI_Z - PI_H / 2  # the port edge, 55.6 mm above the inside floor
OTG_X, PWR_X = PI_X - PI_W / 2 + 41.4, PI_X - PI_W / 2 + 54.0
PORT_Y = PIN - PI_SO - PI_PCB - 1.4   # micro-B receptacle centre, about 1.4 mm above the board

# Adafruit 6513 stereo amp (Adafruit PCB file "Stereo rev B"): 25.4 x 19.05 mm,
# four 2.5 mm plated holes 2.54 mm in from each edge (20.32 x 13.97 mm pattern).
AMP_W, AMP_H, AMP_HX, AMP_HZ = 25.4, 19.05, 20.32, 13.97
AMP_SO = 5.0
AMP_X, AMP_Z = -62.0, PI_Z

# Plug dimensions are estimates based on the Pi's known 65 mm length (about
# +/-2 mm). UNVERIFIED: check with the printed back panel.
ADAPTER = (16.5, 12.0, 22.0)  # right-angle micro-B adapter on OTG: width, thickness, reach below the port edge
ADAPTER_DX = -1.4             # its flange sits off-centre, away from the power plug
PLUG = (11.0, 7.5, 27.0)      # power plug overmold and boot: width, thickness, reach

# Power cable path: plug runs down, a zip tie holds the cable on a saddle, then
# it bends back out through a notch in the panel's bottom edge. The cable slips
# into the notch before the panel goes on, so the plug never has to pass it.
SADDLE_Z = 25.0
EXIT_W, EXIT_TOP = 7.0, 10.0  # notch for a cable up to about 6 mm across
ZIP_SLOT = (2.2, 4.0)         # through-slot for a zip tie up to 3.6 mm wide
COIL_ANCHORS = [(-30.0, 30.0), (-85.0, 30.0)]   # tie points for the Dial cable's slack

HANDLE_SPAN = 140.0       # centre-to-centre of the handle feet
HANDLE_RISE, HANDLE_BAR, HANDLE_T = 44.0, 14.0, 18.0


def _field(u, v):
    """Grille field: 5 x 12 holes over the speaker. The four corner holes are left
    out because the flange's screw posts stand right behind them."""
    return not (abs(u) > 7.5 and abs(v) > 25.0)


def speaker_seat(b, x, z):
    """Four screw posts the flange sits on, and a rim that locates the flange and
    closes the gap between flange and wall, so sound leaves through the grille."""
    for sx in (-1, 1):
        for sz in (-1, 1):
            add(b, ybar(x + sx * SPK_HX / 2, FI - 0.5, FI + SPK_SO, z + sz * SPK_HZ / 2, r=2.9))
    y1 = FI + SPK_SO + 2.0
    iw, ih = SPK_W + 2 * SPK_FIT, SPK_H + 2 * SPK_FIT
    rim = box((iw + 2 * SPK_RIM, y1 - FI + 0.5, ih + 2 * SPK_RIM), (x, (FI - 0.5 + y1) / 2, z), r=0)
    cut(rim, box((iw, 40, ih), (x, FI, z), r=0))
    for sz in (-1, 1):   # notches at both short ends for the leads (exit side UNVERIFIED)
        cut(rim, box((8.0, 4.0, 10.0), (x, FI + SPK_SO + 2.0, z + sz * (ih / 2 + 1)), r=0))
    add(b, rim)


def body():
    b = rr_prism(W, H, Y0, Y1, R, front=1.2, back=1.0, bed="front", name="v1-body")
    cut(b, rr_prism(W - 2 * WALL, H - 2 * WALL, FI, Y1 + 1, R - WALL, z0=WALL, front=0, back=0))
    for x in SPK_XS:
        cut(b, dot_grid(x, SPK_Z, 20, 55, Y0 - 1, FI + 1, pitch=5.0, d=3.0, inside=_field))
        speaker_seat(b, x, SPK_Z)
    cut(b, cyl(DIAL_THREAD_D / 2 + 0.3, FRONT_T + 4, (DIAL_X, Y0 + FRONT_T / 2, DIAL_Z), axis="Y", n=128))
    for x, z in BACK_POSTS:
        add(b, ybar(x, FI - 0.5, PIN, z, r=4.5))
    for x in (-HANDLE_SPAN / 2, HANDLE_SPAN / 2):
        cut(b, cyl(2.3, 12, (x, 0, H - WALL / 2), n=4))   # diamond, 3.25 mm across flats: M3 clearance
    return b


def standoffs(p, cx, cz, hx, hz, h, r):
    """Four M2.5 screw standoffs growing from the panel's inside face (up in the print)."""
    for sx in (-1, 1):
        for sz in (-1, 1):
            x, z = cx + sx * hx / 2, cz + sz * hz / 2
            s = cyl(r, h + 0.5, (x, PIN - h / 2 + 0.25, z), axis="Y", n=32)
            cut(s, cyl(M25_PILOT / 2, h, (x, PIN - h / 2 - 0.01, z), axis="Y", n=16))
            add(p, s)


def zip_slots(p, x, z, half=6.5):
    for dx in (-half, half):
        cut(p, box((ZIP_SLOT[0], 10, ZIP_SLOT[1]), (x + dx, Y1, z), r=0))


def back_panel():
    p = rr_prism(W - 2 * WALL - 0.6, H - 2 * WALL - 0.6, PIN, Y1, R - WALL - 0.3,
                 z0=WALL + 0.3, front=0, back=0.8, bed="back", name="v1-back")
    for x, z in BACK_POSTS:
        cut(p, cyl(M25_CLEAR / 2, 10, (x, Y1, z), axis="Y", n=24))
    standoffs(p, PI_X, PI_Z, PI_HX, PI_HZ, PI_SO, r=3.0)
    standoffs(p, AMP_X, AMP_Z, AMP_HX, AMP_HZ, AMP_SO, r=2.5)
    for i in range(5):   # vents above the Pi, where its heat rises
        cut(p, box((44, 10, 2.4), (PI_X, Y1, PI_Z + PI_H / 2 + 7 + i * 5), r=1.0, seg=2, vertical_only=True))
    # Saddle: the cable rests on it 2 mm behind its centreline and a zip tie
    # through the panel holds it there.
    top = PORT_Y + 2.0
    add(p, box((8.0, PIN - top + 0.5, 6.0), (PWR_X, (PIN + top) / 2 + 0.25, SADDLE_Z), r=0))
    zip_slots(p, PWR_X, SADDLE_Z)
    notch = box((EXIT_W, 2 * EXIT_TOP, 10.0), (0, 0, 0), r=EXIT_W / 2 - 0.01, seg=8, vertical_only=True)
    rotate(notch, 90, "X")   # rounded top runs through the panel; the bottom stays open
    cut(p, move(notch, (PWR_X, Y1, 0.0)))
    for x, z in COIL_ANCHORS:
        zip_slots(p, x, z)
    return p


def handle():
    """Flat bar handle, as Twin, printed on its side so layers run along the bar.
    Screws come up through the top wall into the feet (adult-only, from inside)."""
    w = HANDLE_SPAN + 18.0
    h = rr_prism(w, 2 * HANDLE_RISE, -HANDLE_T / 2, HANDLE_T / 2, 9.0, z0=H - HANDLE_RISE, front=2.0, back=3.0,
                 bed="front", name="v1-handle")
    t = HANDLE_T / 2
    cut(h, eased_slot(w - 36.0, 2 * (HANDLE_RISE - HANDLE_BAR), 4.0, 0, H - HANDLE_RISE + HANDLE_BAR,
                      [(-t - 0.01, 1.5), (-t + 1.5, 0), (t - 1.5, 0), (t + 0.01, 1.5)]))
    cut(h, box((w + 10, HANDLE_T + 10, HANDLE_RISE + 1), (0, 0, H - (HANDLE_RISE + 1) / 2), r=0))
    for x in (-HANDLE_SPAN / 2, HANDLE_SPAN / 2):
        cut(h, cyl(1.5, 14, (x, 0, H + 6.0), n=4))   # diamond pilot: horizontal in the print
    return h


def coupon(b):
    """Front-wall fit test, about an hour: Dial thread clearance and ring clamp,
    one speaker's posts, rim and hole pattern, and the grille."""
    c = lib.dup(b, "v1-coupon-front")
    return keep(c, box((112, 22, 84), (25, Y0 + 11, DIAL_Z), r=0))


def keepouts():
    """Space the electronics and plugs need, as (name, solid). The build fails if
    any printed part enters one, so a later edit cannot block a plug or a board."""
    pcb = PIN - PI_SO
    a_w, a_t, a_r = ADAPTER
    p_w, p_t, p_r = PLUG
    return [
        ("pi", box((PI_W + 1, 12.0, PI_H + 1), (PI_X, pcb - 6.0 - 0.05, PI_Z), r=0)),
        ("adapter", box((a_w, a_t, a_r), (OTG_X + ADAPTER_DX, PORT_Y, PORT_Z - a_r / 2), r=0)),
        ("power-plug", box((p_w, p_t, p_r), (PWR_X, PORT_Y, PORT_Z - p_r / 2), r=0)),
        ("amp", box((AMP_W + 1, 11.0, AMP_H + 1), (AMP_X, PIN - AMP_SO - 5.5 - 0.05, AMP_Z), r=0)),
        ("dial-rear", cyl(DIAL_THREAD_D / 2, 17.7, (DIAL_X, Y0 + 17.7 / 2, DIAL_Z), axis="Y", n=96)),
        *[(f"speaker-{i}", box((SPK_W, SPK_DEPTH, SPK_H), (x, FI + SPK_SO + SPK_DEPTH / 2 + 0.05, SPK_Z), r=0))
          for i, x in enumerate(SPK_XS)],
    ]


def _volume(o):
    bm = bmesh.new()
    bm.from_mesh(o.data)
    v = abs(bm.calc_volume())
    bm.free()
    return v


def check_plugs():
    """The two plugs share one board edge. Their envelopes must not overlap, or
    the model claims a fit that the real plugs cannot make."""
    gap = (PWR_X - PLUG[0] / 2) - (OTG_X + ADAPTER_DX + ADAPTER[0] / 2)
    if gap < 0:
        raise AssertionError(f"adapter and power-plug envelopes overlap by {-gap:.2f} mm")


def check_keepouts(parts):
    """Raise if any printed part overlaps a keep-out by more than 1 mm^3."""
    check_plugs()
    bad = []
    for name, k in keepouts():
        for o in parts:
            probe = lib.dup(o, "probe")
            keep(probe, lib.dup(k, "probe-k"))
            v = _volume(probe)
            bpy.data.objects.remove(probe, do_unlink=True)
            if v > 1.0:
                bad.append(f"{o.name} enters {name} keep-out by {v:.1f} mm^3")
        bpy.data.objects.remove(k, do_unlink=True)
    if bad:
        raise AssertionError("; ".join(bad))


def ref_parts():
    pcb = PIN - PI_SO
    a_w, a_t, a_r = ADAPTER
    p_w, p_t, p_r = PLUG
    spk = []
    for x in SPK_XS:
        spk += [box((SPK_W, SPK_FLANGE, SPK_H), (x, FI + SPK_SO + SPK_FLANGE / 2, SPK_Z), r=0.5, name="ref-speaker"),
                box((SPK_W - 4, SPK_DEPTH - SPK_FLANGE, SPK_H - 10),
                    (x, FI + SPK_SO + SPK_FLANGE + (SPK_DEPTH - SPK_FLANGE) / 2, SPK_Z), r=2, name="ref-speaker")]
    return [*ref_dial(DIAL_X, DIAL_Z, Y0), *spk,
            box((PI_W, PI_PCB, PI_H), (PI_X, pcb - PI_PCB / 2, PI_Z), r=0.5, name="ref-pi"),
            box((14, 8, 14), (PI_X - 3, pcb - PI_PCB - 4, PI_Z), r=0.5, name="ref-pi-heatsink"),
            box((a_w, a_t, a_r), (OTG_X + ADAPTER_DX, PORT_Y, PORT_Z - a_r / 2), r=1.0, name="ref-adapter"),
            box((p_w, p_t, p_r), (PWR_X, PORT_Y, PORT_Z - p_r / 2), r=1.5, name="ref-plug"),
            box((AMP_W, 1.6, AMP_H), (AMP_X, PIN - AMP_SO - 0.8, AMP_Z), r=0.5, name="ref-amp"),
            box((14, 8, 7), (AMP_X + 3, PIN - AMP_SO - 5.6, AMP_Z + 4), r=0.5, name="ref-amp-terminals")]


def build():
    b, p, hd = body(), back_panel(), handle()
    check_keepouts([b, p])
    printed = [(b, (90, "X")), (p, (-90, "X")), (hd, (90, "X")), (coupon(b), (90, "X"))]
    return printed, ref_parts()


PALETTE = {"v1-body": (0.80, 0.80, 0.78), "v1-back": (0.42, 0.43, 0.44), "v1-handle": (0.98, 0.33, 0.04)}


def paint(printed, ref):
    lib.paint(printed, ref, PALETTE, (0.80, 0.80, 0.78))
    ink = (0.08, 0.08, 0.08)
    ref += [label("FAMILY RADIO", (-W / 2 + 12, Y0 - 0.05, H - 11), size=3.6, align="LEFT", rgb=ink),
            label("V1", (W / 2 - 12, Y0 - 0.05, 11), size=2.8, align="RIGHT", rgb=ink),
            label("VOL  •  PLAY", (DIAL_X, Y0 - 0.05, DIAL_Z - 33), size=2.6, rgb=ink),
            *screen_art((DIAL_X, Y0 - 13.25, DIAL_Z), 24, 24)]


def renders(printed, ref, out):
    shown = [o for o in printed if "coupon" not in o.name]
    paint(printed, ref)
    lib.render(os.path.join(out, "v1-front.jpg"), shown + ref, cam_dir=(0.55, -1.8, 0.55), lens=70)
    lib.render_section(os.path.join(out, "v1-section.jpg"), lib.solid(shown + ref), x=PWR_X,
                       cam_dir=(1.0, -0.45, 0.35))
    # Inside of the back panel with the electronics in place, seen from the front.
    inside = [o for o in shown if o.name == "v1-back"] + [o for o in ref if o.name.startswith(
        ("ref-pi", "ref-adapter", "ref-plug", "ref-amp"))]
    lib.render(os.path.join(out, "v1-back-inside.jpg"), inside, cam_dir=(0.15, -1.0, 0.1), lens=60)
