"""Keep daemon tests away from the real runtime dir, for every test module that needs it.

test_daemon.py and test_server.py each call setUpModule and tearDownModule from
their own module hooks. Not named test*.py, so unittest never collects it alone.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_review_pr_dashboard.daemon import get_socket_path

ISOLATED_TEMP_DIR = ""
_previous_tempdir = None
_previous_tmpdir = None


def setUpModule() -> None:
    """Pin every tempdir in this module away from the real /tmp runtime dir.

    start_daemon sweeps stale .lock/.sock pairs out of whatever gettempdir()
    points at: unisolated, a test that forces liveness False would delete the
    files of a daemon the user is really running elsewhere. TMPDIR is set too
    because test_daemon_survives_the_process_that_launched_it runs start_daemon
    in a child python that resolves gettempdir() from the environment.
    """
    global ISOLATED_TEMP_DIR, _previous_tempdir, _previous_tmpdir
    # Short prefix: AF_UNIX socket paths are capped at 108 bytes.
    ISOLATED_TEMP_DIR = tempfile.mkdtemp(prefix="arpd.")
    _previous_tempdir = tempfile.tempdir
    _previous_tmpdir = os.environ.get("TMPDIR")
    tempfile.tempdir = ISOLATED_TEMP_DIR
    os.environ["TMPDIR"] = ISOLATED_TEMP_DIR


def tearDownModule() -> None:
    global ISOLATED_TEMP_DIR
    tempfile.tempdir = _previous_tempdir
    if _previous_tmpdir is None:
        os.environ.pop("TMPDIR", None)
    else:
        os.environ["TMPDIR"] = _previous_tmpdir
    shutil.rmtree(ISOLATED_TEMP_DIR, ignore_errors=True)
    ISOLATED_TEMP_DIR = ""


class RuntimeDirIsolationTestCase(unittest.TestCase):
    def test_socket_paths_resolve_inside_the_isolated_temp_dir(self):
        runtime_dir = get_socket_path(Path.cwd()).parent
        self.assertEqual(
            runtime_dir,
            Path(ISOLATED_TEMP_DIR) / f"auto-review-pr-dashboard-{os.getuid()}",
        )


def kill_agent(process: subprocess.Popen) -> None:
    """Take a test's stand-in agent down whether or not the test killed it first."""
    with suppress(ProcessLookupError):
        process.kill()
    process.wait()
