# needle

[![test](https://github.com/cobdfamily/needle/actions/workflows/test.yml/badge.svg)](https://github.com/cobdfamily/needle/actions/workflows/test.yml)

Audio-fingerprint match service. Records of audio in,
"that's <which file> at <which time>" out.

This is a YAML-defined microservice — no Python source in
the repo, only tests. The HTTP surface lives in
[`config/tools.yaml`](config/tools.yaml) and is consumed
by the upstream `cobdfamily/url2code` engine, which
needle's image is built on top of.

## What it does

For each broad category of audio (films, tv shows,
YouTube videos, YouTube Shorts, Instagram Reels) the
service exposes two endpoints:

```
POST /<category>/identify
   Match an audio recording against the category's
   coarse fingerprint database. Returns the matched
   file id + a rough offset.

POST /<category>/timestamps?id=<file-id>
   For a known file id, match against the file's
   high-density fingerprint database. Returns precise
   time ranges where the recording aligns inside that
   one file.
```

Categories: `films`, `tvshows`, `youtube`, `shorts`,
`reels`. Short-form categories (shorts, reels) use a
lower min-count threshold so a 5-10s query still
triggers a hit against a ~60s reference clip.

Three admin endpoints write into the bind-mounted
database tree:

```
POST /admin/library/add   form: category + audio
   audfprint add — extend <category>/library.pklz with
   one new audio file.

POST /admin/fine/build    form: category + id + audio
   audfprint new — create or replace
   <category>/fine/<id>.pklz from one audio source.

POST /admin/library/list  form: category
   audfprint list — track names indexed in
   <category>/library.pklz.
```

The admin endpoints have **no built-in auth**. Gate them
at your reverse proxy with an API key (see
DEPLOYMENT.md). The `/data` mount must be `:rw` for
them to succeed.

## Quick start

```sh
# Bring up the service. The fingerprint database tree
# bind-mounts at /data — see DEPLOYMENT.md for layout.
mkdir -p /opt/needle/data
docker run -d --name needle \
  -p 8000:8000 \
  -v /opt/needle/data:/data:ro \
  kibble.apps.blindhub.ca/cobdfamily/needle:latest

# Identify a clip against a category's library:
curl -fsS -X POST \
  -F audio=@/path/to/recording.m4a \
  http://localhost:8000/films/identify

# For a known film id, get precise timestamps:
curl -fsS -X POST \
  -F audio=@/path/to/recording.m4a \
  -F id=tt0123456 \
  http://localhost:8000/films/timestamps
```

## Architecture

```
kibble.../url2code:latest
         |
         v
kibble.../needle:<tag>
  + ffmpeg                    (apt-get; audfprint shells
                               out for non-WAV decode)
  + audfprint2                (uv pip install; landmark-
                               based fingerprint matcher)
  + config/tools.yaml         (10 endpoints — 5 categories
                               x 2 surfaces; the entire
                               HTTP API is declared here)
```

The image carries no Python source of its own. Adding a
category, retuning a category, or adding a new endpoint
shape is a YAML edit + image rebuild.

## Testing the design

This repo is a deliberate test of url2code's design — can
a non-trivial HTTP service be built with zero Python in
the consumer repo? Findings as the service grows live
in [DEPLOYMENT.md](DEPLOYMENT.md) and the changelog.

## License

AGPL-3.0 — see `LICENSE`.
