#!/usr/bin/env python3
"""desktop_exec globals match its description; no desktop needed.

Five sessions (2026-09-11 to 2026-11-07) hit NameError calling click() or
key() as the description promised; one overnight run spent ~237 calls polling
a subprocess. Handlers are patched out, so nothing reaches a screen.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deskwright.code_runtime import tool_desktop_exec


def run(code, reset=True):
    return tool_desktop_exec({'reset': reset, 'code': code})


def test_helpers_named_in_the_description_are_globals():
    with patch('deskwright.code_runtime.execute', return_value={'ok': True}) as ex:
        out = run("click(10, 20, target=5)\nkey('ctrl+s', target=5)\n"
                  "type('hi', target=5)\nscroll(1, 2, dy=3)\nlog('ok')")
    assert out['all_ok'], out
    tools = [c.args[0] for c in ex.call_args_list]
    assert tools == ['pointer_click', 'press_keys', 'type_text', 'pointer_scroll']
    assert ex.call_args_list[0].args[1]['expect_window'] == 5


def test_click_without_target_is_allowed_and_unguarded():
    with patch('deskwright.code_runtime.execute', return_value={'ok': True}) as ex:
        assert run("click(1, 2)")['all_ok']
    assert 'expect_window' not in ex.call_args.args[1]


def test_type_still_works_as_the_builtin():
    out = run("log(type(3).__name__)\nlog(type('X', (), {}).__name__)")
    assert out['output'] == ['int', 'X'], out



def test_print_output_arrives_as_whole_lines():
    """print() writes args, separators and newline as separate chunks; a
    trailing partial line is flushed when the code ends."""
    out = run("print('RC', 0)\nprint('a\\nb', end='')\n"
              "import sys\nsys.stderr.write('err\\n')")
    assert out['output'] == ['RC 0', 'a', 'err', 'b'], out


def test_describe_lists_tools_and_suggests_on_a_wrong_name():
    out = run("log(len(describe()) > 20)\ndescribe('mouse_click')")
    assert out['output'] == ['true']
    assert 'Did you mean' in out['error']['message']
    assert 'pointer_click' in out['error']['message']


def test_wait_process_blocks_until_exit_and_reads_the_tail(tmp_path):
    log = tmp_path / 'job.log'
    code = (f"import subprocess\n"
            f"p = subprocess.Popen(['sh', '-c', 'echo one; echo two; exit 3'], "
            f"stdout=open({str(log)!r}, 'w'))\n"
            f"log(wait_process(p, timeout=10, tail={str(log)!r}))")
    out = run(code)
    assert out['all_ok'], out
    assert '"returncode": 3' in out['output'][0]
    assert '"tail": ["one", "two"]' in out['output'][0]


def test_wait_process_times_out_without_killing(tmp_path):
    p = subprocess.Popen(['sleep', '5'])
    try:
        out = run(f"log(wait_process({p.pid}, timeout=0.3))")
        assert '"exited": false' in out['output'][0], out
        assert p.poll() is None
    finally:
        p.kill()
        p.wait()


def test_deadline_left_is_a_number():
    out = run("log(deadline_left() > 0)")
    assert out['output'] == ['true']
