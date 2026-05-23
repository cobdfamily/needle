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
CATEGORIES_YAML = REPO_ROOT / "config" / "categories.yaml"


def _load_catalog():
    return yaml.safe_load(CATEGORIES_YAML.read_text())


# Categories are sourced from config/categories.yaml (the
# canonical catalog). The lists below are derived at import
# time so adding a category to the YAML automatically
# propagates here -- the tools.yaml enum + per-category
# match endpoints are then verified against the catalog
# by tests below, catching drift before it ships.
CATEGORIES = [c["slug"] for c in _load_catalog()]
SHORT_CATEGORIES = {
    c["slug"] for c in _load_catalog() if c["min_count"] is not None
}
LONG_CATEGORIES = {
    c["slug"] for c in _load_catalog() if c["min_count"] is None
}


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
    # The API surface lives under /v1/. Liveness ``/`` and
    # FastAPI's ``/docs`` stay at the root regardless.
    assert cfg["api"]["default_root"] == "/v1"
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
# per-endpoint timeouts (NEEDLE2)
# ---------------------------------------------------------------------------

# Sensible bounds per endpoint shape. url2code defaults to 30s
# when ``command.timeout_seconds`` is missing, which is too tight
# for /admin/fine/build on a full-length film and too loose for
# the catalog-read /admin/categories. Every endpoint now pins an
# explicit value; this test holds the bounds.
EXPECTED_TIMEOUT_BOUNDS = {
    # /identify: coarse-library match, short clip in / short
    # answer out. 30s is roomy.
    "identify": (20, 60),
    # /timestamps: per-file fine match, up to -x 5 hits;
    # measurably slower than identify.
    "timestamps": (45, 120),
    # admin: rebuilds / writes the dbase. Wide bounds because
    # the right value depends on library size and audio length.
    "admin-library-add":  (60, 240),
    "admin-fine-build":   (180, 600),
    "admin-library-list": (5, 30),
    "admin-categories":   (1, 15),
}


def _endpoint_shape(endpoint: dict) -> str:
    """Bucket an endpoint into one of the EXPECTED_TIMEOUT_BOUNDS
    keys. Per-category /<cat>/identify and /<cat>/timestamps
    share bounds across categories."""
    name = endpoint["name"]
    if name.endswith("-identify"):
        return "identify"
    if name.endswith("-timestamps"):
        return "timestamps"
    return name  # admin endpoints are keyed by full name


def test_every_endpoint_pins_a_timeout(endpoints):
    """The default command.timeout_seconds (30s) is fine for
    /identify but wrong for the long-running admin writes and
    the short catalog read. Every endpoint must set an explicit
    value so the choice is auditable in the YAML."""
    for e in endpoints:
        assert "timeout_seconds" in e["command"], \
            f"{e['name']} must set command.timeout_seconds"


def test_timeouts_are_within_sensible_bounds(endpoints):
    """The pinned values must fall inside the bounds for the
    endpoint shape (above). Tightening a timeout below the
    floor risks falsely 504-ing legit calls; loosening above
    the ceiling lets a wedged worker hold a slot too long."""
    for e in endpoints:
        shape = _endpoint_shape(e)
        if shape not in EXPECTED_TIMEOUT_BOUNDS:
            raise AssertionError(
                f"{e['name']}: no timeout bounds known for shape {shape!r}"
            )
        lo, hi = EXPECTED_TIMEOUT_BOUNDS[shape]
        t = e["command"]["timeout_seconds"]
        assert lo <= t <= hi, \
            f"{e['name']}: timeout {t}s outside {lo}-{hi}s bounds"


# ---------------------------------------------------------------------------
# command shape
# ---------------------------------------------------------------------------


def test_every_endpoint_calls_audfprint(endpoints):
    """Belt-and-braces: every audfprint-running endpoint shells
    out to /app/bin/audfprint (match for the per-category
    endpoints, add / new / list for admin). admin-categories is
    the one exception -- it `cat`s the catalog JSON and never
    touches audfprint."""
    valid_subcommands = {"match", "add", "new", "list"}
    for e in endpoints:
        if e["name"] == "admin-categories":
            continue
        assert e["command"]["executable"] == "/app/bin/audfprint"
        assert e["command"]["args"][0] in valid_subcommands, \
            f"{e['name']} uses unexpected subcommand"


def test_match_endpoints_use_match_subcommand(match_endpoints):
    for e in match_endpoints:
        assert e["command"]["args"][0] == "match"


def test_every_endpoint_has_dbase_arg(endpoints):
    """audfprint match / add / new / list all require a -d /
    --dbase pointer; if we drop it the command exits non-zero
    with a usage error and url2code returns 502.
    admin-categories doesn't run audfprint and is exempt."""
    for e in endpoints:
        if e["name"] == "admin-categories":
            continue
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


# Endpoints that intentionally don't take an audio upload:
# admin-library-list reads the dbase, admin-categories serves
# the catalog. Everything else takes audio in multipart field
# `audio`.
NO_AUDIO_ENDPOINTS = {"admin-library-list", "admin-categories"}


def test_audio_upload_field_is_consistent(endpoints):
    """Every endpoint that takes an audio upload uses the
    multipart field `audio`; clients shouldn't have to remember
    a different name per endpoint."""
    for e in endpoints:
        uploads = e.get("uploads") or []
        if not uploads:
            assert e["name"] in NO_AUDIO_ENDPOINTS, \
                f"{e['name']} has no upload but isn't in NO_AUDIO_ENDPOINTS"
            continue
        assert len(uploads) == 1, f"{e['name']} has != 1 upload"
        assert uploads[0]["field_name"] == "audio"
        assert uploads[0]["placeholder"] == "audio"


def test_audio_placeholder_is_substituted(endpoints):
    """Every command that takes an audio file substitutes the
    {audio} placeholder. The no-audio endpoints (catalog
    listing, library list) don't take one."""
    for e in endpoints:
        if e["name"] in NO_AUDIO_ENDPOINTS:
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
    "admin-library-add":  "/admin/library/add",
    "admin-fine-build":   "/admin/fine/build",
    "admin-library-list": "/admin/library/list",
    "admin-categories":   "/admin/categories",
}

# Admin endpoints that take a `category` form field. The
# /admin/categories listing endpoint doesn't (it's the
# discovery surface for the catalog itself, no form params).
ADMIN_CATEGORY_ROUTES = {
    name: route for name, route in ADMIN_ROUTES.items()
    if name != "admin-categories"
}


def test_admin_endpoints_present(endpoints):
    by_name = {e["name"]: e for e in endpoints}
    for name, route in ADMIN_ROUTES.items():
        assert name in by_name, f"missing admin endpoint {name}"
        assert by_name[name]["route"] == route


def test_admin_endpoints_validate_category_as_enum(endpoints):
    """Every category-taking admin endpoint constrains
    ``category`` to the catalog's slug list. Adding a new
    category means editing categories.yaml + extending these
    enums in lockstep with the match endpoints; this test
    catches drift."""
    for e in endpoints:
        if e["name"] not in ADMIN_CATEGORY_ROUTES:
            continue
        validations = e.get("request", {}).get("validations", {})
        assert "category" in validations, \
            f"{e['name']} missing category validation"
        spec = validations["category"]
        assert spec["type"] == "enum"
        assert set(spec["choices"]) == set(CATEGORIES), \
            f"{e['name']} enum {sorted(spec['choices'])} != catalog {sorted(CATEGORIES)}"


def test_admin_endpoints_use_category_in_dbase_path(endpoints):
    """The category enum value substitutes into the audfprint -d
    path. Without the ``{category}`` placeholder the wrong
    database gets written."""
    for e in endpoints:
        if e["name"] not in ADMIN_CATEGORY_ROUTES:
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


def test_admin_add_and_build_use_text_output_mode(endpoints):
    """``audfprint add`` and ``audfprint new`` log status
    through Python's logging module — which writes to stderr,
    not stdout — so a regex parser against stdout finds
    nothing even on success. Both endpoints use ``mode: text``
    and rely on the exit code: HTTP 200 == the .pklz now
    contains the new entry. Verify via /admin/library/list
    or /<category>/identify."""
    for name in ("admin-library-add", "admin-fine-build"):
        e = next(e for e in endpoints if e["name"] == name)
        assert e["output"]["mode"] == "text", \
            f"{name}: stdout is empty on success, must use text mode"


# audfprint list -v prints a Python list repr on a single DEBUG
# line, like:
#   DEBUG:audfprint2:['/path/a.wav (123 hashes)', '/path/b.wav (456 hashes)']
# The regex captures each '<path> (<n> hashes)' entry independently.
SAMPLE_LIST_OUTPUT = (
    "DEBUG:audfprint2:['/sources/films/inception.m4a (1234 hashes)', "
    "'/sources/films/parasite.m4a (1500 hashes)', "
    "'/sources/films/the-matrix.m4a (980 hashes)']"
)


def test_admin_list_regex_yields_one_per_track(endpoints):
    e = next(e for e in endpoints if e["name"] == "admin-library-list")
    pattern = e["output"]["regex"]["pattern"]
    matches = list(re.compile(pattern, re.MULTILINE).finditer(SAMPLE_LIST_OUTPUT))
    assert len(matches) == 3
    assert [m["track"] for m in matches] == [
        "/sources/films/inception.m4a",
        "/sources/films/parasite.m4a",
        "/sources/films/the-matrix.m4a",
    ]
    # Hash count is captured too so operators can see DB depth.
    assert [m["hashes"] for m in matches] == ["1234", "1500", "980"]


# ---------------------------------------------------------------------------
# categories.yaml -- the canonical catalog
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def catalog():
    return _load_catalog()


def test_catalog_parses(catalog):
    """categories.yaml must be a list of objects with the
    documented shape. Anything else and the wrapper / loader
    fails at request time rather than CI."""
    assert isinstance(catalog, list)
    assert len(catalog) >= 1
    required = {"slug", "name", "description",
                "library_density", "fine_density", "min_count"}
    for entry in catalog:
        assert isinstance(entry, dict)
        assert required <= set(entry.keys()), \
            f"entry {entry!r} missing keys {required - set(entry.keys())}"


def test_catalog_slugs_are_unique(catalog):
    slugs = [e["slug"] for e in catalog]
    assert len(slugs) == len(set(slugs)), \
        f"duplicate slugs: {sorted(slugs)}"


def test_catalog_slugs_are_url_safe(catalog):
    """Slugs become route prefixes (/<slug>/identify) and form
    field values. Letters, digits, hyphens; not empty; no
    leading dash."""
    pattern = re.compile(r"^[a-z0-9][a-z0-9-]*$")
    for entry in catalog:
        assert pattern.match(entry["slug"]), \
            f"slug {entry['slug']!r} is not URL-safe"


def test_catalog_densities_are_positive_ints(catalog):
    for entry in catalog:
        for key in ("library_density", "fine_density"):
            v = entry[key]
            assert isinstance(v, int) and v > 0, \
                f"{entry['slug']}: {key} = {v!r} (must be a positive int)"


def test_catalog_min_count_shape(catalog):
    """min_count is either a positive int (override of
    audfprint's default of 5) or null (use the default)."""
    for entry in catalog:
        v = entry["min_count"]
        assert v is None or (isinstance(v, int) and v > 0), \
            f"{entry['slug']}: min_count = {v!r}"


# ---------------------------------------------------------------------------
# catalog <-> YAML consistency
# ---------------------------------------------------------------------------


def test_match_endpoints_exist_for_every_catalog_slug(endpoints, catalog):
    """For every {slug} in the catalog, the YAML must declare
    /<slug>/identify and /<slug>/timestamps. Adding a slug to
    the catalog without adding the endpoints is precisely the
    drift this catalog refactor was meant to surface."""
    routes = {e["route"] for e in endpoints}
    for entry in catalog:
        slug = entry["slug"]
        assert f"/{slug}/identify" in routes, \
            f"catalog has slug {slug!r} but no /{slug}/identify endpoint"
        assert f"/{slug}/timestamps" in routes, \
            f"catalog has slug {slug!r} but no /{slug}/timestamps endpoint"


def test_min_count_override_matches_catalog(endpoints, catalog):
    """Match endpoints' -N values must match the catalog's
    min_count. Catalog says null -> no -N flag. Catalog says
    integer -> -N <integer>."""
    by_slug = {e["slug"]: e for e in catalog}
    for endpoint in endpoints:
        if endpoint["name"].startswith("admin-"):
            continue
        slug = endpoint["route"].strip("/").split("/")[0]
        if slug not in by_slug:
            continue  # lets test_match_endpoints_exist... handle drift
        expected = by_slug[slug]["min_count"]
        args = endpoint["command"]["args"]
        if expected is None:
            assert "-N" not in args, \
                f"{endpoint['name']} sets -N but catalog says min_count is null"
        else:
            assert "-N" in args, \
                f"{endpoint['name']} missing -N (catalog wants {expected})"
            n_idx = args.index("-N")
            actual = int(args[n_idx + 1])
            assert actual == expected, \
                f"{endpoint['name']} -N {actual} != catalog min_count {expected}"


def test_admin_density_defaults_match_catalog(endpoints, catalog):
    """admin-library-add's density default must match the
    catalog's library_density (homogeneous across the catalog
    today; if a future entry differs, the default would need
    to become per-category, but for now they all share)."""
    library_densities = {e["library_density"] for e in catalog}
    fine_densities = {e["fine_density"] for e in catalog}

    by_name = {e["name"]: e for e in endpoints}
    if len(library_densities) == 1:
        # Single shared default makes sense.
        d = next(iter(library_densities))
        assert int(by_name["admin-library-add"]["defaults"]["density"]) == d
    if len(fine_densities) == 1:
        d = next(iter(fine_densities))
        assert int(by_name["admin-fine-build"]["defaults"]["density"]) == d


# ---------------------------------------------------------------------------
# /admin/categories -- discovery endpoint
# ---------------------------------------------------------------------------


def test_admin_categories_endpoint_is_get(endpoints):
    """Discovery is parameter-less and read-only -- GET is
    the honest verb. POST is the default in url2code for
    tool invocations; this is the exception."""
    e = next(e for e in endpoints if e["name"] == "admin-categories")
    assert e["method"] == "GET"


def test_admin_categories_returns_native_json(endpoints):
    """/admin/categories `cat`s the catalog and returns its
    parsed contents as `parsed_output`. text mode would force
    callers to re-parse; regex_json is wrong for a
    structured array."""
    e = next(e for e in endpoints if e["name"] == "admin-categories")
    assert e["output"]["mode"] == "native_json"


def test_admin_categories_reads_catalog_file(endpoints):
    """The endpoint runs cat-yaml-as-json on the YAML
    catalog -- drift between the served file and the file
    the test loads means /admin/categories advertises
    slugs the YAML doesn't have endpoints for, or vice
    versa. Also pins the wrapper path so a stray refactor
    that switches back to /bin/cat (which would emit raw
    YAML and break native_json parsing) fails fast."""
    e = next(e for e in endpoints if e["name"] == "admin-categories")
    assert e["command"]["executable"] == "/app/bin/cat-yaml-as-json"
    assert e["command"]["args"] == ["/app/config/categories.yaml"]
