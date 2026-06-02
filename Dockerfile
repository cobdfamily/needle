# needle image: cobdfamily/url2code base + ffmpeg + audfprint2.
#
# No Python source in this repo (tests aside). The HTTP surface is
# entirely defined in config/tools.yaml — url2code reads it on
# startup and registers the FastAPI routes from it.
#
# Operators bind-mount the fingerprint database tree at /data
# (per-category .pklz files); see DEPLOYMENT.md.

ARG URL2CODE_TAG=2.1.0
FROM kibble.apps.blindhub.ca/cobdfamily/url2code:${URL2CODE_TAG}

USER root

# ffmpeg: audfprint2 shells out to it to decode any non-WAV audio
# (mp3, m4a, opus, webm, ...) on its way to the spectrogram.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Pre-create the bind-mount target while still root, owned by the
# unprivileged runtime user so the admin endpoints can write to
# the mount when the operator opts into a :rw bind. A `docker run`
# without -v boots cleanly (every endpoint that touches /data will
# 502 until the operator mounts a real data tree, but the
# container itself starts).
RUN mkdir -p /data \
 && chown url2code:url2code /data

# Drop privileges before pip-installing into the venv so the new
# site-packages stay owned by the unprivileged runtime user the
# url2code base image already created.
USER url2code

# audfprint2 — fork of the Columbia audfprint, packages well as a
# CLI: `audfprint match`, `audfprint new`, `audfprint add`, ...
# url2code's runtime image already ships `uv`; use it to land
# audfprint2 in the venv at /app/.venv (the uv-built venv has no
# pip of its own).
RUN uv pip install --no-cache --python /app/.venv/bin/python audfprint2

# Replace url2code's bundled example tools.yaml with needle's
# YAML — this is the entire HTTP surface of the resulting image.
# The base image's URL2CODE_CONFIG defaults to /app/config/tools.yaml,
# so a plain `COPY` over the same path is enough.
COPY --chown=url2code:url2code config /app/config

# Tiny shell wrapper around audfprint. Two reasons:
#  * audfprint logs through Python's logging module (stderr);
#    url2code's regex parser reads stdout. Wrapper merges
#    stderr -> stdout so the parser sees match / "Saved..."
#    lines.
#  * ``audfprint add`` errors when the dbase doesn't exist;
#    wrapper auto-switches to ``new`` so operators seeding a
#    fresh data tree don't have to special-case the first call.
COPY --chown=url2code:url2code bin /app/bin
USER root
RUN chmod 0755 /app/bin/audfprint /app/bin/needle-data-check /app/bin/needle-snapshot
USER url2code

# bin/cat-yaml-as-json is provided by url2code:>=1.0.7 itself
# (lives at /app/bin/cat-yaml-as-json in the base layer); this
# image's bin/ COPY layers on top without clobbering it.

# CMD inherited from the base image
# (uvicorn url2code.main:app --host 0.0.0.0 --port 8000) is
# preserved; ENTRYPOINT is unset.
