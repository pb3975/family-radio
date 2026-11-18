"""Slice every STL with OrcaSlicer's Bambu Lab P1P profiles and fail if any part
needs support material. Also reports print time and filament per part.

    python3 cad/slice_check.py --orca ORCA_BIN --profiles ORCA_RESOURCES/profiles/BBL cad/stl/*.stl

Supports are switched ON (auto) for the probe: if the slicer generates any, the
part has an overhang it cannot print cleanly in its exported orientation.
Standard library only.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

MACHINE = "Bambu Lab P1P 0.4 nozzle"
PROCESS = "0.20mm Standard @BBL P1P"
FILAMENT = "Bambu PLA Basic @BBL X1C"
PROBE = {"enable_support": "1", "support_type": "normal(auto)", "wall_loops": "3"}


def flatten(profile_dir, kind, name, overrides=None):
    """Resolve a system profile's `inherits` chain (the CLI does not) into one dict."""
    j = json.load(open(os.path.join(profile_dir, kind, name + ".json")))
    if j.get("inherits"):
        base = flatten(profile_dir, kind, j["inherits"])
        base.update(j)
        j = base
    j.pop("inherits", None)
    j.update(overrides or {})
    return j


def gcode_stats(path):
    """(print time string, filament grams, support % of extrusion) from Orca G-code."""
    time_s, grams, total, support, in_support = "?", 0.0, 0.0, 0.0, False
    with open(path) as f:
        for line in f:
            if line.startswith("; FEATURE:"):
                in_support = "Support" in line
            elif line.startswith("G1") and " E" in line:
                m = re.search(r" E(-?[\d.]+)", line)
                e = float(m.group(1)) if m else 0.0
                if e > 0:
                    total += e
                    support += e if in_support else 0.0
            elif "model printing time:" in line:
                time_s = line.split("model printing time:")[1].split(";")[0].strip()
            elif line.startswith("; filament used [g]"):
                grams = float(line.split("=")[1].split(",")[0])
    return time_s, grams, 100.0 * support / total if total else 0.0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--orca", required=True, help="orca-slicer executable")
    ap.add_argument("--profiles", required=True, help="OrcaSlicer resources/profiles/BBL directory")
    ap.add_argument("--markdown", help="also write a results table here")
    ap.add_argument("files", nargs="+")
    args = ap.parse_args(argv)

    tmp = tempfile.mkdtemp(prefix="slice-")
    cfg = {}
    for kind, name, extra in (("machine", MACHINE, None), ("process", PROCESS, PROBE), ("filament", FILAMENT, None)):
        cfg[kind] = os.path.join(tmp, kind + ".json")
        json.dump(flatten(args.profiles, kind, name, extra), open(cfg[kind], "w"))

    rows, failed = [], 0
    for stl in args.files:
        out = os.path.join(tmp, os.path.basename(stl)[:-4])
        os.makedirs(out)
        subprocess.run([args.orca, "--load-settings", f"{cfg['machine']};{cfg['process']}",
                        "--load-filaments", cfg["filament"], "--slice", "0", "--arrange", "1",
                        "--orient", "0", "--outputdir", out, stl],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
        rpath, gcode = os.path.join(out, "result.json"), os.path.join(out, "plate_1.gcode")
        result = json.load(open(rpath)) if os.path.exists(rpath) else {"error_string": "slicer crashed"}
        if result.get("return_code") != 0 or not os.path.exists(gcode):
            print(f"{stl}: FAIL slicer error: {result.get('error_string')}")
            failed += 1
            continue
        t, g, sup = gcode_stats(gcode)
        ok = sup == 0.0          # any support extrusion at all fails
        failed += not ok
        rows.append((os.path.basename(stl), t, g, sup))
        print(f"{stl}: {t}, {g:.0f} g, support {sup:.4f}%{'' if ok else '  FAIL needs support'}")

    if args.markdown:
        with open(args.markdown, "w") as f:
            f.write("| Part | Print time | PLA | Support |\n| --- | --- | --- | --- |\n")
            for name, t, g, sup in rows:
                f.write(f"| `{name}` | {t} | {g:.0f} g | {sup:.2f}% |\n")
    print(f"{len(args.files) - failed}/{len(args.files)} parts slice without support "
          f"({MACHINE}, {PROCESS}, {FILAMENT})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
