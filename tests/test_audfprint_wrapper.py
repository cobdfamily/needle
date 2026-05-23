"""NEEDLE3: tests for the bin/audfprint shell wrapper.

The wrapper enforces a path-safety guard so a malicious or
mistyped ``id`` / ``category`` field on /admin/* or
/<cat>/timestamps can never resolve to a dbase path outside
/data/. audfprint is not installed in the test environment;
we verify exit codes + stderr instead of actual audfprint
output.

Exit codes the wrapper uses:
  2 -- guard refused the dbase path (out-of-tree or contains ..)
  127 -- audfprint binary not found at the hardcoded path
         (expected on a host without the container's venv;
         tests below tolerate it because the wrapper still
         had to pass the guard to reach exec).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


WRAPPER = Path(__file__).resolve().parent.parent / "bin" / "audfprint"


def _run(*args):
    """Run the wrapper with the given args; return (rc, stderr_text)."""
    p = subprocess.run(
        [str(WRAPPER), *args],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return p.returncode, p.stderr


def test_wrapper_refuses_dbase_outside_data():
    """The dbase path is the value of -d / --dbase. The guard
    requires it to live under /data/ and end in .pklz; anything
    else exits 2 with a refusal line on stderr."""
    rc, err = _run("match", "-d", "/etc/passwd.pklz", "/tmp/q.wav")
    assert rc == 2
    assert "must be inside /data/" in err


def test_wrapper_refuses_dbase_without_pklz_suffix():
    """The .pklz suffix is part of the guard so a typo doesn't
    write to or read from an unintended file. Same exit code."""
    rc, err = _run("match", "-d", "/data/films/library", "/tmp/q.wav")
    assert rc == 2
    assert "must be inside /data/" in err


def test_wrapper_refuses_traversal_segment():
    """An id like `../../../etc/passwd` would substitute into
    the dbase path and resolve outside /data/ once open(2)
    canonicalises it. The wrapper rejects any path with a
    `..` segment before audfprint sees it."""
    rc, err = _run(
        "match", "-d", "/data/films/../../etc/passwd.pklz", "/tmp/q.wav",
    )
    assert rc == 2
    assert ".." in err


def test_wrapper_accepts_well_formed_data_path():
    """A path under /data/ ending in .pklz must pass the guard.
    The wrapper then tries to exec the audfprint binary at
    /app/.venv/bin/audfprint; on a host without the container's
    venv that binary doesn't exist and we get rc=127. The point
    is that we get past the guard (rc != 2)."""
    rc, _err = _run("match", "-d", "/data/films/library.pklz", "/tmp/q.wav")
    assert rc != 2, "well-formed dbase must not trip the guard"


def test_wrapper_accepts_fine_subpath():
    """The /timestamps endpoints substitute {id} into a /fine/
    subpath. That shape must pass the guard."""
    rc, _err = _run(
        "match", "-d", "/data/films/fine/tt0123456.pklz", "/tmp/q.wav",
    )
    assert rc != 2
