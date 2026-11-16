#!/bin/bash
set -e

# Parse arguments
API_PORT="5000"
USE_DOCKER=false
BASE_URL=""
DOCKER_IMAGE="edr-rmtn-pygeoapi"
DOCKER_CONTAINER="edr-rmtn-pygeoapi"
usage() {
    echo "Usage: $0 [--docker] [--base-url URL]"
    echo "  (default)       install into the current Python env and run gunicorn"
    echo "  --docker        build Dockerfile.edr and run the same gunicorn in a background container"
    echo "  --base-url URL  set server.url in example-config.yaml (the public URL used in all links)"
    echo "                  e.g. --base-url https://edr-api-c.mdl.nws.noaa.gov/ra-iv"
}
while [ $# -gt 0 ]; do
    case "$1" in
        --docker) USE_DOCKER=true ;;
        --base-url)
            if [ -z "${2:-}" ] || [[ "$2" == --* ]]; then
                echo "--base-url needs a URL" >&2; exit 1
            fi
            BASE_URL="$2"; shift ;;
        --base-url=*) BASE_URL="${1#--base-url=}" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1 (try --help)" >&2; exit 1 ;;
    esac
    shift
done
# Configuration
GUNICORN_WORKERS=2

# Environment setup
export PYGEOAPI_CONFIG=example-config.yaml
export PYGEOAPI_OPENAPI=openapi-local.yaml

# PID tracking
PIDS=()

cleanup() {
    echo "Shutting down services..."
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "Stopping process $pid"
            kill "$pid" 2>/dev/null || true
        fi
    done
    wait
    echo "All services stopped"
}

trap cleanup EXIT INT TERM
set_base_url() {
    # Rewrites only the `url:` line inside the `server:` block, so comments
    # and formatting in the rest of the file are kept.
    echo "Setting server.url in $PYGEOAPI_CONFIG to $BASE_URL"
    python3 - "$PYGEOAPI_CONFIG" "$BASE_URL" <<'PYEOF'
import re
import sys

path, url = sys.argv[1], sys.argv[2].rstrip('/')
if not re.fullmatch(r'https?://[^\s"\'#]+', url):
    sys.exit(f'Invalid base URL: {url!r} (expected http:// or https://)')

lines = open(path).read().split('\n')
in_server = False
done = False
for i, line in enumerate(lines):
    if re.match(r'^\S', line):  # top-level key
        in_server = line.startswith('server:')
        continue
    if in_server and re.match(r'^    url:', line):
        lines[i] = f'    url: {url}'
        done = True
        break
if not done:
    sys.exit(f'No server.url found in {path}')
open(path, 'w').write('\n'.join(lines))
PYEOF
}

build_app() {
    echo "Building application..."
    pip install -r requirements.txt
    pip install -r requirements-dev.txt
    pip install gunicorn gevent bs4 fsspec pygrib eccodes
    python3 setup.py build
    python3 setup.py install
}

generate_openapi() {
    echo "Generating OpenAPI spec..."
    pygeoapi openapi generate "$PYGEOAPI_CONFIG" --output-file "$PYGEOAPI_OPENAPI"
}

start_api() {
    echo ""
    echo "=========================================="
    echo "Starting pygeoapi"
    echo "URL:     http://localhost:$API_PORT"
    echo "Workers: $GUNICORN_WORKERS (gevent)"
    echo "Cache:   $ENABLE_CACHE"
    echo "=========================================="
    echo ""

    gunicorn edr_app:APP \
        -w "$GUNICORN_WORKERS" \
        -k gevent \
        --worker-connections 1000 \
        -b "0.0.0.0:$API_PORT" \
        --access-logfile - \
        --error-logfile - \
        --log-level info \
        # -c gunicorn_config.py &
    PIDS+=($!)
    wait "${PIDS[-1]}"
}

start_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        echo "docker not found" >&2
        exit 1
    fi
    if docker ps -a --format '{{.Names}}' | grep -qx "$DOCKER_CONTAINER"; then
        echo "Removing old container $DOCKER_CONTAINER"
        docker rm -f "$DOCKER_CONTAINER" >/dev/null
    fi
    echo "Building image $DOCKER_IMAGE (Dockerfile.edr)..."
    docker build -f Dockerfile.edr -t "$DOCKER_IMAGE" .
    echo "Starting container $DOCKER_CONTAINER in the background..."
    # -d: detached. --restart: comes back after a crash or a Docker restart,
    # until stopped with `docker stop`.
    # Config is mounted read-only, so edits only need a restart, not a rebuild.
    # Runs as the host user so the mounted config is readable.
    if ! docker run -d --init \
        --restart unless-stopped \
        --name "$DOCKER_CONTAINER" \
        --user "$(id -u):$(id -g)" \
        -p "$API_PORT:5000" \
        -e GUNICORN_WORKERS="$GUNICORN_WORKERS" \
        -v "$(pwd)/$PYGEOAPI_CONFIG:/pygeoapi/example-config.yaml:ro" \
        "$DOCKER_IMAGE" >/dev/null; then
        echo "Could not start the container (is port $API_PORT in use?)" >&2
        docker rm -f "$DOCKER_CONTAINER" >/dev/null 2>&1 || true
        exit 1
    fi

    # Wait until the API answers, so failures are reported here.
    echo -n "Waiting for http://localhost:$API_PORT "
    for _ in $(seq 1 60); do
        if curl -sf -o /dev/null "http://localhost:$API_PORT/"; then
            echo "ok"
            echo ""
            echo "=========================================="
            echo "pygeoapi running in Docker"
            echo "URL:       http://localhost:$API_PORT"
            echo "Base URL:  $(grep -m1 '^    url:' "$PYGEOAPI_CONFIG" | sed 's/^    url: //')   (server.url, used in links)"
            echo "Workers:   $GUNICORN_WORKERS (gevent)"
            echo "Container: $DOCKER_CONTAINER"
            echo "Logs:      docker logs -f $DOCKER_CONTAINER"
            echo "Restart:   docker restart $DOCKER_CONTAINER   (after config edits)"
            echo "Stop:      docker rm -f $DOCKER_CONTAINER"
            echo "=========================================="
            exit 0
        fi
        if [ "$(docker inspect -f '{{.State.Running}}' "$DOCKER_CONTAINER" 2>/dev/null)" != "true" ]; then
            break
        fi
        echo -n "."
        sleep 1
    done
    echo ""
    echo "pygeoapi did not start. Last log lines:" >&2
    docker logs --tail 30 "$DOCKER_CONTAINER" >&2 || true
    docker rm -f "$DOCKER_CONTAINER" >/dev/null 2>&1 || true
    exit 1
}

if [ -n "$BASE_URL" ]; then
    set_base_url
fi

if [ "$USE_DOCKER" = true ]; then
    trap - EXIT INT TERM
    start_docker
fi

build_app
generate_openapi
start_api
