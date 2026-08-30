#!/usr/bin/env python3
"""Turn a recording into the two things a language model can actually perceive.

The honest constraint: an LLM cannot watch a video. It sees still images. So a
recording is only useful once it has been turned into stills -- and WHICH stills,
and how they are laid out, is the whole game.

Two outputs:

1. A contact sheet. N frames, evenly spaced, each stamped with its frame number
   and timestamp, tiled into ONE image. This is the closest thing to perceiving
   motion in a single look: the eye (or the model) reads the sequence across the
   grid instead of holding a dozen separate images in working memory.

2. A motion trace. Per-frame mean absolute difference against the previous
   frame, printed as a sparkline plus the numbers. This is not a nicer way to
   look -- it is a measurement a human eyeballing a clip cannot make reliably:
   a frozen frame reads as 0.0, a stutter as a gap, a hard cut as a spike.

    python3 -m wcu.frames IN.mp4 OUTDIR                                # 12 frames, whole clip
    python3 -m wcu.frames IN.mp4 OUTDIR --cols 5 --rows 4              # denser grid
    python3 -m wcu.frames IN.mp4 OUTDIR --from-frame 55 --to-frame 66  # sub-second slice

Then Read the contact sheet PNG. Never try to Read the mp4 itself -- a model
cannot decode it, and asking for it wastes a turn.

The shebang on line 1 is load-bearing: without it the kernel hands this file to
/bin/sh, which tries to execute the docstring and hangs waiting for a closing
quote rather than failing (found 2026-08-12).
"""
from __future__ import annotations

import argparse
import itertools
import json
import pathlib
import subprocess
import sys


def probe(path: pathlib.Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=nb_frames,width,height,avg_frame_rate",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    d = json.loads(out)
    s = d["streams"][0]
    num, den = (int(x) for x in s["avg_frame_rate"].split("/"))
    fps = num / den if den else 0
    duration = float(d["format"]["duration"])
    # Fragmented mp4 -- which screencast now writes so a cut recording stays
    # readable -- carries no nb_frames, so ffprobe reports N/A. Falling through
    # with 0 made every tile land on frame 0 and produced a sheet of twelve
    # identical images that looked like a frozen capture. Derive it instead.
    frames = int(s.get("nb_frames") or 0) or round(duration * fps)
    return {
        "frames": frames,
        "w": int(s["width"]), "h": int(s["height"]),
        "fps": fps,
        "duration": duration,
    }


def contact_sheet(src: pathlib.Path, outdir: pathlib.Path, cols: int, rows: int,
                  info: dict, lo: int | None = None, hi: int | None = None,
                  name: str = "contact_sheet.png") -> pathlib.Path:
    n = cols * rows
    frames_dir = outdir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for f in frames_dir.glob("*.png"):
        f.unlink()

    # Evenly spaced across the whole clip, first and last included, so the sheet
    # shows the real start and end state rather than an arbitrary middle slice.
    lo = 0 if lo is None else lo
    hi = (info["frames"] or 1) - 1 if hi is None else hi
    picks = [lo + round(i * (hi - lo) / max(n - 1, 1)) for i in range(n)]

    tiles = []
    for i, fno in enumerate(picks):
        tile = frames_dir / f"f{i:02d}.png"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(src),
             "-vf", f"select='eq(n\\,{fno})'", "-vsync", "0",
             "-frames:v", "1", str(tile)],
            check=True,
        )
        secs = fno / info["fps"] if info["fps"] else 0
        subprocess.run(
            ["magick", str(tile), "-resize", "300x",
             "-background", "#111", "-fill", "#7dd3fc", "-pointsize", "18",
             "-gravity", "north", "label:" + f"frame {fno}  t={secs:.2f}s",
             "+swap", "-gravity", "center", "-append",
             "-bordercolor", "#333", "-border", "2",
             "-set", "label", "", str(tile)],
            check=True,
        )
        tiles.append(str(tile))

    sheet = outdir / name
    subprocess.run(
        ["magick", "montage", *tiles, "-tile", f"{cols}x{rows}",
         "-geometry", "+4+4", "-background", "#111", str(sheet)],
        check=True,
    )
    return sheet


def motion_trace(src: pathlib.Path, outdir: pathlib.Path, info: dict) -> list[float]:
    """Mean absolute difference between consecutive frames, 0..1 per frame."""
    stats = outdir / "motion.txt"
    # signalstats on a tblend difference gives a per-frame average that ffmpeg
    # will print via metadata -- cheaper and more accurate than decoding to PNGs.
    graph = ("tblend=all_mode=difference,signalstats,"
             f"metadata=print:key=lavfi.signalstats.YAVG:file={stats}")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", graph,
         "-f", "null", "-"],
        check=True,
    )
    values = []
    for line in stats.read_text().splitlines():
        if "YAVG" in line:
            try:
                values.append(float(line.split("=")[1]))
            except (IndexError, ValueError):
                pass
    return values


def sparkline(values: list[float]) -> str:
    if not values:
        return "(no data)"
    blocks = " ▁▂▃▄▅▆▇█"
    hi = max(values) or 1.0
    return "".join(blocks[min(int(v / hi * (len(blocks) - 1)), len(blocks) - 1)]
                   for v in values)


def summarise_motion(motion: list[float], capture_fps: float) -> dict:
    """Turn the raw per-frame deltas into the few numbers worth reporting.

    The duplicate-frame count is the useful one and it is not something a human
    gets from watching: if the source repaints slower than the capture rate,
    every Nth captured frame is byte-identical to the one before it, and the
    spacing between duplicates gives the source's real rate.
    """
    if not motion:
        return {}
    still = [i for i, v in enumerate(motion) if v < 0.05]
    gaps = [b - a for a, b in itertools.pairwise(still)]
    source_fps = None
    if gaps:
        period = max(set(gaps), key=gaps.count)
        if period > 1:
            source_fps = round(capture_fps / period)
    out = {
        "peak": round(max(motion), 2),
        "mean": round(sum(motion) / len(motion), 2),
        "duplicate_frames": len(still),
        "duplicate_pct": round(len(still) / len(motion) * 100),
        # None means the source kept up with the capture rate.
        "source_fps_estimate": source_fps,
    }
    out.update(_smoothness(motion))
    return out


# How many deltas to hand back. Enough to see the shape of a transition,
# few enough that the series is not the whole reply.
SERIES_MAX = 120


def _smoothness(motion: list[float]) -> dict:
    """Whether the motion was STEADY, which peak and mean cannot say.

    Reported 2026-08-18: on a first-person camera pan, peak and mean are
    dominated by how far the camera swung, so smooth and jolting both read
    3-9 and the aggregates could not tell them apart -- the session fell back
    to the project's own probe for the camera derivative. What separates them
    is the change BETWEEN consecutive deltas (jerk): steady motion has a
    small one whatever its magnitude, a stutter has a large one.

    `jerk_ratio` normalises that against the mean delta, so it is comparable
    between a gentle pan and a fast one.

    Measured 2026-08-27 on three 4s 60fps clips built to have known motion
    character (ffmpeg, a full-frame pattern panning; scratch script kept out
    of the tree, the recipe is in the commit):

        clip     jerk_ratio  duplicate%   what it is
        smooth      0.29          0       constant-rate pan, every frame new
        jitter      0.61          0       same pan, step size varies per frame
        stutter     2.01         84       same pan sampled at 10fps, held to 60

    The threshold below sits at 1.0: between the two continuous clips and the
    dropped-frame one. `steady` therefore answers "were frames delivered
    evenly", not "was the movement pretty" -- jitter is real unevenness a
    viewer can see, and it stays under the line. What separates it from
    smooth is the RATIO ITSELF (0.61 against 0.29) and the shape of `series`,
    which is what the 2026-08-18 report actually asked for; a boolean was
    never going to answer that question.

    Note the first attempt computed jerk over MOVING frames only, which
    scored the stuttery clip 0.08 -- the steadiest of the three -- because
    dropping the duplicates deletes the very evidence of the stutter. Jerk is
    over every delta for that reason; do not "fix" it back.
    """
    if len(motion) < 3:
        return {}
    jerks = [abs(b - a) for a, b in itertools.pairwise(motion)]
    mean_delta = sum(motion) / len(motion)
    mean_jerk = sum(jerks) / len(jerks)
    ratio = round(mean_jerk / mean_delta, 2) if mean_delta else 0.0
    # The series itself, decimated to a readable length: the ask was for a
    # per-frame series rather than two aggregates, because "is it smooth"
    # is a question about the SHAPE.
    # Ceiling division: floor division leaves the series one bucket over the
    # cap (500 deltas // 120 gives step 4, and 500[::4] is 125 items).
    step = max(1, -(-len(motion) // SERIES_MAX))
    return {
        "jerk_ratio": ratio,
        "steady": ratio < 1.0,
        "series_step": step,
        "series": [round(v, 2) for v in motion[::step]],
        "reading": ("steady -- consecutive frame deltas are close to each "
                    "other" if ratio < 1.0 else
                    "uneven -- consecutive frame deltas jump around, which is "
                    "what dropped or repeated frames look like"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("outdir")
    ap.add_argument("--cols", type=int, default=4)
    ap.add_argument("--rows", type=int, default=3)
    ap.add_argument("--from-frame", type=int, default=None,
                    help="start of a dense slice, in frames")
    ap.add_argument("--to-frame", type=int, default=None)
    ap.add_argument("--json", action="store_true",
                    help="emit one JSON object instead of the human report")
    a = ap.parse_args()

    src = pathlib.Path(a.video)
    outdir = pathlib.Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    info = probe(src)
    sheet = contact_sheet(src, outdir, a.cols, a.rows, info,
                          a.from_frame, a.to_frame,
                          "dense_sheet.png" if a.from_frame is not None
                          else "contact_sheet.png")
    motion = motion_trace(src, outdir, info)
    stats = summarise_motion(motion, info["fps"])

    if a.json:
        print(json.dumps({
            "sheet": str(sheet),
            "tiles": a.cols * a.rows,
            "source": {k: (round(v, 2) if isinstance(v, float) else v)
                       for k, v in info.items()},
            "motion": stats,
            "next_step": f"Read {sheet} -- it is the frames, tiled. Do not Read the mp4.",
        }))
        return 0

    print(f"source: {info['w']}x{info['h']}  {info['frames']} frames  "
          f"{info['fps']:.1f} fps  {info['duration']:.2f}s")
    print(f"contact sheet: {sheet}  ({a.cols}x{a.rows} = {a.cols * a.rows} frames)")
    if motion:
        # Downsample to one column per ~0.1s so the sparkline stays readable.
        step = max(1, len(motion) // 90)
        print(f"\nmotion, {len(motion)} inter-frame deltas "
              f"(0 = identical frame, higher = more changed):")
        print("  " + sparkline(motion[::step]))
        print(f"  peak {stats['peak']}   mean {stats['mean']}   "
              f"duplicate frames: {stats['duplicate_frames']}/{len(motion)} "
              f"({stats['duplicate_pct']}%)")
        if stats["source_fps_estimate"]:
            print(f"  -> the SOURCE is repainting at about "
                  f"{stats['source_fps_estimate']} fps, below the "
                  f"{info['fps']:.0f} fps capture rate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
