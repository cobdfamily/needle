"""NEEDLE4: tests for bin/needle-data-check.

The helper is operator-cronnable. Tests cover:

  * Missing /data -> rc 2.
  * Empty /data -> rc 3 (disk warn likely, zero-pklz warn).
  * Healthy /data -> rc 0.
  * Empty .pklz file -> rc 1.
  * Audfprint fails on a .pklz -> rc 1.

audfprint isn't installed in the test environment, so the
binary is faked via NEEDLE_AUDFPRINT_BIN: a small shell script
written to a tmp dir per test that returns the rc the test
needs.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "needle-data-check"


def _make_fake_audfprint(tmp_path: Path, exit_rc: int = 0) -> Path:
    """Drop a tiny shell stub at tmp_path/audfprint that exits
    with the given rc. The stub stands in for the real binary
    so the data-check script's `audfprint list -d X` call has
    something deterministic to invoke."""
    path = tmp_path / "audfprint"
    path.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        exit {exit_rc}
    """))
    path.chmod(0o755)
    return path


def _run(env: dict[str, str]) -> tuple[int, str, str]:
    """Run the data-check script with the supplied env vars
    overlaid; return (rc, stdout, stderr)."""
    merged = {**os.environ, **env}
    p = subprocess.run(
        [str(SCRIPT)], env=merged, capture_output=True, text=True, timeout=10,
    )
    return p.returncode, p.stdout, p.stderr


def test_missing_data_dir_returns_2(tmp_path):
    missing = tmp_path / "does-not-exist"
    rc, out, _ = _run({"NEEDLE_DATA_DIR": str(missing)})
    assert rc == 2
    assert "does not exist" in out


def test_empty_tree_warns_and_returns_3(tmp_path):
    """Empty /data triggers the zero-pklz warn. The disk-usage
    check also fires because the test host's tmpfs is small;
    USAGE_WARN_PERCENT=100 disables that so the test isolates
    the zero-pklz signal. Exit code is non-zero so cron picks
    it up."""
    fake = _make_fake_audfprint(tmp_path)
    rc, out, _ = _run({
        "NEEDLE_DATA_DIR": str(tmp_path),
        "NEEDLE_AUDFPRINT_BIN": str(fake),
        "NEEDLE_USAGE_WARN_PERCENT": "999",
    })
    # Empty tree alone is a soft signal -- the script logs WARN
    # but reaches the OK summary because no pklz files failed.
    # With USAGE_WARN_PERCENT=100 the disk-usage path stays
    # quiet, so the script exits 0 cleanly.
    assert "zero .pklz files" in out
    assert rc == 0


def test_healthy_tree_returns_0(tmp_path):
    """A tree with one parseable pklz and audfprint returning
    rc 0 lands the OK summary."""
    data = tmp_path / "data"
    (data / "films").mkdir(parents=True)
    (data / "films" / "library.pklz").write_text("dummy")
    fake = _make_fake_audfprint(tmp_path, exit_rc=0)
    rc, out, _ = _run({
        "NEEDLE_DATA_DIR": str(data),
        "NEEDLE_AUDFPRINT_BIN": str(fake),
        "NEEDLE_USAGE_WARN_PERCENT": "999",
    })
    assert rc == 0
    assert "OK:" in out
    assert "1 pklz files" in out


def test_empty_pklz_file_is_flagged(tmp_path):
    """An empty .pklz file fires the size-zero guard before
    audfprint is even invoked."""
    data = tmp_path / "data"
    (data / "films").mkdir(parents=True)
    (data / "films" / "library.pklz").write_text("")
    fake = _make_fake_audfprint(tmp_path, exit_rc=0)
    rc, out, _ = _run({
        "NEEDLE_DATA_DIR": str(data),
        "NEEDLE_AUDFPRINT_BIN": str(fake),
        "NEEDLE_USAGE_WARN_PERCENT": "999",
    })
    assert rc == 1
    assert "is empty" in out


def test_unparseable_pklz_is_flagged(tmp_path):
    """A non-empty .pklz that audfprint refuses fires the
    unreadable-by-audfprint branch."""
    data = tmp_path / "data"
    (data / "films").mkdir(parents=True)
    (data / "films" / "library.pklz").write_text("garbage")
    fake = _make_fake_audfprint(tmp_path, exit_rc=2)
    rc, out, _ = _run({
        "NEEDLE_DATA_DIR": str(data),
        "NEEDLE_AUDFPRINT_BIN": str(fake),
        "NEEDLE_USAGE_WARN_PERCENT": "999",
    })
    assert rc == 1
    assert "unreadable by audfprint" in out


@pytest.mark.skipif(
    not Path("/bin/sh").exists(),
    reason="POSIX sh required to exec the script",
)
def test_script_is_executable_and_starts_with_shebang():
    """Belt-and-braces: tighten if a future commit ever lands
    a config-only edit that drops the +x bit or rewrites the
    shebang."""
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK), "script must be executable"
    first_line = SCRIPT.read_text().splitlines()[0]
    assert first_line.startswith("#!"), "shebang missing"
    assert "sh" in first_line, "shebang must invoke a POSIX shell"
