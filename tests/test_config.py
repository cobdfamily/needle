"""Static checks on config/tools.yaml.

needle has no Python source of its own — the HTTP surface
is entirely declared in config/tools.yaml and consumed by
url2code at runtime. These tests pin the YAML shape so a
careless edit can't ship a malformed config that only
surfaces at container-start.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "config" / "tools.yaml"

CATEGORIES = ["films", "tvshows", "youtube", "shorts", "reels"]
SHORT_CATEGORIES = {"shorts", "reels"}
LONG_CATEGORIES = {"films", "tvshows", "youtube"}


@pytest.fixture(scope="module")
def cfg():
    return yaml.safe_load(CONFIG.read_text())


@pytest.fixture(scope="module")
def endpoints(cfg):
    return cfg["endpoints"]


@pytest.fixture(scope="module")
def match_endpoints(endpoints):
    """Just the audfprint-match endpoints — the per-category
    /identify and /timestamps. Excludes admin endpoints (which
    use add / new / list and have a different shape)."""
    return [e for e in endpoints if not e["name"].startswith("admin-")]


# ---------------------------------------------------------------------------
# top-level shape
# ---------------------------------------------------------------------------


def test_yaml_parses(cfg):
    """Sentinel — if any commit lands a syntactically broken
    YAML, this test fires before the image ever builds."""
    assert isinstance(cfg, dict)
    assert "endpoints" in cfg
    assert isinstance(cfg["endpoints"], list)


def test_top_level_metadata(cfg):
    assert cfg["api"]["title"] == "needle"
    assert cfg["api"]["default_root"] == "/"
    assert cfg["logging"]["level"] in {"DEBUG", "INFO", "WARNING", "ERROR"}


def test_every_category_has_two_endpoints(endpoints):
    """Each declared category must expose both /identify and
    /timestamps. Missing one is a half-shipped category."""
    by_route = {e["route"]: e for e in endpoints}
    for cat in CATEGORIES:
        assert f"/{cat}/identify" in by_route, f"{cat} missing /identify"
        assert f"/{cat}/timestamps" in by_route, f"{cat} missing /timestamps"


def test_routes_are_unique(endpoints):
    """url2code's loader rejects duplicate (method, route)
    pairs at load time; this test surfaces it earlier."""
    pairs = [(e.get("method", "GET"), e["route"]) for e in endpoints]
    assert len(pairs) == len(set(pairs)), f"duplicate routes: {pairs}"


def test_endpoint_names_are_unique(endpoints):
    names = [e["name"] for e in endpoints]
    assert len(names) == len(set(names)), f"duplicate endpoint names: {names}"


# ---------------------------------------------------------------------------
# command shape
# ---------------------------------------------------------------------------


def test_every_endpoint_calls_audfprint(endpoints):
    """Belt-and-braces: every endpoint shells out to audfprint
    (match for the per-category endpoints, add / new / list for
    admin). If a future edit accidentally points an endpoint at a
    different binary, catch it here."""
    valid_subcommands = {"match", "add", "new", "list"}
    for e in endpoints:
        assert e["command"]["executable"] == "/app/.venv/bin/audfprint"
        assert e["command"]["args"][0] in valid_subcommands, \
            f"{e['name']} uses unexpected subcommand"


def test_match_endpoints_use_match_subcommand(match_endpoints):
    for e in match_endpoints:
        assert e["command"]["args"][0] == "match"


def test_every_endpoint_has_dbase_arg(endpoints):
    """audfprint match requires a -d / --dbase pointer; if
    we drop it the command exits non-zero with a usage
    error and url2code returns 502."""
    for e in endpoints:
        args = e["command"]["args"]
        assert "-d" in args, f"{e['name']} missing -d"
        d_idx = args.index("-d")
        dbase = args[d_idx + 1]
        assert dbase.startswith("/data/"), f"{e['name']} dbase outside /data/"
        assert dbase.endswith(".pklz"), f"{e['name']} dbase not a .pklz"


def test_identify_endpoints_use_library_pklz(endpoints):
    """The /identify endpoints look up against the coarse
    multi-file library; the /timestamps endpoints look up
    against the per-file fine database."""
    for e in endpoints:
        if e["route"].endswith("/identify"):
            args = e["command"]["args"]
            d_idx = args.index("-d")
            assert args[d_idx + 1].endswith("/library.pklz"), \
                f"{e['name']} identify must point at library.pklz"


def test_timestamps_endpoints_template_id(endpoints):
    """The /timestamps endpoints take a request-validated
    `id` and substitute it into the database path."""
    for e in endpoints:
        if e["route"].endswith("/timestamps"):
            args = e["command"]["args"]
            d_idx = args.index("-d")
            assert "{id}" in args[d_idx + 1], \
                f"{e['name']} timestamps must template {{id}}"
            assert "/fine/" in args[d_idx + 1], \
                f"{e['name']} timestamps must use the fine/ tier"
            assert "id" in e["request"]["validations"], \
                f"{e['name']} must validate `id` request field"


def test_audio_upload_field_is_consistent(endpoints):
    """Every endpoint that takes an audio upload uses the
    multipart field `audio`; clients shouldn't have to remember
    a different name per endpoint. ``admin-library-list``
    intentionally has no upload (it's a read of the dbase)."""
    for e in endpoints:
        uploads = e.get("uploads") or []
        if not uploads:
            assert e["name"] == "admin-library-list", \
                f"{e['name']} has no upload but isn't list"
            continue
        assert len(uploads) == 1, f"{e['name']} has != 1 upload"
        assert uploads[0]["field_name"] == "audio"
        assert uploads[0]["placeholder"] == "audio"


def test_audio_placeholder_is_substituted(endpoints):
    """Every command that takes an audio file substitutes the
    {audio} placeholder. ``admin-library-list`` doesn't take one."""
    for e in endpoints:
        if e["name"] == "admin-library-list":
            continue
        args = e["command"]["args"]
        assert "{audio}" in args, f"{e['name']} missing {{audio}} arg"


# ---------------------------------------------------------------------------
# tuning per category
# ---------------------------------------------------------------------------


def test_short_categories_lower_min_count(match_endpoints):
    """shorts + reels are ~60s reference clips — a 5-10s
    query needs a lower -N / min-count threshold to trigger
    a hit. Long-content categories don't set -N (audfprint
    default = 5). Admin endpoints take ``category`` as a
    request field rather than route prefix and don't differ
    on -N (they don't run match)."""
    for e in match_endpoints:
        category = e["route"].strip("/").split("/")[0]
        args = e["command"]["args"]
        if category in SHORT_CATEGORIES:
            assert "-N" in args, f"{e['name']} (short) missing -N"
            n_idx = args.index("-N")
            assert int(args[n_idx + 1]) <= 5, \
                f"{e['name']} -N must be <= audfprint default"
        else:
            assert category in LONG_CATEGORIES
            assert "-N" not in args, \
                f"{e['name']} (long) shouldn't override -N"


def test_identify_returns_one_match(endpoints):
    """`/identify` is a "which file is this" call — one
    answer is what the caller wants. -x 1 keeps audfprint
    from spending CPU on alternatives."""
    for e in endpoints:
        if e["route"].endswith("/identify"):
            args = e["command"]["args"]
            x_idx = args.index("-x")
            assert args[x_idx + 1] == "1", \
                f"{e['name']} identify must use -x 1"


def test_timestamps_returns_multiple(endpoints):
    """`/timestamps` searches one file for repeats / multiple
    aligned regions of the query. -x 5 caps without
    truncating most realistic cases."""
    for e in endpoints:
        if e["route"].endswith("/timestamps"):
            args = e["command"]["args"]
            x_idx = args.index("-x")
            assert int(args[x_idx + 1]) >= 5, \
                f"{e['name']} timestamps must allow >=5 results"


def test_match_uses_R_and_v_for_parseable_output(match_endpoints):
    """audfprint match's stdout format the regex below
    parses comes from -R (find time range) + -v (verbose).
    Without both, the regex won't match a single line.
    (Admin endpoints don't run match.)"""
    for e in match_endpoints:
        args = e["command"]["args"]
        assert "-R" in args, f"{e['name']} missing -R (find time range)"
        assert "-v" in args, f"{e['name']} missing -v (verbose)"


# ---------------------------------------------------------------------------
# regex parser
# ---------------------------------------------------------------------------


SAMPLE_MATCHED = (
    "Matched   12.3 s starting at    1.5 s in /tmp/q.wav "
    "to time   13.7 s in /sources/films/inception.m4a "
    "with    42 of    60 common hashes at rank  0"
)
SAMPLE_NOMATCH = "NOMATCH: /tmp/q.wav"


def test_identify_regex_matches_match_line(endpoints):
    """Regression-grade fixture: the actual audfprint
    output format should match. If audfprint upstream ever
    rephrases the line, this test catches it before the
    image ships."""
    for e in endpoints:
        if not e["route"].endswith("/identify"):
            continue
        pattern = e["output"]["regex"]["pattern"]
        m = re.compile(pattern, re.MULTILINE).search(SAMPLE_MATCHED)
        assert m, f"{e['name']} regex didn't match the sample line"
        d = m.groupdict()
        assert d["matched"] == "/sources/films/inception.m4a"
        assert d["query_start"] == "1.5"
        assert d["query_end"] == "13.7"
        assert d["hashes_aligned"] == "42"
        assert d["hashes_total"] == "60"


def test_identify_regex_handles_nomatch(endpoints):
    """A NOMATCH line should populate `no_match` (and only
    that group). The /identify regex has both alternatives;
    /timestamps does not, since "no match" against a fine
    DB is just an empty result list."""
    for e in endpoints:
        if not e["route"].endswith("/identify"):
            continue
        pattern = e["output"]["regex"]["pattern"]
        m = re.compile(pattern, re.MULTILINE).search(SAMPLE_NOMATCH)
        assert m, f"{e['name']} regex didn't match a NOMATCH line"
        d = m.groupdict()
        assert d["no_match"] is not None
        assert d["matched"] is None


def test_timestamps_regex_collects_multiple(endpoints):
    """/timestamps uses ``multiple: true`` — a query that
    aligns at three separate places in the reference file
    should yield three dicts."""
    multi = "\n".join([SAMPLE_MATCHED] * 3)
    for e in endpoints:
        if not e["route"].endswith("/timestamps"):
            continue
        pattern = e["output"]["regex"]["pattern"]
        assert e["output"]["regex"]["multiple"] is True
        matches = list(re.compile(pattern, re.MULTILINE).finditer(multi))
        assert len(matches) == 3, \
            f"{e['name']} regex collected {len(matches)} hits, expected 3"


# ---------------------------------------------------------------------------
# admin endpoints — write paths into the database tree
# ---------------------------------------------------------------------------


ADMIN_ROUTES = {
    "admin-library-add": "/admin/library/add",
    "admin-fine-build":  "/admin/fine/build",
    "admin-library-list": "/admin/library/list",
}


def test_admin_endpoints_present(endpoints):
    by_name = {e["name"]: e for e in endpoints}
    for name, route in ADMIN_ROUTES.items():
        assert name in by_name, f"missing admin endpoint {name}"
        assert by_name[name]["route"] == route


def test_admin_endpoints_validate_category_as_enum(endpoints):
    """Every admin endpoint takes a ``category`` field constrained
    to the five known categories. Adding a new category means
    extending these enums in lockstep with the match endpoints."""
    for e in endpoints:
        if not e["route"].startswith("/admin/"):
            continue
        validations = e.get("request", {}).get("validations", {})
        assert "category" in validations, \
            f"{e['name']} missing category validation"
        spec = validations["category"]
        assert spec["type"] == "enum"
        assert set(spec["choices"]) == set(CATEGORIES)


def test_admin_endpoints_use_category_in_dbase_path(endpoints):
    """The category enum value substitutes into the audfprint -d
    path. Without the ``{category}`` placeholder the wrong
    database gets written."""
    for e in endpoints:
        if not e["route"].startswith("/admin/"):
            continue
        args = e["command"]["args"]
        d_idx = args.index("-d")
        dbase = args[d_idx + 1]
        assert "{category}" in dbase, \
            f"{e['name']} dbase missing {{category}} placeholder"


def test_admin_library_add_uses_audfprint_add(endpoints):
    e = next(e for e in endpoints if e["name"] == "admin-library-add")
    assert e["command"]["args"][0] == "add"
    # Defaults to density 20 (audfprint default; same as library
    # match endpoints). Operators override per-call if needed.
    assert e["defaults"]["density"] == "20"
    # ``id`` validation is required so the canonical id flows
    # through to the upload's on-disk name.
    assert "id" in e["request"]["validations"]


def test_admin_fine_build_uses_audfprint_new(endpoints):
    """``audfprint new`` creates the fine/<id>.pklz from a single
    upload; ``add`` would extend an existing one."""
    e = next(e for e in endpoints if e["name"] == "admin-fine-build")
    assert e["command"]["args"][0] == "new"
    assert "{id}" in " ".join(e["command"]["args"])
    # Higher-density default than library — sub-second timestamps
    # in /timestamps need it.
    assert int(e["defaults"]["density"]) > 20


def test_admin_uploads_use_name_template_for_canonical_id(endpoints):
    """Both write-side admin endpoints save the upload as
    ``<id>.<ext>`` rather than the default random-hex token —
    that way audfprint records the canonical id in the .pklz
    track names, instead of a hex token operators have to map
    back through a side-table.

    Pinned because the ``name_template`` -> ``{id}`` wiring is
    load-bearing for the operator's lookup story; a careless
    YAML edit removing it would silently regress to random hex
    track names."""
    for name in ("admin-library-add", "admin-fine-build"):
        e = next(e for e in endpoints if e["name"] == name)
        upload = e["uploads"][0]
        assert upload.get("name_template") == "{id}", \
            f"{name}: upload.name_template must be {{id}}"


def test_admin_library_list_is_get_safe(endpoints):
    """``list`` doesn't accept an upload (no audio body) and
    doesn't mutate state. Currently still POST because url2code's
    request parser is built around POST + multipart; the lack
    of mutation is encoded in the audfprint subcommand."""
    e = next(e for e in endpoints if e["name"] == "admin-library-list")
    assert e["command"]["args"][0] == "list"
    assert "uploads" not in e or not e.get("uploads"), \
        "list should not require an audio upload"


def test_admin_density_is_optional(endpoints):
    """Density is request-overridable via the ``density`` field
    but ships with a sensible default per endpoint, so callers
    that don't tune don't have to know."""
    for name in ("admin-library-add", "admin-fine-build"):
        e = next(e for e in endpoints if e["name"] == name)
        assert "density" in e["defaults"]
        assert e["request"]["validations"]["density"]["type"] == "number"


SAMPLE_SAVED = "Saved fprints for 1 files to /data/films/library.pklz"


def test_admin_save_regex_captures_dbase_and_count(endpoints):
    """The ``Saved fprints for N files to <pklz>`` line is what
    audfprint emits on a successful add/new. Pin the regex
    against the literal string."""
    for name in ("admin-library-add", "admin-fine-build"):
        e = next(e for e in endpoints if e["name"] == name)
        pattern = e["output"]["regex"]["pattern"]
        m = re.compile(pattern, re.MULTILINE).search(SAMPLE_SAVED)
        assert m, f"{name} regex didn't match the sample"
        d = m.groupdict()
        assert d["saved"] == "1"
        assert d["dbase"] == "/data/films/library.pklz"


SAMPLE_LIST_OUTPUT = """
   /sources/films/inception.m4a
   /sources/films/parasite.m4a
   /sources/films/the-matrix.m4a
""".strip()


def test_admin_list_regex_yields_one_per_line(endpoints):
    e = next(e for e in endpoints if e["name"] == "admin-library-list")
    pattern = e["output"]["regex"]["pattern"]
    matches = list(re.compile(pattern, re.MULTILINE).finditer(SAMPLE_LIST_OUTPUT))
    assert len(matches) == 3
    assert [m["track"] for m in matches] == [
        "/sources/films/inception.m4a",
        "/sources/films/parasite.m4a",
        "/sources/films/the-matrix.m4a",
    ]
