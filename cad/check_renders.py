"""Check local render filenames and bytes without rerunning Blender."""
import ast
import hashlib
from pathlib import Path
import sys

CONCEPT_RENDERS = [f"{name}-{view}.jpg" for name in
                   ("pebble", "retro", "wedge", "twin", "deck", "frame")
                   for view in ("front", "section")] + ["lineup.jpg", "lineup-boombox.jpg",
                                                   "v1-front.jpg", "v1-section.jpg", "v1-back-inside.jpg"]


def write_outputs(directory, filenames):
    directory = Path(directory)
    lines = [f"{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  {name}\n"
             for name in sorted(filenames)]
    (directory / "outputs.sha256").write_text("".join(lines))


def check_outputs(directory, filenames):
    directory = Path(directory)
    expected = set(filenames)
    actual = {p.name for p in directory.glob("*.jpg")}
    if actual != expected:
        raise ValueError(f"{directory}: missing {sorted(expected - actual)}, extra {sorted(actual - expected)}")
    records = [line.split("  ", 1) for line in (directory / "outputs.sha256").read_text().splitlines()]
    if len(records) != len(expected) or {name for _, name in records} != expected:
        raise ValueError(f"{directory}: output manifest filenames differ")
    for digest, name in records:
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"{directory / name}: output hash differs")


if __name__ == "__main__":
    root = Path(__file__).parent
    if "--colorways" in sys.argv:
        # Read palette names without importing Blender or duplicating the palette list.
        tree = ast.parse((root / "colorways.py").read_text())
        palettes = next(ast.literal_eval(node.value) for node in tree.body
                        if isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "PALETTES" for t in node.targets))
        check_outputs(root / "renders" / "colorways", [f"{name}.jpg" for name in palettes])
    else:
        check_outputs(root / "renders", CONCEPT_RENDERS)
    print("render outputs verified")
