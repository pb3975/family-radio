"""Shared fit coupon: print this first (about 15 minutes) to tune the knob press fit
and check the encoder's M7 bushing, nut and anti-rotation clearance on a 2 mm wall.
"""
from lib import add, box, cut, cyl
from parts import ENC_BUSHING, PANEL_AT_ENC, SHAFT_D, SHAFT_FLAT


def build():
    plate = box((70, 22, PANEL_AT_ENC), (0, 0, PANEL_AT_ENC / 2), r=0, name="fit-coupon-knob-encoder")
    cut(plate, cyl(ENC_BUSHING / 2 + 0.2, 6, (-26, 0, 1), n=48))   # same hole as the enclosures
    # Three D-bore hubs: diameter -0.15, -0.10 (the knobs' value), -0.05 mm. Notches count 1-3.
    for i, delta in enumerate((-0.15, -0.10, -0.05)):
        x = -6 + i * 16
        hub = cyl(6.5, 9, (x, 0, PANEL_AT_ENC + 4.5 - 0.01), n=64)
        bore = cyl((SHAFT_D + delta) / 2, 8, (x, 0, PANEL_AT_ENC + 9 - 3.99), n=48)
        cut(bore, box((SHAFT_D, SHAFT_D, 10), (x + SHAFT_FLAT, 0, PANEL_AT_ENC + 5)))
        cut(hub, bore)
        for n in range(i + 1):
            cut(hub, box((1.2, 3, 2.4), (x - 2 + 2 * n, -6.5, PANEL_AT_ENC + 9), r=0))
        add(plate, hub)
    return [(plate, None)], []


def renders(printed, ref, out):
    pass
