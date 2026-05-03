"""End-to-end tests for needle.

Assumes the docker-compose stack at the repo root is up and
reachable at http://localhost:8000. The CI workflow builds the
image locally and brings the stack up before invoking pytest;
locally, ``docker compose up -d`` is enough.

The flow:

  1. Generate a deterministic noise WAV in tmp_path.
  2. POST it to /admin/fine/build to populate
     /data/films/fine/<id>.pklz.
  3. POST it to /admin/library/add to extend
     /data/films/library.pklz.
  4. POST /admin/library/list — assert the entry is listed.
  5. POST /films/identify with the same audio — assert the
     match comes back with our id.
  6. POST /films/timestamps with the same audio + id —
     assert at least one matched time range.

Round-tripping the same audio through fingerprint -> match
is a strict but realistic exercise. audfprint produces
identical fingerprints from identical input, so a high-
score self-match is the expected baseline.
"""

from __future__ import annotations

import os
import random
import struct
import wave
from pathlib import Path

import pytest
import requests

NEEDLE_BASE_URL = os.environ.get("NEEDLE_BASE_URL", "http://localhost:8000")

# Stable seed → deterministic WAV → reproducible fingerprints
# across runs. Anything pre-recorded would also work; this just
# avoids committing audio bytes to the repo.
NOISE_SEED = 42
NOISE_SECONDS = 8
NOISE_RATE = 22050


def _make_noise_wav(path: Path) -> None:
    """Write `NOISE_SECONDS` of seeded white noise to `path` as a
    16-bit mono WAV. Deterministic — same seed produces the same
    bytes, so audfprint sees identical fingerprints across
    add / match calls."""
    rng = random.Random(NOISE_SEED)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(NOISE_RATE)
        # Write in 1k-sample chunks to keep peak memory low.
        chunk_size = 1024
        for _ in range(0, NOISE_SECONDS * NOISE_RATE, chunk_size):
            samples = bytes()
            for _i in range(chunk_size):
                samples += struct.pack("<h", rng.randint(-32767, 32767))
            w.writeframes(samples)


@pytest.fixture(scope="module")
def query_audio(tmp_path_factory) -> Path:
    """Per-test-module noise WAV. Reused across the seed → match
    flow so the fingerprints align."""
    path = tmp_path_factory.mktemp("audio") / "query.wav"
    _make_noise_wav(path)
    return path


@pytest.fixture(scope="module")
def film_id() -> str:
    return "tt0000001"


# ---------------------------------------------------------------------------
# liveness — the / endpoint needle inherits from url2code
# ---------------------------------------------------------------------------


def test_liveness_returns_needle_service():
    """``/`` reports ``service: needle`` (not ``url2code``).
    The api.title -> service field wiring landed in
    url2code 1.0.6; this test pins the inheritance."""
    r = requests.get(NEEDLE_BASE_URL + "/", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "needle"
    assert body["status"] == "ok"
    assert body["version"]


# ---------------------------------------------------------------------------
# /admin/categories — discovery
# ---------------------------------------------------------------------------


def test_admin_categories_returns_curated_catalog():
    """GET /admin/categories returns the catalog as
    parsed_output: a list of {slug, name, description,
    library_density, fine_density, min_count} entries.
    Without it consumers can't discover valid category
    slugs without reading the docs."""
    r = requests.get(NEEDLE_BASE_URL + "/admin/categories", timeout=5)
    assert r.status_code == 200, r.text
    body = r.json()
    catalog = body.get("parsed_output")
    assert isinstance(catalog, list)
    assert len(catalog) >= 1
    for entry in catalog:
        assert {
            "slug", "name", "description",
            "library_density", "fine_density", "min_count",
        } <= set(entry.keys())
    # The slug used by the rest of these tests must be in
    # the catalog (films is the seed entry; if a future
    # refactor renames it, both this test and the rest of
    # the suite need updating in lockstep).
    slugs = [e["slug"] for e in catalog]
    assert "films" in slugs


# ---------------------------------------------------------------------------
# admin pipeline — populate the dbase tree from scratch
# ---------------------------------------------------------------------------


def test_admin_fine_build_creates_per_file_pklz(query_audio, film_id):
    """``/admin/fine/build`` runs ``audfprint new`` against the
    upload, creating ``/data/films/fine/<id>.pklz``. audfprint
    logs through Python's logging module (stderr), so the YAML
    uses ``mode: text`` and stdout is empty on success — we
    rely on the exit code for success and verify the side
    effect via /admin/library/list and /films/identify
    downstream."""
    with open(query_audio, "rb") as f:
        r = requests.post(
            NEEDLE_BASE_URL + "/admin/fine/build",
            data={"category": "films", "id": film_id},
            files={"audio": ("query.wav", f, "audio/wav")},
            timeout=60,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("exit_code") == 0


def test_admin_library_add_extends_library(query_audio, film_id):
    """``/admin/library/add`` runs ``audfprint add`` against the
    coarse library. Track name in the .pklz comes from the
    upload's saved filename — name_template puts ``<id>.<ext>``
    on disk, so the entry name matches the canonical id.

    Same stdout-empty caveat as fine/build — we trust the exit
    code here and verify the side effect via /admin/library/list."""
    with open(query_audio, "rb") as f:
        r = requests.post(
            NEEDLE_BASE_URL + "/admin/library/add",
            data={"category": "films", "id": film_id},
            files={"audio": ("query.wav", f, "audio/wav")},
            timeout=60,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("exit_code") == 0


def test_admin_library_list_includes_seeded_entry(film_id):
    """``/admin/library/list`` runs ``audfprint list`` and
    returns one row per indexed track. The id we just added
    should show up — needle saves uploads as ``<id>.<ext>``,
    so the list entry's path ends with our id.

    /admin/library/list takes no audio upload, so request is a
    plain JSON body rather than multipart."""
    r = requests.post(
        NEEDLE_BASE_URL + "/admin/library/list",
        json={"category": "films"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    parsed = body.get("parsed_output") or []
    tracks = [row.get("track", "") for row in parsed]
    assert any(film_id in t for t in tracks), \
        f"id {film_id!r} missing from tracks: {tracks}"


# ---------------------------------------------------------------------------
# match pipeline — round-trip the same audio through identify
# ---------------------------------------------------------------------------


def test_films_identify_finds_seeded_entry(query_audio, film_id):
    """Re-uploading the same audio that seeded the library
    should match (audfprint is deterministic on input). The
    matched filename echoes our canonical id — that's the
    name_template + library.pklz round-trip working."""
    with open(query_audio, "rb") as f:
        r = requests.post(
            NEEDLE_BASE_URL + "/films/identify",
            files={"audio": ("query.wav", f, "audio/wav")},
            timeout=60,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    parsed = body.get("parsed_output") or {}
    # When matched, ``no_match`` is None and ``matched`` is set.
    assert parsed.get("no_match") is None, \
        f"unexpected NOMATCH for self-match: {parsed}"
    assert film_id in (parsed.get("matched") or "")


def test_films_timestamps_returns_aligned_range(query_audio, film_id):
    """For the same audio against the per-file fine database,
    ``/films/timestamps`` returns one (or more) aligned ranges.
    A self-match should produce at least one row with matching
    duration close to NOISE_SECONDS."""
    with open(query_audio, "rb") as f:
        r = requests.post(
            NEEDLE_BASE_URL + "/films/timestamps",
            data={"id": film_id},
            files={"audio": ("query.wav", f, "audio/wav")},
            timeout=60,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    parsed = body.get("parsed_output") or []
    assert len(parsed) >= 1, f"expected >=1 timestamp row, got {parsed}"
    row = parsed[0]
    # Duration is in seconds as a float; should be a sizable
    # chunk of the NOISE_SECONDS source.
    duration = float(row["duration"])
    assert duration > 1.0, f"duration too small for self-match: {duration}"
