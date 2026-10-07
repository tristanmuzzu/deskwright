"""Semantic GUI research spike; load in private desktop_exec with runpy.run_path.

This is NOT a production addressing or action API. Compact references are valid
only inside one returned snapshot; native paths remain in a separate side table.
No text values are collected. Unknown/unsupported controls must remain eligible
for visual inspection. All timings exclude model inference and MCP transport.
Snapshot collection is read-only. exercise_form explicitly mutates the separate
disposable form fixture and its supplied output file.
"""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from deskwright import atspi

STRUCTURAL = {"application", "panel", "grouping", "filler", "viewport", "scroll pane"}
PASSIVE = {"label", "image", "icon", "separator"}


def snapshot(app: str, *, cap: int = 400, depth: int = 30, budget_s: float = 3) -> dict:
    """Bounded prototype: semantic controls, labels, states, relations, capabilities.

    The budget is checked between provider calls, not a hard RPC timeout. Run
    inside the existing supervised worker for its external deadline. State bits
    describe what the provider reports, not proof of physical reachability.
    """
    if cap < 1 or depth < 0 or budget_s <= 0:
        raise ValueError("cap/budget must be positive and depth nonnegative")
    start = time.perf_counter()
    api = atspi._atspi()
    root = atspi._find_app(app)
    pid = atspi._app_pid(root)
    # Always qualify, even when only one instance currently exists.
    root_path = f"{root.get_name()}#{pid}"
    stack = [(root, root_path, 0, None)]
    controls, addresses, gaps = [], {}, []
    visited = 0
    while stack:
        if visited >= cap or time.perf_counter() - start >= budget_s:
            gaps.append("node budget" if visited >= cap else "time budget")
            break
        node, path, level, parent = stack.pop()
        visited += 1
        try:
            role, name = node.get_role_name(), node.get_name() or ""
            interfaces = list(node.get_interfaces())
            states = node.get_state_set()
            state = {
                key: bool(states.contains(getattr(api.StateType, key.upper())))
                for key in ("enabled", "sensitive", "showing", "visible", "focused",
                            "editable", "checked", "selected", "expanded", "defunct")
            }
            # Anonymous structure is collapsed, not its descendants. An
            # interactive role is retained even when the author forgot a name.
            keep = role not in STRUCTURAL or bool(name)
            if keep:
                ref = f"e{len(controls) + 1}"
                item = {"ref": ref, "role": role, "name": name, "parent": parent}
                if role not in PASSIVE:
                    item["state"] = state
                    item["interfaces"] = interfaces
                    # GTK4 advertises many inherited actions even on labels.
                    # Show a count; discover the complete action list on demand.
                    if "Action" in interfaces:
                        item["action_count"] = node.get_n_actions()
                        if item["action_count"] <= 3:
                            item["actions"] = [node.get_localized_name(i)
                                               for i in range(item["action_count"])]
                    if not name:
                        labels = []
                        for relation in node.get_relation_set():
                            if relation.get_relation_type() == api.RelationType.LABELLED_BY:
                                labels.extend(relation.get_target(i).get_name()
                                              for i in range(relation.get_n_targets()))
                        if labels:
                            item["labelled_by"] = labels
                controls.append(item)
                addresses[ref] = path
                parent = ref
            count = node.get_child_count()
            if count and level >= depth:
                gaps.append(f"depth limit at {path}")
                continue
            # Bound child retrieval too: a virtual table can report millions.
            available = max(0, cap - visited - len(stack))
            if count > available:
                gaps.append(f"child budget at {path}")
            children = []
            for index in range(min(count, available)):
                child = node.get_child_at_index(index)
                if child is not None:
                    children.append((child, f"{path}/{index}", level + 1, parent))
            stack.extend(reversed(children))
        except Exception as exc:
            gaps.append(f"{path}: {type(exc).__name__}: {exc}")
    return {"app": app, "pid": pid, "visited": visited, "controls": controls,
            "incomplete": bool(gaps), "gaps": gaps, "addresses": addresses,
            "elapsed_ms": (time.perf_counter() - start) * 1000}


def render(data: dict) -> str:
    """Small agent view; retain ancestry and provider capability/state signals."""
    lines = [f"app={data['app']!r} pid={data['pid']} incomplete={data['incomplete']}"]
    for item in data["controls"]:
        bits = [item["ref"], item["role"], json.dumps(item["name"], ensure_ascii=False)]
        if item["parent"]:
            bits.append(f"in={item['parent']}")
        if item.get("labelled_by"):
            bits.append(f"label={item['labelled_by']!r}")
        state = item.get("state")
        if state:
            bits += [key for key in ("editable", "checked", "selected", "expanded", "focused")
                     if state[key]]
            # Measured GTK4 editor: ENABLED is false on working controls while
            # SENSITIVE is true. Preserve both bits, do not infer disabled.
            bits.append(f"enabled={state['enabled']} sensitive={state['sensitive']}")
            if not state["showing"]:
                bits.append("not-showing")
            if state["defunct"]:
                bits.append("defunct")
        if item.get("action_count"):
            bits.append(f"actions={item.get('actions', item['action_count'])!r}")
        capabilities = [s for s in item.get("interfaces", [])
                        if s in {"EditableText", "Value", "Selection", "Table", "Text"}]
        if capabilities:
            bits.append("interfaces=" + ",".join(capabilities))
        lines.append(" ".join(bits))
    lines.extend(f"GAP: {gap}" for gap in data["gaps"])
    return "\n".join(lines)


def compare(desktop, app: str, samples: int = 7) -> dict:
    """Alternate raw tree and semantic scan order; warm each once before timing.

    Both have a 400-node/depth-30 bound but collect different properties. This
    is a view-design experiment, not an equivalent-output microbenchmark.
    """
    if not 1 <= samples <= 20:
        raise ValueError("samples must be between 1 and 20")
    desktop.call("ui_tree", app=app, depth=30)
    snapshot(app)
    rows, outputs = [], {}
    for trial in range(samples):
        order = ["raw", "semantic"] if trial % 2 == 0 else ["semantic", "raw"]
        for mode in order:
            start = time.perf_counter()
            result = (desktop.call("ui_tree", app=app, depth=30) if mode == "raw"
                      else snapshot(app))
            elapsed = (time.perf_counter() - start) * 1000
            wire = json.dumps(result, ensure_ascii=False) if mode == "raw" else render(result)
            rows.append({"trial": trial, "mode": mode, "ms": elapsed,
                         "utf8_bytes": len(wire.encode()),
                         "visited": result.get("visited", result.get("nodes")),
                         "incomplete": result.get("incomplete", result.get("truncated"))})
            outputs[mode] = result
    return {"app": app, "samples": rows, "outputs": outputs,
            "summary": {mode: {
                "median_ms": statistics.median(r["ms"] for r in rows if r["mode"] == mode),
                "median_bytes": statistics.median(r["utf8_bytes"] for r in rows if r["mode"] == mode),
            } for mode in ("raw", "semantic")}}


def exercise_form(desktop, saved_file: str, samples: int = 5) -> dict:
    """Exercise ONLY the disposable benchmarks/contract_form.py fixture.

    Launch it separately on the private desktop with saved_file as its output.
    This deliberately mutates its fields and output. No coordinates are used.
    """
    if not 1 <= samples <= 10:
        raise ValueError("samples must be between 1 and 10")
    app = "deskwright-contract-form"
    fields = desktop.call("ui_find", app=app, role="text")["results"]
    paths = {n["name"]: n["path"] for n in fields}
    if len(fields) != 5 or set(paths) != {"Name", "City", "Reference", "Notes", "Status"}:
        raise ValueError("not the expected five-field disposable fixture")
    buttons = desktop.call("ui_find", app=app, role="button", actionable_only=True)["results"]

    def press(label, mode="compact"):
        matches = [n for n in buttons if n["name"] == label]
        if len(matches) != 1:
            raise ValueError(f"ambiguous fixture button {label}")
        return desktop.call("ui_press", path=matches[0]["path"], expect_name=label,
                            expect_role="button", observation_mode=mode)

    trials = []
    for trial in range(samples):
        expected = {"Name": f"Trial {trial}", "City": "München", "Reference": "山-42",
                    "Notes": "Café — ready.", "Status": "Verified"}
        start = time.perf_counter()
        for name, value in expected.items():
            desktop.call("ui_set_text", path=paths[name], text=value, replace=True)
        press("Save")
        for path in paths.values():
            desktop.call("ui_set_text", path=path, text="", replace=True)
        press("Reopen")
        observed = {name: desktop.call("ui_read_text", path=path)["text"]
                    for name, path in paths.items()}
        disk = json.loads(Path(saved_file).read_text())
        passed = observed == expected and disk == expected
        trials.append({"seconds": time.perf_counter() - start, "passed": passed,
                       "guarded_primitives": 17, "screenshots": 0})
        if not passed:
            raise AssertionError({"observed": observed, "disk": disk, "expected": expected})
    # Hold the action and its starting state constant; vary only look policy.
    # Restore the same status label before each measured Save operation.
    observations = []
    for trial in range(7):
        for mode in (["auto", "compact"] if trial % 2 == 0 else ["compact", "auto"]):
            press("Reopen")
            start = time.perf_counter()
            result = press("Save", mode)
            passed = json.loads(Path(saved_file).read_text()) == expected
            observations.append({"trial": trial, "mode": mode,
                                 "ms": (time.perf_counter() - start) * 1000,
                                 "passed": passed, "look": result.get("look")})
            if not passed:
                raise AssertionError("Save did not preserve expected data")
    return {"kind": "scripted warm trials; excludes model/MCP transport latency",
            "trials": trials, "observations": observations,
            "median_task_seconds": statistics.median(t["seconds"] for t in trials),
            "median_save_ms": {mode: statistics.median(r["ms"] for r in observations
                                                       if r["mode"] == mode)
                               for mode in ("auto", "compact")}}


def collection_buttons(app: str, samples: int = 7) -> dict:
    """Read-only provider query, explicitly scoped to the first app window.

    Returns at most 40 button objects/names, not a complete semantic snapshot.
    Some providers advertise Collection but fail or time out; callers must
    handle that as a failed fast path, never evidence of no matching controls.
    """
    if not 1 <= samples <= 20:
        raise ValueError("samples must be between 1 and 20")
    api = atspi._atspi()
    frame = atspi._find_app(app).get_child_at_index(0)
    if frame is None or "Collection" not in frame.get_interfaces():
        return {"app": app, "supported": False}
    match = api.CollectionMatchType
    rule = api.MatchRule.new(None, match.ALL, {}, match.ALL, [api.Role.PUSH_BUTTON],
                             match.ANY, [], match.ALL, False)
    rows = []
    for _ in range(samples):
        start = time.perf_counter()
        nodes = frame.get_matches(rule, api.CollectionSortOrder.CANONICAL, 40, True)
        names = [node.get_name() for node in nodes]
        rows.append({"ms": (time.perf_counter() - start) * 1000, "names": names,
                     "possibly_truncated": len(nodes) == 40})
    return {"app": app, "supported": True, "samples": rows}
