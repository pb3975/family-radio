"""Self-tests for check_stl.py and slice_check.py on synthetic data. Run: python3 cad/test_checks.py"""
import os
import tempfile

from check_stl import check
from slice_check import gcode_stats

# check_stl: a 10 mm cube passes; open, inverted, oversize and floating meshes fail.
V = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0), (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
FACES = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
cube = [tuple(V[i] for i in f) for f in FACES]
bed = (246, 246, 246)

problems, stats = check(cube, bed)
assert problems == [], problems
assert abs(stats["vol_cm3"] - 1.0) < 1e-9, stats
assert any("watertight" in p for p in check(cube[:-1], bed)[0]), "open mesh must fail"
assert any("volume" in p for p in check([t[::-1] for t in cube], bed)[0]), "inverted mesh must fail"
assert any("exceeds" in p for p in check(cube, (5, 5, 5))[0]), "oversize must fail"
lifted = [tuple((x, y, z + 3) for x, y, z in t) for t in cube]
assert any("bed" in p for p in check(lifted, bed)[0]), "floating part must fail"

# slice_check: support share counts only positive extrusion inside Support features.
GCODE = """; model printing time: 1h 2m 3s; total estimated time: 1h 9m 0s
; FEATURE: Outer wall
G1 X1 Y1 E3.0
G1 E-0.8
; FEATURE: Support
G1 X2 Y2 E1.0
; FEATURE: Sparse infill
G1 X3 Y3 E6.0
; filament used [g] = 12.34
"""
with tempfile.NamedTemporaryFile("w", suffix=".gcode", delete=False) as f:
    f.write(GCODE)
t, g, sup = gcode_stats(f.name)
os.unlink(f.name)
assert t == "1h 2m 3s", t
assert g == 12.34, g
assert abs(sup - 10.0) < 1e-9, sup
print("cad self-tests passed")

# Render outputs: filename sets and bytes must match the committed manifest.
from pathlib import Path
from check_renders import check_outputs, write_outputs

with tempfile.TemporaryDirectory() as tmp:
    directory = Path(tmp)
    image = directory / "front.jpg"
    image.write_bytes(b"synthetic render bytes")
    write_outputs(directory, ["front.jpg"])
    check_outputs(directory, ["front.jpg"])
    manifest = (directory / "outputs.sha256").read_text()
    for failure in ("missing", "changed", "extra", "omitted", "duplicate"):
        image.write_bytes(b"synthetic render bytes")
        (directory / "outputs.sha256").write_text(manifest)
        if failure == "missing":
            image.unlink()
        elif failure == "changed":
            image.write_bytes(b"corrupted render")
        elif failure == "extra":
            (directory / "extra.jpg").write_bytes(b"extra render")
        elif failure == "omitted":
            (directory / "outputs.sha256").write_text("")
        else:
            (directory / "outputs.sha256").write_text(manifest * 2)
        try:
            check_outputs(directory, ["front.jpg"])
        except ValueError:
            pass
        else:
            raise AssertionError(f"{failure} render must fail")
        (directory / "extra.jpg").unlink(missing_ok=True)
print("render checker self-tests passed")
