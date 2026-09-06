#!/usr/bin/env python3
"""Saved-artifact checks; visual composition and GUI reopening need separate evidence."""
import argparse
import json
from pathlib import Path

from PIL import Image

parser = argparse.ArgumentParser()
parser.add_argument('directory', type=Path)
args = parser.parse_args()
png = args.directory / 'alpine-lake.png'
xcf = args.directory / 'alpine-lake.xcf'
result = {'png_exists': png.is_file(), 'xcf_exists': xcf.is_file()}
if png.is_file():
    with Image.open(png) as source:
        source.load()
        result['size'] = list(source.size)
        rgb = source.convert('RGB')
        colors = rgb.getcolors(rgb.width * rgb.height)
        counts = {tuple(color): count for count, color in colors}
        result['exact_color_pixels'] = {
            name: counts.get(color, 0) for name, color in {
                'dark_blue': (35, 75, 92), 'orange': (232, 163, 75), 'white': (255, 255, 255)
            }.items()
        }
if xcf.is_file():
    with xcf.open('rb') as source:
        result['xcf_magic_valid'] = source.read(9) == b'gimp xcf '
result['file_checks_passed'] = bool(
    result.get('size') == [800, 500] and result.get('xcf_magic_valid')
    and all(count > 100 for count in result.get('exact_color_pixels', {}).values())
)
result['visual_shapes_and_reopen'] = 'not established by this file check; inspect the GUI evidence'
print(json.dumps(result, indent=2))
raise SystemExit(0 if result['file_checks_passed'] else 1)
