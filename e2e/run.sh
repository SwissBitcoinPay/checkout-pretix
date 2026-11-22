#!/bin/bash
# The end-to-end test on real pretix, in pretix's own Docker image: the plugin installed from the
# wheel the build made, enabled and given a key in pretix's backend, orders placed and paid in
# the shop. The SDK is faked by e2e/fake, a test-only plugin installed beside it (the plugin
# itself has no switch for it). Needs Docker, curl, and the lockfile's environment
# (`uv sync --locked`); pretix's URL is http://127.0.0.1 on a free port.
#
#     e2e/run.sh [--celery] [pretix's image]
#
# pretix runs on PostgreSQL, its database in production. Without --celery, it runs without task
# workers, as a small installation may: every check of a payment runs in a web request. With it,
# pretix runs its Celery workers beside its web server (its image's `all`), with Redis, as a
# production installation does: the checks the payment page asks for run in the workers, and so
# do each invoice's background checks, which alone see the payment of a customer who left the
# page.
set -euo pipefail

mode=web
if [ "${1:-}" = "--celery" ]; then
  mode=all
  shift
fi
image="${1:-pretix/standalone:2026.9.1@sha256:5e0ccb514c7f16ccf4cb26c060049ae38de160e646e9a80f866a9f1d705701dc}"
redis_image="redis:8.10.3-alpine@sha256:7958cfbb9d152f87dadfbc89f1b0b15938f0b81ff8982feb327476f4911935c3"
postgres_image="postgres:16.15@sha256:9e2cf6287f25c3151f4c9421ccce2b17f3e60061c111a29165e0d2cdd9ff0a97"
root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d "${TMPDIR:-/tmp}/pretix-sattleme-e2e.XXXXXX")"
name="pretix-sattleme-e2e-$$"
tag="pretix-sattleme-e2e:$$"
cleanup() {
  # With their anonymous volumes (pretix's and Redis's images declare some).
  docker rm -f -v "$name" "$name-redis" "$name-db" >/dev/null 2>&1 || true
  docker network rm "$name" >/dev/null 2>&1 || true
  docker image rm -f "$tag" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
cd "$root"

echo "== The wheels: the plugin's (checked as the CI checks it), the SDK's (sdk/), the fake's"
uv run --locked --no-sync python -m build --no-isolation --outdir "$work/dist" . >/dev/null
uv run --locked --no-sync python scripts/check_dist.py "$work/dist"
mkdir -p "$work/context/wheels"
cp "$work"/dist/*.whl "$work/context/wheels/"
# Each from a copy of its sources: setuptools writes its build directories next to them.
cp -R sdk e2e/fake "$work/"
for package in sdk fake; do
  uv run --locked --no-sync python -m build --wheel --no-isolation \
    --outdir "$work/context/wheels" "$work/$package" >/dev/null
done

echo "== The SDK's dependencies: the lockfile's versions and hashes, for the image's Python"
uv export --quiet --locked --no-default-groups --no-emit-project \
  --no-emit-package sattleme-checkout --format requirements-txt -o "$work/requirements.txt"
# Pinned by their digests: an image already here is the one pinned, never pulled again.
pull() {
  docker image inspect "$1" >/dev/null 2>&1 || docker pull --quiet "$1" >/dev/null
}
pull "$image"
docker run --rm --network host --user "$(id -u):$(id -g)" --entrypoint pip3 \
  -e HTTPS_PROXY -e https_proxy -e NO_PROXY -e no_proxy -e PIP_CERT -e SSL_CERT_FILE \
  ${PIP_CERT:+-v "$PIP_CERT:$PIP_CERT:ro"} -e HOME=/tmp \
  -v "$work:/work" "$image" download --quiet --no-cache-dir --require-hashes --no-deps \
  --only-binary :all: -r /work/requirements.txt -d /work/context/wheels
ls "$work/context/wheels"

echo "== pretix's image, with the plugin installed from its wheel"
docker build --quiet --build-arg "PRETIX_IMAGE=$image" -t "$tag" -f e2e/Dockerfile \
  "$work/context" >/dev/null
port="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
cat > "$work/pretix.cfg" <<EOF
[pretix]
instance_name=pretix (end-to-end test)
url=http://127.0.0.1:$port
currency=CHF
datadir=/data
[database]
backend=postgresql
name=pretix
user=pretix
password=pretix
host=$name-db
[locale]
default=en
timezone=Europe/Zurich
EOF
docker network create "$name" >/dev/null
echo "== PostgreSQL, pretix's database"
pull "$postgres_image"
docker run -d --name "$name-db" --network "$name" \
  -e POSTGRES_USER=pretix -e POSTGRES_PASSWORD=pretix -e POSTGRES_DB=pretix \
  --mount type=tmpfs,destination=/var/lib/postgresql/data "$postgres_image" >/dev/null
# Over TCP: while the image initializes its database, a first server answers on its socket only.
for _ in $(seq 120); do
  if docker exec "$name-db" pg_isready -q -h 127.0.0.1 -U pretix -d pretix 2>/dev/null; then
    break
  fi
  sleep 1
done
if [ "$mode" = all ]; then
  echo "== Redis, for pretix's cache, its sessions and its Celery workers"
  pull "$redis_image"
  docker run -d --name "$name-redis" --network "$name" "$redis_image" >/dev/null
  cat >> "$work/pretix.cfg" <<EOF
[redis]
location=redis://$name-redis:6379/0
sessions=true
[celery]
backend=redis://$name-redis:6379/1
broker=redis://$name-redis:6379/2
EOF
fi
chmod 644 "$work/pretix.cfg"
docker run -d --name "$name" --network "$name" -p "127.0.0.1:$port:80" \
  --mount type=tmpfs,destination=/data,tmpfs-mode=1777 \
  -v "$work/pretix.cfg:/etc/pretix/pretix.cfg:ro" "$tag" "$mode" >/dev/null

echo "== pretix starting (its migrations, its '$mode') at http://127.0.0.1:$port"
# Its login page: pretix's /healthcheck/ needs a cache (Redis, memcached), which a pretix without
# Celery lacks here.
for _ in $(seq 300); do
  if curl --noproxy '*' -fsS -o /dev/null "http://127.0.0.1:$port/control/login" 2>/dev/null; then
    break
  fi
  sleep 2
done
status=0
docker exec -i -e AUTOMIGRATE=skip "$name" pretix shell < e2e/setup_event.py || status=$?
if [ "$status" = 0 ]; then
  if [ "$mode" = all ]; then
    python3 e2e/driver.py --celery "http://127.0.0.1:$port" || status=$?
  else
    python3 e2e/driver.py "http://127.0.0.1:$port" || status=$?
  fi
fi
if [ "$status" = 0 ]; then
  # The mails pretix wrote (by its workers, with Celery: soon after): each order's, with the
  # plugin's words on how to pay, then each payment's.
  orders=1
  if [ "$mode" = all ]; then
    orders=3
  fi
  for _ in $(seq 60); do
    docker exec "$name" sh -c 'cat /data/mails/* 2>/dev/null' > "$work/mails.txt" || true
    placed="$(grep -c "^Subject: Your order: " "$work/mails.txt" || true)"
    paid="$(grep -c "^Subject: Payment received for your order: " "$work/mails.txt" || true)"
    if [ "$placed" = "$orders" ] && [ "$paid" = "$orders" ]; then
      break
    fi
    sleep 2
  done
  if [ "$placed" = "$orders" ] && [ "$paid" = "$orders" ] \
    && grep -q "To pay with Bitcoin, open your order" "$work/mails.txt"; then
    echo "ok: each customer's mails say how to pay with Bitcoin, then that the payment came"
  else
    echo "FAILED: the customers' mails ($placed of orders, $paid of payments)" >&2
    status=1
  fi
fi
docker logs "$name" > "$work/pretix.log" 2>&1 || true
if ! grep -q "The sattle.me SDK is faked" "$work/pretix.log"; then
  echo "FAILED: the fake of the SDK did not start" >&2
  status=1
fi
if [ "$status" != 0 ]; then
  echo "== pretix's log" >&2
  tail -n 300 "$work/pretix.log" >&2
fi
exit "$status"
