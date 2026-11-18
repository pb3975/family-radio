"""Build the Family Radio enclosure concepts and export printable STLs.

    blender-5.2 -b --python-exit-code 1 --python cad/build.py -- [--concept NAME] [--render]

A full --render run (no --concept) also renders the lineup and writes
cad/renders/inputs.sha256: the STLs and scripts the renders were made from.
"""
import argparse
import glob
import hashlib
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lib  # noqa: E402
from check_renders import CONCEPT_RENDERS, write_outputs

CONCEPTS = ("pebble", "retro", "wedge", "twin", "deck", "frame", "v1", "fit")
RENDER_CODE = ("build.py", "lib.py", "parts.py", "pebble.py", "retro.py", "wedge.py", "twin.py", "deck.py", "frame.py",
               "v1.py")
HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--concept", choices=CONCEPTS, action="append")
    ap.add_argument("--render", action="store_true", help="also write JPEG renders")
    args = ap.parse_args(argv)
    for name in args.concept or CONCEPTS:
        lib.reset()
        concept = importlib.import_module(name)
        printed, reference = concept.build()
        for o, rot in printed:
            lib.export_stl(o, os.path.join(HERE, "stl", f"{o.name}.stl"), print_rot=rot)
        if args.render:
            concept.renders([o for o, _ in printed], reference, os.path.join(HERE, "renders"))
        print(f"built {name}: {', '.join(o.name for o, _ in printed)}")
    if args.render and not args.concept:
        lineup(("pebble", "retro", "wedge"), "lineup.jpg")
        lineup(("twin", "deck", "frame"), "lineup-boombox.jpg", gap=270.0)
        write_outputs(os.path.join(HERE, "renders"), CONCEPT_RENDERS)
        write_render_stamp()


def write_render_stamp():
    """Record the STLs and scripts the renders came from (sha256sum format, paths
    relative to cad/). Use this manifest to check a local render's inputs."""
    stls = sorted(os.path.relpath(f, HERE) for f in glob.glob(os.path.join(HERE, "stl", "*.stl")))
    lines = [f"{hashlib.sha256(open(os.path.join(HERE, f), 'rb').read()).hexdigest()}  {f}\n"
             for f in stls + list(RENDER_CODE)]
    with open(os.path.join(HERE, "renders", "inputs.sha256"), "w") as f:
        f.writelines(lines)


def lineup(names, filename, gap=230.0):
    """One scene with three concepts side by side, for comparing size and form."""
    lib.reset()
    shown, x = [], -gap
    for name in names:
        concept = importlib.import_module(name)
        printed, reference = concept.build()
        printed = [o for o, _ in printed if "coupon" not in o.name]
        concept.paint(printed, reference)
        for o in printed + reference:
            lib.move(o, (x, 0, 0))
        shown += printed + reference
        x += gap
    lib.render(os.path.join(HERE, "renders", filename), shown, cam_dir=(0.25, -1.6, 0.55),
               lens=50, res=(1800, 900))


main()
