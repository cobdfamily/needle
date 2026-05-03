# Deployment

needle ships as a container image to the kibble registry
on every `git tag v*`. The image is built on top of
`cobdfamily/url2code:<tag>` and adds:

- ffmpeg (`apt-get`)
- audfprint2 (`uv pip install` into the base image's venv)
- `config/tools.yaml` — the entire HTTP surface

No Python source is added; the runtime is url2code's
FastAPI engine, configured by the YAML.

## Pre-flight checklist

- [ ] Public hostname for needle (eg. `match.cobd.ca`)
      with an A record. The service speaks plain HTTP
      on `:8000` behind your reverse proxy / TLS
      terminator.
- [ ] Fingerprint database tree on the deploy host
      (see "Data tree" below). Bind-mounted at
      `/data` inside the container.
- [ ] Disk space for uploads + outputs. Each request
      writes the uploaded audio under `/tmp/needle/`
      and audfprint reads it from there.

## Image distribution

`.github/workflows/release.yml` builds and pushes the
image on every `git tag v*`:

```sh
git tag -a v0.1.0 -m "Release 0.1.0"
git push origin v0.1.0
```

Within a couple of minutes:

- `kibble.apps.blindhub.ca/cobdfamily/needle:0.1.0`
- `kibble.apps.blindhub.ca/cobdfamily/needle:latest`

Multi-arch (amd64 + arm64), matching the fleet.

## Data tree

needle expects a bind-mounted database tree at `/data`:

```
/data/
  films/
    library.pklz                  # coarse, multi-file
    fine/
      <file-id>.pklz              # high-density, one
                                  # per known film
  tvshows/
    library.pklz
    fine/<file-id>.pklz
  youtube/
    library.pklz
    fine/<file-id>.pklz
  shorts/
    library.pklz
    fine/<file-id>.pklz
  reels/
    library.pklz
    fine/<file-id>.pklz
```

`<file-id>` is whatever stable identifier you key by
(IMDb id for films, episode id for tv, YouTube video
id for youtube/shorts, Instagram media id for reels).
needle does not interpret the id; it only substitutes
it into the database path.

The two database tiers exist because:

- `library.pklz` is built at low fingerprint density
  (the audfprint default). Small file, fast lookup,
  good enough to identify *which* file a clip comes
  from.
- `fine/<id>.pklz` is built per-file at higher density
  (a typical setting is `--density 40-80`). Slower
  per-call but returns precise time ranges for the
  match.

## Build the database files

Two paths: in-band via the admin endpoints, or
out-of-band on a host with audfprint installed.

### In-band: admin endpoints (write to /data)

```sh
# Add one film to the films category's coarse library:
curl -fsS -X POST \
  -H "X-Api-Key: $NEEDLE_ADMIN_KEY" \
  -F category=films \
  -F id=tt0123456 \
  -F audio=@/sources/films/inception.m4a \
  https://match.cobd.ca/admin/library/add

# Build the fine-grained per-file database for that film:
curl -fsS -X POST \
  -H "X-Api-Key: $NEEDLE_ADMIN_KEY" \
  -F category=films \
  -F id=tt0123456 \
  -F audio=@/sources/films/inception.m4a \
  https://match.cobd.ca/admin/fine/build

# List what's currently indexed in the films library:
curl -fsS -X POST \
  -H "X-Api-Key: $NEEDLE_ADMIN_KEY" \
  -F category=films \
  https://match.cobd.ca/admin/library/list
```

needle has no built-in auth. The `X-Api-Key` header
above is enforced **at the reverse proxy**, not by the
service itself; nginx or Traefik can do this with a
single auth_request directive. Without that gate, the
admin endpoints are open to the world. Sample nginx
snippet:

```nginx
location /admin/ {
    if ($http_x_api_key != "$NEEDLE_ADMIN_KEY") {
        return 401;
    }
    proxy_pass http://127.0.0.1:8000;
    # ...
}
```

The data volume must be mounted **read-write** for the
admin endpoints to succeed (the match endpoints don't
write, so a `:ro` mount is fine for an
identification-only deployment).

Track names in the .pklz files match the canonical
``id`` you pass per call. Internally the upload is saved
to ``<temp_dir>/<id>.<ext>``, so audfprint stores
``/tmp/needle/uploads/admin-library-add/<id>.<ext>`` as
the entry name. ``/identify`` then returns ``matched``
with that path; the trailing component is your id.
(This relies on `url2code >= 1.0.6`, which added
``uploads[*].name_template`` for exactly this case.)

### Out-of-band: audfprint directly

```sh
# Coarse library (one entry per film, name preserved)
audfprint new   --dbase films/library.pklz \
                --density 20 \
                /sources/films/*.m4a

# Per-file fine database (high density, single file)
audfprint new   --dbase films/fine/tt0123456.pklz \
                --density 40 \
                /sources/films/inception.m4a
```

Use this when you want stable / canonical track names
in the .pklz file (audfprint records whatever filename
you hand it).

## Run

```yaml
# /opt/needle/docker-compose.yaml
services:
  needle:
    image: kibble.apps.blindhub.ca/cobdfamily/needle:0.1.0
    container_name: needle
    restart: unless-stopped
    ports:
      - "127.0.0.1:8000:8000"
    volumes:
      # :rw if you'll use the admin endpoints (write into
      # the database tree). :ro for an identification-only
      # deployment.
      - ./data:/data:rw
```

```sh
mkdir -p /opt/needle/data
# rsync the database tree into /opt/needle/data
cd /opt/needle
docker compose pull
docker compose up -d
docker compose logs -f needle
```

Behind your TLS reverse proxy, route
`https://match.cobd.ca/*` to `127.0.0.1:8000`.

## Verify

```sh
# Liveness — returns service / status / version:
curl -fsS https://match.cobd.ca/

# Generated OpenAPI docs at /docs and /redocs.

# Identify a clip:
curl -fsS -X POST \
  -F audio=@/path/to/clip.m4a \
  https://match.cobd.ca/films/identify | jq

# For a known film id, precise timestamps:
curl -fsS -X POST \
  -F audio=@/path/to/clip.m4a \
  -F id=tt0123456 \
  https://match.cobd.ca/films/timestamps | jq
```

## Routine operations

### Upgrading

```sh
git tag -a v0.1.1 -m "Release 0.1.1"
git push origin v0.1.1
# CI builds and pushes.

sed -i 's|needle:[^ ]*|needle:0.1.1|' docker-compose.yaml
docker compose pull
docker compose up -d --no-deps needle
```

### Adding a category

Two-step edit:

1. **Add the slug to `config/categories.yaml`.** Each
   entry is `{slug, name, description, library_density,
   fine_density, min_count}`. The catalog is the canonical
   source of truth and is served at `/admin/categories`.

   ```yaml
   - slug: podcasts
     name: Podcasts
     description: Long-form podcast audio.
     library_density: 20
     fine_density: 40
     min_count: null
   ```

2. **Add the matching endpoints + admin enum entries to
   `config/tools.yaml`.** Two new match endpoints
   (`/<slug>/identify` and `/<slug>/timestamps`) plus
   extending the `category` enum on the three admin
   endpoints (`admin-library-add`, `admin-fine-build`,
   `admin-library-list`). Copy an existing category's
   blocks; if `min_count` is set, include `-N <value>` in
   the match args.

CI fails fast if (1) and (2) drift -- `test_config.py`
asserts the YAML enum lists, per-category match endpoints,
and `-N` overrides all match the catalog. Retuning an
existing category (changing densities or `min_count`) is
a `categories.yaml` edit alone.

Then rebuild the image.

### Backups

What must persist:

- `/opt/needle/data/` — the entire database tree.
  Without this, every endpoint returns 502 (audfprint
  can't open a missing pklz). Build offline, rsync to
  the host, version on your side; needle has no
  database management.

Safe to lose:

- `/tmp/needle/uploads/` — per-request scratch.
- Container logs (ship to your aggregator).
