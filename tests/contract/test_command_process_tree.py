"""Real local subprocesses prove helper deadlines also stop descendants."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from wayvia.adapters.base import AdapterError, CommandRunner


def running(pid):
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    state = result.stdout.strip()
    # A killed orphan may briefly await the operating system's reaper. It cannot
    # execute or retain sockets; unlike S/R states it is not a surviving helper.
    return bool(state) and not state.startswith("Z")


@pytest.mark.parametrize("parent_exits", [False, True])
def test_deadline_kills_child_and_grandchild_even_if_parent_exited(tmp_path, parent_exits):
    pids_path = tmp_path / "pids.json"
    child = "\n".join(
        [
            "import json, os, subprocess, sys, time",
            "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
            "with open(sys.argv[1], 'w') as out:",
            "    json.dump([os.getppid(), os.getpid(), grandchild.pid], out)",
            "time.sleep(60)",
        ]
    )
    parent = "\n".join(
        [
            "import pathlib, subprocess, sys, time",
            f"subprocess.Popen([sys.executable, '-c', {child!r}, sys.argv[1]])",
            "while not pathlib.Path(sys.argv[1]).exists(): time.sleep(0.01)",
            "pass" if parent_exits else "time.sleep(60)",
        ]
    )
    try:
        started = time.monotonic()
        with pytest.raises(AdapterError) as raised:
            CommandRunner().run(
                [sys.executable, "-c", parent, str(pids_path)], timeout=1.5, process_tree=True
            )
        assert raised.value.code == "COMMAND_TIMEOUT"
        assert time.monotonic() - started < 5
        pids = json.loads(pids_path.read_text())
        assert len(pids) == 3
        until = time.monotonic() + 2
        while any(running(pid) for pid in pids) and time.monotonic() < until:
            time.sleep(0.02)
        assert not any(running(pid) for pid in pids)
    finally:
        # Keep the test disposable even if a regression leaves children alive.
        if pids_path.exists():
            for pid in json.loads(pids_path.read_text()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_process_tree_preserves_input_output_and_exit_semantics():
    runner = CommandRunner()
    result = runner.run(
        [
            sys.executable,
            "-c",
            "import sys; print(sys.stdin.read()); "
            "print('diagnostic', file=sys.stderr); sys.exit(7)",
        ],
        input_text="probe input",
        check=False,
        process_tree=True,
    )
    assert result.returncode == 7
    assert result.stdout == "probe input\n"
    assert result.stderr == "diagnostic\n"
    with pytest.raises(AdapterError) as raised:
        runner.run([sys.executable, "-c", "raise SystemExit(7)"], process_tree=True)
    assert raised.value.code == "COMMAND_FAILED"
