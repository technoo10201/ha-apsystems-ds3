# Thin layer over the official HA stable image.
#
# Why: as of 2026-05, the upstream `ghcr.io/home-assistant/home-assistant:stable`
# image ships a numpy build whose `_core/arrayprint.py` contains stray null
# bytes — `cloud`, `stream`, `mobile_app` and a few others fail to import,
# polluting the logs even though aps_zigbee doesn't need numpy. We force-
# reinstall numpy at image-build time so the fix survives every
# `docker compose down && up`.
#
# Drop this Dockerfile and switch back to `image:` in docker-compose.yml once
# the upstream image is repaired.

FROM ghcr.io/home-assistant/home-assistant:stable

RUN pip install --no-cache --force-reinstall --root-user-action=ignore numpy
