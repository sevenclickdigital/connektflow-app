#!/bin/sh
# Runs the web server and makes its container leave the proxy's rotation before
# it stops. Used by `app` and `app-standby` in docker-compose.dokploy.yml.
#
# Traefik keeps sending requests to a container until Docker reports it stopped
# or unhealthy, and Gunicorn stops accepting them the moment it is told to stop:
# every request in between is a 502. So, when asked to stop, this first raises a
# flag the container's healthcheck reads, waits DRAIN_SECONDS for the container
# to turn unhealthy and the proxy to move on to the other one, and only then
# lets the server finish the requests it has and exit.
#
# DRAIN_SECONDS must cover the healthcheck's interval x retries, and the
# service's stop_grace_period must cover DRAIN_SECONDS plus Gunicorn's own
# graceful timeout.
#
# Usage: sh deploy/drain.sh gunicorn config.wsgi:application ...
"$@" &
server=$!
trap 'touch /tmp/draining; sleep "${DRAIN_SECONDS:-25}"; kill -TERM "$server"' TERM INT
# The first wait returns when the signal arrives; the second, when the server ends.
wait "$server"
wait "$server"
