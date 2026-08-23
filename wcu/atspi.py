from __future__ import annotations

import time
from typing import Any

from .capture import _look, _look_before
from .errors import ToolError
from .shell import list_windows

MAX_TREE_NODES = 400          # keeps a tree dump inside a sane token budget
DEFAULT_TREE_DEPTH = 8
# GTK4 nests brutally: gnome-text-editor's document text view sits at depth 23
# behind a stack of anonymous panels and groupings. A depth-8 search finds
# nothing at all in a modern GNOME app, which reads as "the app has no widgets"
# rather than "you did not look far enough". Search deep and cap on node count.
DEFAULT_FIND_DEPTH = 30
MAX_FIND_NODES = 4000


# ---- AT-SPI --------------------------------------------------------------
def _atspi():
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except Exception as e:
        raise ToolError(f"AT-SPI is unavailable ({type(e).__name__}: {e})") from None
    Atspi.init()
    return Atspi


def _describe(node, path: str) -> dict:
    out: dict[str, Any] = {"path": path, "role": node.get_role_name(),
                           "name": node.get_name() or ""}
    try:
        ext = node.get_extents(0)  # 0 == Atspi.CoordType.SCREEN
        if ext.width > 0 and ext.height > 0:
            out["bounds"] = {"x": ext.x, "y": ext.y, "w": ext.width, "h": ext.height}
    except Exception:
        pass
    try:
        if node.get_action_iface() and node.get_n_actions() > 0:
            out["actions"] = [node.get_localized_name(i)
                              for i in range(node.get_n_actions())]
    except Exception:
        pass
    return out


def _app_labels(apps: list) -> dict:
    """Path roots for these applications: the bare name, or `name#pid` when the
    name alone would address more than one of them."""
    named = []
    for app in apps:
        try:
            named.append((app, app.get_name() or "<unnamed application>"))
        except Exception:
            named.append((app, "<application that would not identify itself>"))
    counts: dict[str, int] = {}
    for _app, name in named:
        counts[name] = counts.get(name, 0) + 1
    return {id(app): (f"{name}#{_app_pid(app)}" if counts[name] > 1 else name)
            for app, name in named}


def _app_pid(app) -> int | None:
    try:
        return int(app.get_process_id())
    except Exception:
        return None


def _find_app(app_name: str):
    """The AT-SPI application called `app_name`, or `name#<pid>` when several are.

    Taking the first match was wrong in a way that produced no error and the
    wrong answer: two windows of the same program are two applications on this
    bus, so `ui_find` would walk instance A, hand back a path rooted at the
    shared name, and `ui_press` would resolve that path against instance B and
    act on whatever happened to sit at those indices. Measured 2026-08-22 with
    two Chrome windows -- ui_find located a button on the page, ui_press
    resolved into the other window's tree and failed. Failing loudly with the
    pids is the only safe behaviour; silently acting on the wrong window is not.
    """
    Atspi = _atspi()
    desk = Atspi.get_desktop(0)

    wanted, _, pid_text = str(app_name).partition("#")
    wanted_pid = int(pid_text) if pid_text.isdigit() else None

    apps, names = [], []
    for i in range(desk.get_child_count()):
        app = desk.get_child_at_index(i)
        if app is None:
            continue
        try:
            name = app.get_name() or ""
        except Exception:
            continue
        names.append(name)
        apps.append((name, app))

    exact = [(n, a) for n, a in apps if n == wanted]
    matches = exact or [(n, a) for n, a in apps if wanted.lower() in n.lower()]

    if wanted_pid is not None:
        for name, app in matches or apps:
            if _app_pid(app) == wanted_pid:
                return app
        raise ToolError(
            f"no application {wanted!r} with pid {wanted_pid} on the AT-SPI bus. "
            "Check list_windows -- it reports the pid of every window."
        )

    if not matches:
        raise ToolError(
            f"no application named {app_name!r} on the AT-SPI bus. Present: "
            + ", ".join(repr(n) for n in names)
            + ". An app started while toolkit-accessibility was false exposes a "
              "stunted tree for its whole life -- restart the app, not the setting."
        )
    if len(matches) > 1:
        listed = ", ".join(f"{n!r}#{_app_pid(a)}" for n, a in matches)
        raise ToolError(
            f"{len(matches)} applications match {app_name!r}, and an index path is "
            f"only meaningful inside one of them: {listed}. Pass the pid form, e.g. "
            f"app: \"{matches[0][0]}#{_app_pid(matches[0][1])}\". list_windows "
            "reports the pid of every window, so you can pick the right one."
        )
    return matches[0][1]


def _walk(node, path: str, depth: int, max_depth: int, out: list[dict],
          cap: int = MAX_TREE_NODES) -> None:
    if len(out) >= cap:
        return
    out.append(_describe(node, path))
    if depth >= max_depth:
        return
    for i in range(node.get_child_count()):
        child = node.get_child_at_index(i)
        if child is not None:
            _walk(child, f"{path}/{i}", depth + 1, max_depth, out, cap)


TEXT_ROLES = ("text", "document_text", "entry", "document frame", "paragraph")


def _text_ifaces(node) -> tuple[Any, Any]:
    """(text_iface, editable_iface) -- either may be None."""
    try:
        text = node.get_text_iface()
    except Exception:
        text = None
    try:
        editable = node.get_editable_text_iface()
    except Exception:
        editable = None
    return text, editable


def _read_text(node) -> str:
    Atspi = _atspi()
    text_iface, _ = _text_ifaces(node)
    if text_iface is None:
        raise ToolError(
            f"{node.get_role_name()} {node.get_name()!r} exposes no AT-SPI text "
            "interface, so its content cannot be read"
        )
    count = Atspi.Text.get_character_count(text_iface)
    return Atspi.Text.get_text(text_iface, 0, count) if count else ""


def _find_text_widget(app_name: str, path: str | None):
    """The editable text widget of an app, or the one at an explicit path."""
    if path:
        return _resolve_path(path)
    app = _find_app(app_name)
    collected: list[dict] = []
    _walk(app, app.get_name(), 0, DEFAULT_FIND_DEPTH, collected, cap=MAX_FIND_NODES)
    candidates = [n for n in collected if n["role"] in TEXT_ROLES]
    if not candidates:
        raise ToolError(
            f"no text widget in {app_name!r} after reading {len(collected)} nodes to "
            f"depth {DEFAULT_FIND_DEPTH}. If the app was started while "
            "toolkit-accessibility was false its tree is stunted for its whole "
            "life -- restart the app."
        )

    # The FOCUSED one first. "The app's first text widget" is the wrong document
    # the moment an editor has two tabs open, which on a real desktop it usually
    # does: typing goes to the visible tab and the readback came from whichever
    # tab happened to be first in the tree, so type_text reported that the wrong
    # characters had arrived when they had arrived perfectly. Documented as a
    # known fragility in tests/test_e2e_real_task.py since 2026-08-17.
    live_candidates = []
    for node in candidates:
        try:
            live = _resolve_path(node["path"])
        except ToolError:
            continue
        _, editable = _text_ifaces(live)
        live_candidates.append((live, editable is not None, _is_focused(live)))

    for want_focus in (True, False):
        for live, editable, focused in live_candidates:
            if editable and focused == want_focus:
                return live
    for live, _editable, focused in live_candidates:
        if focused:
            return live
    if live_candidates:
        return live_candidates[0][0]
    return _resolve_path(candidates[0]["path"])


def _is_focused(node) -> bool:
    """Whether this widget holds the keyboard, according to AT-SPI."""
    try:
        Atspi = _atspi()
        state = node.get_state_set()
        return bool(state.contains(Atspi.StateType.FOCUSED))
    except Exception:
        return False


def _resolve_path(path: str):
    app_name, *indices = str(path).split("/")
    node = _find_app(app_name)
    for part in indices:
        try:
            node = node.get_child_at_index(int(part))
        except (ValueError, TypeError):
            raise ToolError(f"{path!r} is not a valid index path") from None
        if node is None:
            raise ToolError(
                f"{path!r} no longer resolves -- the tree changed since it was found. "
                "Call ui_find again; paths are never cacheable."
            )
    return node


def list_atspi_apps() -> list[dict]:
    Atspi = _atspi()
    desk = Atspi.get_desktop(0)
    apps = []
    for i in range(desk.get_child_count()):
        app = desk.get_child_at_index(i)
        if app is None:
            continue
        try:
            apps.append({"name": app.get_name(), "pid": _app_pid(app),
                         "children": app.get_child_count()})
        except Exception:
            continue
    seen: dict[str, int] = {}
    for a in apps:
        seen[a["name"]] = seen.get(a["name"], 0) + 1
    for a in apps:
        if seen[a["name"]] > 1:
            # Two windows of one program are two applications here. Say so, and
            # give the form that addresses one of them.
            a["address_as"] = f'{a["name"]}#{a["pid"]}'
    return apps


def tool_ui_apps(_: dict) -> dict:
    apps = list_atspi_apps()
    return {"count": len(apps), "apps": apps}


def tool_ui_tree(a: dict) -> dict:
    app = str(a.get("app") or "")
    if not app:
        raise ToolError("app is required (see ui_apps for the names on the bus)")
    depth = int(a.get("depth") or DEFAULT_TREE_DEPTH)
    node = _find_app(app)
    out: list[dict] = []
    _walk(node, node.get_name(), 0, depth, out)
    truncated = len(out) >= MAX_TREE_NODES
    return {"app": node.get_name(), "depth": depth, "nodes": len(out),
            "truncated": truncated,
            "note": (f"stopped at {MAX_TREE_NODES} nodes; narrow with ui_find "
                     "instead of raising depth") if truncated else "",
            "tree": out}


def tool_ui_find(a: dict) -> dict:
    text = str(a.get("text") or "")
    role = a.get("role")
    depth = int(a.get("depth") or DEFAULT_FIND_DEPTH)
    actionable_only = bool(a.get("actionable_only", False))
    if not text and not role and not actionable_only:
        raise ToolError(
            "give text, or role, or actionable_only:true. Without any of them this "
            "would dump the whole tree -- use ui_tree for that."
        )

    if a.get("app"):
        roots = [_find_app(str(a["app"]))]
    else:
        Atspi = _atspi()
        desk = Atspi.get_desktop(0)
        roots = [desk.get_child_at_index(i) for i in range(desk.get_child_count())]
        roots = [r for r in roots if r is not None]

    # A path is only meaningful inside ONE application, so when two of them
    # share a name the path has to carry the pid or ui_press will resolve it
    # against the wrong instance -- silently, and against whatever sits at those
    # indices there.
    labels = _app_labels(roots)

    needle = text.lower()
    hits: list[dict] = []
    unreachable: list[str] = []
    for root in roots:
        # One AppArmor-confined snap used to end the whole scan: hitting
        # snap.telegram-desktop raised out of the loop and ui_find returned
        # nothing at all, from every other application as well. An app that
        # cannot be read is a gap in the answer, not the end of it.
        name = labels.get(id(root), "<unnamed application>")
        collected: list[dict] = []
        try:
            _walk(root, name, 0, depth, collected, cap=MAX_FIND_NODES)
        except Exception as e:
            unreachable.append(f"{name}: {str(e).strip()[:120] or type(e).__name__}")
            continue
        for node in collected:
            if needle and needle not in node["name"].lower() \
                    and needle not in node["path"].lower():
                continue
            if role and node["role"] != role:
                continue
            if actionable_only and not node.get("actions"):
                continue
            hits.append(node)
    out = {
        "query": text or (role or "actionable widgets"),
        "matches": len(hits), "results": hits[:40],
        "hint": ("pass the path plus expect_name or expect_role to ui_press; "
                 "pressing the real widget cannot miss, and paths go stale as soon "
                 "as the tree changes"),
    }
    if unreachable:
        out["unreachable_apps"] = unreachable
        out["unreachable_note"] = (
            "these applications refused to be read (AppArmor-confined snaps do "
            "this) and were skipped; anything inside them is not in these results. "
            "find_text reads them from the pixels instead."
        )
    return out


def tool_ui_press(a: dict) -> dict:
    path = str(a.get("path") or "")
    if not path:
        raise ToolError("path is required (get one from ui_find)")
    expect_name = a.get("expect_name")
    expect_role = a.get("expect_role")
    # An empty expect_name defeats the whole check, because "" is a substring of
    # every string -- expect_name="" would have matched a widget called
    # "Delete Everything". Treat blank as absent.
    if isinstance(expect_name, str) and not expect_name.strip():
        expect_name = None
    if isinstance(expect_role, str) and not expect_role.strip():
        expect_role = None
    if expect_name is None and expect_role is None:
        raise ToolError(
            "expect_name or expect_role is required, and neither may be blank. An "
            "AT-SPI index path is only valid while the tree is unchanged, so acting "
            "on one without checking what it now points at is how you press the "
            "wrong widget."
        )
    index = int(a.get("action_index") or 0)

    node = _resolve_path(path)
    actual_name = node.get_name() or ""
    actual_role = node.get_role_name()
    if expect_name is not None and str(expect_name).lower() not in actual_name.lower():
        raise ToolError(
            f"refusing to act: {path} now points at {actual_name!r} [{actual_role}], "
            f"not {expect_name!r}. The tree moved -- call ui_find again."
        )
    if expect_role is not None and expect_role != actual_role:
        raise ToolError(
            f"refusing to act: {path} is a {actual_role}, not a {expect_role}."
        )

    n_actions = node.get_n_actions() if node.get_action_iface() else 0
    if n_actions <= index:
        raise ToolError(
            f"{path} ({actual_name!r} [{actual_role}]) exposes {n_actions} action(s), "
            f"so action_index {index} does not exist"
        )
    action_name = node.get_localized_name(index)
    watching = _look_before(a)
    ok = node.do_action(index)
    if not ok:
        raise ToolError(f"do_action({index}) on {path} returned false; nothing happened")
    result = {"path": path, "widget": actual_name, "role": actual_role,
              "action": action_name,
              "detail": f"pressed {action_name!r} on {actual_name!r} [{actual_role}]"}
    return _look(a, result, watching)


def tool_ui_read_text(a: dict) -> dict:
    app = str(a.get("app") or "")
    path = a.get("path")
    if not app and not path:
        raise ToolError("app or path is required")
    node = _find_text_widget(app, path)
    content = _read_text(node)
    return {"path": path or "auto-located", "role": node.get_role_name(),
            "characters": len(content), "text": content}


def tool_ui_set_text(a: dict) -> dict:
    """Write text through AT-SPI EditableText -- no focus, no ydotool.

    This is the best text-entry path on this machine and it is worth knowing why.
    ydotool injects below the compositor and is focus-blind, so it needs a window
    activated first and can still lose the race. AT-SPI hands the characters
    straight to the widget: it works on an unfocused window, works while the
    screen is locked, and can be verified by reading the widget back.
    """
    text = a.get("text")
    if not isinstance(text, str):
        raise ToolError("text is required")
    app = str(a.get("app") or "")
    path = a.get("path")
    if not app and not path:
        raise ToolError("app or path is required")
    replace = bool(a.get("replace", False))

    node = _find_text_widget(app, path)
    Atspi = _atspi()
    text_iface, editable = _text_ifaces(node)
    if editable is None:
        raise ToolError(
            f"{node.get_role_name()} {node.get_name()!r} is not editable through "
            "AT-SPI. Use type_text with an explicit target window instead, "
            "accepting that injection is focus-blind."
        )

    before = _read_text(node)
    if replace and before:
        Atspi.EditableText.delete_text(editable, 0, len(before))
    offset = 0 if replace else Atspi.Text.get_character_count(text_iface)
    if not node.insert_text(offset, text, len(text)):
        raise ToolError("insert_text returned false; nothing was written")

    time.sleep(0.2)
    after = _read_text(node)
    if text not in after:
        raise ToolError(
            "insert_text reported success but the text is not in the widget "
            f"(now {len(after)} chars). Treat this as a failure, not a success."
        )
    # With replace=True, `text in after` is too weak: a no-op delete_text leaves
    # the old content, the new text is found anyway, and the tool would report
    # verified:True on a widget that was never actually cleared.
    if replace and after.strip() != text.strip():
        raise ToolError(
            f"replace=True did not clear the widget: it holds {len(after)} chars "
            f"but {len(text)} were written. delete_text appears to be a no-op on "
            f"this widget ({node.get_role_name()}); content now starts "
            f"{after[:60]!r}."
        )
    return {"role": node.get_role_name(), "wrote": len(text),
            "characters_before": len(before), "characters_after": len(after),
            "verified": True,
            "detail": f"wrote {len(text)} chars and read them back out of the widget"}


def _window_for_atspi_app(app_name: str) -> dict | None:
    """The window belonging to an AT-SPI application name.

    The reverse of _atspi_app_for_window, and needed for the same reason: the
    two names are not the same string. Without it a do_steps sequence made
    entirely of ui_press steps has no window to look at, falls back to the whole
    desktop, and reports "nothing changed" for six presses that all worked --
    because a calculator's answer is 8 cells of 1920x1080 and whatever else is
    on screen is moving.
    """
    def norm(s: str) -> str:
        return "".join(c for c in (s or "").lower() if c.isalnum())

    wanted = norm(app_name)
    if not wanted:
        return None
    try:
        windows = list_windows()
    except ToolError:
        return None
    for w in windows:
        cls = norm(w.get("wm_class"))
        if cls and (cls in wanted or wanted in cls):
            return w
    return None


def _atspi_app_for_window(window: dict) -> str | None:
    """Best-effort map a window's wm_class to an AT-SPI application name.

    They are not the same string: gnome-text-editor's wm_class is
    'org.gnome.TextEditor' while its AT-SPI name is 'gnome-text-editor'. Compare
    with separators and case stripped.
    """
    def norm(s: str) -> str:
        return "".join(c for c in (s or "").lower() if c.isalnum())

    wanted = norm(window.get("wm_class"))
    if not wanted:
        return None
    try:
        names = [a["name"] for a in list_atspi_apps()]
    except ToolError:
        return None
    for name in names:
        n = norm(name)
        if n and (n in wanted or wanted in n):
            return name
    return None


def _clickable_widgets(app_name: str, limit: int) -> list[dict]:
    """Widgets that can be pressed, each with the screen box to press it at.

    The AT-SPI tree already carries screen extents, so this is the answer to
    "where do I click for X" without measuring anything off a screenshot. Press
    them with ui_press where possible -- it cannot miss -- and use these
    coordinates when the widget only responds to a real pointer.
    """
    app = _find_app(app_name)
    collected: list[dict] = []
    _walk(app, app.get_name(), 0, DEFAULT_FIND_DEPTH, collected, cap=MAX_FIND_NODES)
    out = []
    for node in collected:
        bounds = node.get("bounds")
        if not bounds or not node.get("actions"):
            continue
        if bounds["w"] < 4 or bounds["h"] < 4:
            continue
        out.append({
            "name": node["name"], "role": node["role"], "path": node["path"],
            "bounds": bounds,
            "click_at": [bounds["x"] + bounds["w"] // 2, bounds["y"] + bounds["h"] // 2],
        })
        if len(out) >= limit:
            break
    return out
