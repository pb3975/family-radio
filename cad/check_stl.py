"""Check that printable STLs are closed solids that fit the printer.

Usage: python3 cad/check_stl.py [--bed X Y Z] FILE.stl...
Standard library only, so CI needs nothing but Python.
"""
import argparse
import struct
import sys
from collections import Counter

# Assumed printer: Bambu P1P, 256 mm cube. Keep 10 mm for brim/purge clearance.
DEFAULT_BED = (246.0, 246.0, 246.0)


def read_stl(path):
    """Return a list of triangles, each a tuple of three (x, y, z) vertices."""
    data = open(path, "rb").read()
    if data[:5] == b"solid" and b"facet" in data[:300]:
        verts = [tuple(float(v) for v in line.split()[1:4])
                 for line in data.decode().splitlines() if line.strip().startswith("vertex")]
    else:
        (count,) = struct.unpack_from("<I", data, 80)
        if len(data) != 84 + 50 * count:
            raise ValueError(f"truncated binary STL ({len(data)} bytes, {count} triangles)")
        verts = []
        for i in range(count):
            f = struct.unpack_from("<12f", data, 84 + 50 * i)
            verts += [f[3:6], f[6:9], f[9:12]]
    return [tuple(verts[i:i + 3]) for i in range(0, len(verts), 3)]


def check(tris, bed):
    """Return (problems, stats) for one mesh."""
    problems = []
    if not tris:
        return ["empty mesh"], {}
    # Closed and consistently oriented: every directed edge appears once and its reverse once.
    directed = Counter()
    for a, b, c in tris:
        for e in ((a, b), (b, c), (c, a)):
            directed[e] += 1
    bad = sum(1 for (a, b), n in directed.items() if n != 1 or directed.get((b, a)) != 1)
    if bad:
        problems.append(f"not watertight/manifold: {bad} bad edges")
    # Signed volume via divergence theorem; positive means outward-facing normals.
    vol = sum(a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
              + a[2] * (b[0] * c[1] - b[1] * c[0]) for a, b, c in tris) / 6.0
    if vol <= 0:
        problems.append(f"non-positive volume {vol:.1f} mm^3 (inverted normals?)")
    pts = [p for t in tris for p in t]
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    size = [h - l for h, l in zip(hi, lo)]
    if any(s > b for s, b in zip(size, bed)):
        problems.append(f"size {size[0]:.1f} x {size[1]:.1f} x {size[2]:.1f} exceeds bed {bed}")
    if abs(lo[2]) > 0.01:
        problems.append(f"not on the bed: min z = {lo[2]:.3f} (export in print orientation)")
    if max(size) < 2:
        problems.append("suspiciously small; STL units must be millimetres")
    return problems, {"tris": len(tris), "size": size, "vol_cm3": vol / 1000}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bed", nargs=3, type=float, default=DEFAULT_BED)
    ap.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    failed = 0
    for path in args.files:
        try:
            problems, s = check(read_stl(path), args.bed)
        except (OSError, ValueError, struct.error) as e:
            problems, s = [str(e)], {}
        if s:
            print(f"{path}: {s['tris']} tris, {s['size'][0]:.1f} x {s['size'][1]:.1f} x "
                  f"{s['size'][2]:.1f} mm, {s['vol_cm3']:.1f} cm^3")
        for p in problems:
            print(f"  FAIL {p}")
        failed += bool(problems)
    print(f"{len(args.files) - failed}/{len(args.files)} STLs passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
