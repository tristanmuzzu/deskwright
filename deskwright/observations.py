"""Bounded, process-local image coordinate frames. Restart invalidates all IDs."""
from __future__ import annotations

import math
import uuid
from collections import OrderedDict

from .errors import ToolError

FRAMES = OrderedDict()


def geometry():
    from .shell import list_windows
    return sorted((w['id'], w['x'], w['y'], w['width'], w['height']) for w in list_windows())


def changed_at(before: list, after: list, x: float, y: float) -> str | None:
    """Whether the window geometry changed AT this point since the observation.

    Any change anywhere used to expire every observation. On 2026-11-09 that
    refused a click into a Nautilus popover: opening the popover added a
    surface, so the screenshot taken a moment earlier counted as stale even
    where nothing under the target point had moved. The image is still a
    true map of every point no window appeared at, left, or moved across;
    those are the only points refused now.
    """
    def inside(rect, px, py):
        _id, rx, ry, rw, rh = rect
        return rx <= px < rx + rw and ry <= py < ry + rh
    old = {r[0]: r for r in before}
    new = {r[0]: r for r in after}
    for wid, rect in new.items():
        if old.get(wid) != rect:
            if inside(rect, x, y):
                return f"window {wid} {'moved or resized' if wid in old else 'appeared'} there"
            if wid in old and inside(old[wid], x, y):
                return f"window {wid} moved away from there"
    for wid, rect in old.items():
        if wid not in new and inside(rect, x, y):
            return f"window {wid} that was there has closed"
    return None


def register(result: dict, origin: tuple, native: tuple, shown: tuple) -> None:
    from .execution import session_key
    try:
        signature = geometry()
    except Exception:
        # The image remains useful, but cannot promise guarded coordinate input.
        result['observation_unavailable'] = 'window geometry unavailable'
        return
    key = uuid.uuid4().hex
    frame = {'id': key, 'session': session_key(), 'origin': list(origin),
             'source_size': list(native), 'image_size': list(shown), 'geometry': signature,
             'path': result.get('path')}
    FRAMES[key] = frame
    while len(FRAMES) > 64:
        FRAMES.popitem(last=False)
    result['observation'] = {k: v for k, v in frame.items() if k not in ('geometry', 'path')}


def map_input(name: str, args: dict) -> dict:
    key = args.pop('observation_id', None)
    if key is None:
        return args
    from .execution import session_key
    if name not in {'pointer_click', 'pointer_move', 'pointer_drag', 'pointer_path', 'pointer_scroll'}:
        raise ToolError('observation_id applies only to pointer input', code='bad_args')
    if not isinstance(key, str):
        raise ToolError('observation_id must be a string', code='bad_args')
    f = FRAMES.get(key)
    if not f or f['session'] != session_key():
        raise ToolError('observation expired or belongs to another desktop; observe again',
                        code='stale_observation')
    current = geometry()
    iw, ih = f['image_size']
    sw, sh = f['source_size']
    ox, oy = f['origin']

    def point(x, y):
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in (x, y)) or not (0 <= x < iw and 0 <= y < ih):
            raise ToolError('point is outside the observed image', code='bad_args')
        sx, sy = ox + x * sw / iw, oy + y * sh / ih
        culprit = changed_at(f['geometry'], current, sx, sy)
        if culprit:
            raise ToolError(f'desktop geometry changed under ({round(sx)}, {round(sy)}): '
                            f'{culprit}; observe again', code='stale_observation')
        return [sx, sy]

    if name == 'pointer_path':
        raw = args.get('points')
        if not isinstance(raw, list) or any(not isinstance(p, (list, tuple)) or len(p) != 2 for p in raw):
            raise ToolError('points must be [x, y] pairs', code='bad_args')
        args['points'] = [point(*p) for p in raw]
    elif name == 'pointer_drag':
        args['from_x'], args['from_y'] = point(args.get('from_x'), args.get('from_y'))
        args['to_x'], args['to_y'] = point(args.get('to_x'), args.get('to_y'))
    else:
        args['x'], args['y'] = point(args.get('x'), args.get('y'))
    return args
