#!/usr/bin/env python3
"""Read and drive the desktop through AT-SPI.

This is the part of desktop automation that Wayland did not take away. Pointer
position is unknowable to a client, global input injection is focus-blind, and
gnome-shell's screenshot and accelerator D-Bus methods are refused outright --
but the accessibility tree still exposes every application's real widgets, with
names, roles, screen coordinates and invokable actions.

Acting through AT-SPI is also strictly better than clicking pixels: `do_action`
presses the actual button, so it cannot miss, cannot be defeated by a window
moving, and needs no pointer at all.

Usage:
    wcu-atspi apps
    wcu-atspi tree "Google Chrome" [--depth 4]
    wcu-atspi find "Reload" [--app "Google Chrome"] [--role push_button]
    wcu-atspi actions <path>
    wcu-atspi do <path> [action_index]

<path> is the index path printed by `tree`/`find`, e.g. "Google Chrome/0/3/1".
It is resolved fresh on every call, so it is only valid while the tree is
unchanged -- find, then act, and do not cache.
"""
from __future__ import annotations

import argparse
import sys

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi


def desktop():
    Atspi.init()
    return Atspi.get_desktop(0)


def children(node):
    for i in range(node.get_child_count()):
        child = node.get_child_at_index(i)
        if child is not None:
            yield i, child


def find_app(name: str):
    for _, app in children(desktop()):
        if app.get_name() == name:
            return app
    raise SystemExit(f"no application named {name!r} (try: atspi_ui.py apps)")


def resolve(path: str):
    """Turn "App/0/3/1" back into a live accessible."""
    app_name, *indices = path.split("/")
    node = find_app(app_name)
    for part in indices:
        node = node.get_child_at_index(int(part))
        if node is None:
            raise SystemExit(f"path {path!r} no longer resolves")
    return node


def describe(node, path: str) -> str:
    name = node.get_name() or ""
    role = node.get_role_name()
    bits = [f"{path}  [{role}]"]
    if name:
        bits.append(repr(name))
    try:
        extents = node.get_extents(Atspi.CoordType.SCREEN)
        if extents.width > 0 and extents.height > 0:
            bits.append(f"@{extents.x},{extents.y} {extents.width}x{extents.height}")
    except Exception:
        pass
    try:
        n_actions = node.get_action_iface() and node.get_n_actions()
        if n_actions:
            names = [node.get_localized_name(i) for i in range(n_actions)]
            bits.append(f"actions={names}")
    except Exception:
        pass
    return " ".join(bits)


def walk(node, path, depth, max_depth, out):
    out.append(describe(node, path))
    if depth >= max_depth:
        return
    for i, child in children(node):
        walk(child, f"{path}/{i}", depth + 1, max_depth, out)


def cmd_apps(_args):
    for _, app in children(desktop()):
        print(f"{app.get_name()!r}  children={app.get_child_count()}")


def cmd_tree(args):
    app = find_app(args.app)
    out = []
    walk(app, args.app, 0, args.depth, out)
    print("\n".join(out))


def cmd_find(args):
    roots = [find_app(args.app)] if args.app else [a for _, a in children(desktop())]
    needle = args.text.lower()
    hits = 0
    for root in roots:
        out = []
        walk(root, root.get_name(), 0, args.depth, out)
        for line in out:
            if needle in line.lower() and (not args.role or f"[{args.role}]" in line):
                print(line)
                hits += 1
    if not hits:
        sys.exit(1)


def cmd_actions(args):
    node = resolve(args.path)
    n = node.get_n_actions()
    print(f"{args.path}: {n} action(s)")
    for i in range(n):
        print(f"  [{i}] {node.get_localized_name(i)}  -- {node.get_action_description(i)}")


def cmd_do(args):
    node = resolve(args.path)
    if node.get_n_actions() <= args.index:
        raise SystemExit(f"no action {args.index} on {args.path}")
    name = node.get_localized_name(args.index)
    ok = node.do_action(args.index)
    print(f"{args.path}: {name} -> {'ok' if ok else 'FAILED'}")
    sys.exit(0 if ok else 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("apps").set_defaults(func=cmd_apps)

    p = sub.add_parser("tree")
    p.add_argument("app")
    p.add_argument("--depth", type=int, default=4)
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("find")
    p.add_argument("text")
    p.add_argument("--app")
    p.add_argument("--role")
    p.add_argument("--depth", type=int, default=8)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("actions")
    p.add_argument("path")
    p.set_defaults(func=cmd_actions)

    p = sub.add_parser("do")
    p.add_argument("path")
    p.add_argument("index", type=int, nargs="?", default=0)
    p.set_defaults(func=cmd_do)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
