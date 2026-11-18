"""Colourway renders of the boombox round (Twin, Deck, Frame). Geometry is unchanged;
only the colours differ. Each palette gives five roles as sRGB hex, the values to
match against filament swatches:

    body       main shell (and the Deck's spine)
    secondary  the Frame's cheeks and bar
    accent     the one bright part: Twin handle, Deck and Frame knobs
    service    back panels and screen clamps
    ink        render-only labels

    blender-5.2 -b --python-exit-code 1 --python cad/colorways.py

Writes local JPEGs and SHA-256 manifests in cad/renders/colorways/.
"""
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import deck  # noqa: E402
import frame  # noqa: E402
import lib  # noqa: E402
import twin  # noqa: E402
from check_renders import write_outputs

PALETTES = {
    "snow":     {"body": "ECEBE6", "secondary": "BFC0BD", "accent": "FF5A1F", "service": "8C8E90", "ink": "1A1A1A"},
    "graphite": {"body": "3A3C3F", "secondary": "1C1D1F", "accent": "FF5A1F", "service": "6E7174", "ink": "E8E4D8"},
    "cobalt":   {"body": "D9D9D6", "secondary": "2B2D30", "accent": "1F5BFF", "service": "8C8E90", "ink": "1A1A1A"},
    "signal":   {"body": "ECEBE6", "secondary": "2B2D30", "accent": "FFC400", "service": "8C8E90", "ink": "1A1A1A"},
    "sage":     {"body": "9DAA92", "secondary": "3E4A3C", "accent": "F2E9D8", "service": "6F7A66", "ink": "1E241C"},
    "tomato":   {"body": "D8392B", "secondary": "7E1C15", "accent": "F4F1EA", "service": "9A2A20", "ink": "FFF6EA"},
    "midnight": {"body": "1E2A44", "secondary": "10151F", "accent": "5FE3C0", "service": "3B4660", "ink": "DCE3EE"},
    "sand":     {"body": "D9CBB0", "secondary": "5A4636", "accent": "E2631C", "service": "A39478", "ink": "2A2119"},
    "braun":    {"body": "ECEBE6", "secondary": "ECEBE6", "accent": "2B2D30", "service": "8C8E90", "ink": "1A1A1A"},
    "lilac":    {"body": "C9C2DE", "secondary": "4A4560", "accent": "FF5A1F", "service": "8E88A6", "ink": "1F1C2B"},
    "ocean":    {"body": "1F6F78", "secondary": "123F45", "accent": "F2C14E", "service": "3E8A92", "ink": "EAF4F2"},
}
CONCEPTS = (twin, deck, frame)
CODE = ("colorways.py", "lib.py", "parts.py", "twin.py", "deck.py", "frame.py")
OUT = os.path.join(HERE, "renders", "colorways")


def role(name):
    if name.endswith(("-body", "-spine")):
        return "body"
    if name.endswith(("-back", "-clamp")):
        return "service"
    if name.startswith(("frame-cheek", "frame-bar")):
        return "secondary"
    return "accent"            # twin-handle and the knobs


def linear(hex_rgb):
    """sRGB hex to the linear RGB that Blender's material colours expect."""
    c = [int(hex_rgb[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return tuple(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in c)


def main():
    lib.reset()
    printed, shown, x = [], [], -270.0
    for concept in CONCEPTS:
        parts, ref = concept.build()
        parts = [o for o, _ in parts]
        concept.paint(parts, ref)              # reference electronics and screen art keep their colours
        for o in parts + ref:
            lib.move(o, (x, 0, 0))
        printed += parts
        shown += parts + ref
        x += 270.0
    labels = [o for o in shown if o.name.startswith("label")]
    os.makedirs(OUT, exist_ok=True)
    for name, pal in PALETTES.items():
        for o in printed:
            lib.material(o, linear(pal[role(o.name)]))
        for o in labels:
            lib.material(o, linear(pal["ink"]))
        lib.render(os.path.join(OUT, f"{name}.jpg"), shown, cam_dir=(0.25, -1.6, 0.55), lens=50, res=(1800, 900))
    write_outputs(OUT, [f"{name}.jpg" for name in PALETTES])
    write_stamp()


def write_stamp():
    """The STLs and scripts these renders came from (sha256sum format, relative to cad/)."""
    files = [f"stl/{f}" for f in sorted(os.listdir(os.path.join(HERE, "stl")))
             if f.startswith(("twin-", "deck-", "frame-"))] + list(CODE)
    with open(os.path.join(OUT, "inputs.sha256"), "w") as f:
        for p in files:
            f.write(f"{hashlib.sha256(open(os.path.join(HERE, p), 'rb').read()).hexdigest()}  {p}\n")


main()
