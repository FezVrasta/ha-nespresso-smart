#!/usr/bin/env python3
"""Build the integration's brand images from the Nespresso app icon.

Takes the 512x512 store icon shipped in the XAPK and reshapes it into an
iOS-style squircle, then writes the sizes Home Assistant wants:

    custom_components/nespresso_smart/brand/icon.png      256x256
    custom_components/nespresso_smart/brand/icon@2x.png   512x512

The squircle is a superellipse, |x|^n + |y|^n = 1. Apple's icon shape is a
continuous-curvature rounded rect; n = 5 tracks it closely enough that the two
are hard to tell apart at icon sizes, and it avoids the corner "wobble" you get
from a plain rounded rectangle.

The source icon's own corners are only slightly rounded, so the squircle is
strictly inside them and masking never clips the artwork. The N and the ring's
dots sit at the centre and at the edge midpoints, away from the corners.

    python3 tools/make_brand_icon.py
"""

from __future__ import annotations

import pathlib
import sys

from PIL import Image, ImageChops, ImageDraw

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "work" / "xapk" / "icon.png"
OUT = REPO / "custom_components" / "nespresso_smart" / "brand"

#: Superellipse exponent. Lower is rounder (4 looks blobby, 2 would be a circle),
#: higher is boxier with flatter sides (6 is already noticeably square). 5 is the
#: usual approximation of the iOS app-icon shape.
EXPONENT = 5.0

#: Supersampling factor for the mask. The squircle edge is a curve, so we draw
#: it big and downsample to get clean antialiasing rather than jaggies.
SUPERSAMPLE = 8


def squircle_mask(size: int, exponent: float = EXPONENT) -> Image.Image:
    """Return an 'L' mask of a superellipse filling a size x size square."""
    hi = size * SUPERSAMPLE
    mask = Image.new("L", (hi, hi), 0)
    draw = ImageDraw.Draw(mask)

    r = hi / 2.0
    # Walk the top edge, solve the superellipse for y, and mirror. Building a
    # polygon this way keeps the curve exact rather than approximating with arcs.
    points: list[tuple[float, float]] = []
    steps = hi
    for i in range(steps + 1):
        x = -1.0 + 2.0 * i / steps
        y = (max(0.0, 1.0 - abs(x) ** exponent)) ** (1.0 / exponent)
        points.append((r + x * r, r - y * r))
    for i in range(steps, -1, -1):
        x = -1.0 + 2.0 * i / steps
        y = (max(0.0, 1.0 - abs(x) ** exponent)) ** (1.0 / exponent)
        points.append((r + x * r, r + y * r))

    draw.polygon(points, fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def main() -> None:
    if not SRC.is_file():
        sys.exit(
            f"Source icon not found at {SRC}\n"
            "Unpack the XAPK first:  unzip -o *.xapk -d work/xapk"
        )

    src = Image.open(SRC).convert("RGBA")
    if src.width != src.height:
        sys.exit(f"Source icon must be square, got {src.width}x{src.height}")

    OUT.mkdir(parents=True, exist_ok=True)

    for name, size in (("icon@2x.png", 512), ("icon.png", 256)):
        img = (
            src.resize((size, size), Image.LANCZOS) if src.width != size else src.copy()
        )
        # Multiply into the existing alpha so any transparency in the source
        # survives instead of being overwritten.
        img.putalpha(ImageChops.multiply(img.getchannel("A"), squircle_mask(size)))
        dest = OUT / name
        img.save(dest, "PNG", optimize=True)
        print(f"wrote {dest.relative_to(REPO)}  {size}x{size}")


if __name__ == "__main__":
    main()
