#!/usr/bin/env python3
"""Independently decode synthetic native Dial renders and check QR visibility.

Install test dependencies with:
  python -m pip install --only-binary=:all: zxing-cpp==3.1.1 Pillow==13.0.0
The manifest is {"cases": [{"name": "...", "image": "relative.png",
"payload": "expected UTF-8 payload"}]}. Never use real credentials here.
"""

import argparse
import json
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageOps
import zxingcpp


SIZE = 240
SQUARE = (38, 38, 202, 202)
MODULE = 4
MODULES = 33  # Explicit version 4, matching the firmware layout.
QUIET = 4 * MODULE
CODE = (SQUARE[0] + QUIET, SQUARE[1] + QUIET,
        SQUARE[2] - QUIET, SQUARE[3] - QUIET)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def circular_display(image):
    """Model the round panel with pixel centers and black outside the panel."""
    require(image.size == (SIZE, SIZE), "render must be 240x240")
    image = image.convert("RGB").copy()
    for y in range(SIZE):
        for x in range(SIZE):
            if (x + 0.5 - 120) ** 2 + (y + 0.5 - 120) ** 2 > 120 ** 2:
                image.putpixel((x, y), (0, 0, 0))
    return image


def verify(image, payload):
    image = circular_display(image)
    # Decode the actual masked screen before checking layout. No expected QR
    # matrix or firmware encoder participates in this independent decode.
    codes = zxingcpp.read_barcodes(
        image, formats=zxingcpp.BarcodeFormat.QRCode, try_invert=False,
        try_rotate=False, try_downscale=False,
    )
    require(len(codes) == 1, "expected one decodable QR on the circular display")
    code = codes[0]
    require(code.bytes == payload.encode("utf-8"), "decoded payload bytes differ")

    corners = (code.position.top_left, code.position.top_right,
               code.position.bottom_right, code.position.bottom_left)
    for corner, expected in zip(corners, ((54, 54), (186, 54), (186, 186), (54, 186))):
        require(abs(corner.x - expected[0]) <= 1 and abs(corner.y - expected[1]) <= 1,
                "decoded QR bounds differ from the version-4 layout")

    left, top, right, bottom = SQUARE
    for y in range(top, bottom):
        for x in range(left, right):
            require((x + 0.5 - 120) ** 2 + (y + 0.5 - 120) ** 2 <= 120 ** 2,
                    "QR or quiet zone is clipped by the circular display")
            if not (CODE[0] <= x < CODE[2] and CODE[1] <= y < CODE[3]):
                require(image.getpixel((x, y)) == (255, 255, 255),
                        "QR needs an unobstructed white quiet zone of four modules")

    modules = []
    for row in range(MODULES):
        values = []
        for column in range(MODULES):
            x, y = CODE[0] + column * MODULE, CODE[1] + row * MODULE
            colors = set(image.crop((x, y, x + MODULE, y + MODULE)).get_flattened_data())
            require(colors in ({(0, 0, 0)}, {(255, 255, 255)}),
                    "QR modules must be solid black or white at four pixels per module")
            values.append(colors == {(0, 0, 0)})
        modules.append(values)

    # Finder rings establish the required dark-on-light polarity and catch
    # clipped modules even when decoder error correction repairs the payload.
    for ox, oy in ((0, 0), (MODULES - 7, 0), (0, MODULES - 7)):
        for y in range(7):
            for x in range(7):
                dark = x in (0, 6) or y in (0, 6) or (2 <= x <= 4 and 2 <= y <= 4)
                require(modules[oy + y][ox + x] == dark,
                        "QR finder is clipped or has incorrect dark-on-light polarity")
    return image


def self_test(image, payload):
    """Mutate a real rendered QR, so the rejection tests exercise the decoder."""
    verify(image, payload)
    quiet = image.copy()
    quiet.putpixel((CODE[0] - 1, CODE[1]), (0, 0, 0))
    clipped = image.copy()
    ImageDraw.Draw(clipped).rectangle((CODE[0], CODE[1], CODE[2] - 1, CODE[1] + 3),
                                      fill="white")
    mutations = (
        (image, payload + "wrong", "incorrect expected payload"),
        (quiet, payload, "damaged quiet zone"),
        (clipped, payload, "clipped finder row"),
        (ImageOps.invert(image.convert("RGB")), payload, "inverted QR"),
    )
    for changed, expected, label in mutations:
        try:
            verify(changed, expected)
        except ValueError:
            continue
        raise ValueError(f"self-test accepted {label}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    require(isinstance(manifest, dict), "manifest must be an object")
    cases = manifest.get("cases")
    require(isinstance(cases, list) and cases, "manifest needs nonempty cases")
    for case in cases:
        require(isinstance(case, dict) and all(isinstance(case.get(key), str)
                    and case[key] for key in ("name", "image", "payload")),
                "each case needs nonempty name, image and payload strings")
        try:
            with Image.open(args.manifest.parent / case["image"]) as source:
                require(source.format == "PNG", "render must be a PNG")
                image = source.convert("RGB")
            verify(image, case["payload"])
            if args.self_test:
                self_test(image, case["payload"])
        except ValueError as error:
            raise ValueError(f'{case["name"]}: {error}') from error
        print(f'{case["name"]}: decoded exact payload, black modules, white quiet zone, circular fit')
    if args.self_test:
        print("QR rejection self-tests passed")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        sys.exit(f"QR verification failed: {error}")
