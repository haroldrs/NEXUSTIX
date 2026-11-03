#!/bin/sh
# Solo dentro de un contenedor efímero sin red: backends HTTP sintéticos, no APIs.
set -eu
[ -f /.dockerenv ] && [ "${NEXUSTIX_SIMULATION:-}" = 1 ] || exit 64
sandbox=$(mktemp -d)
for instance in 1 2; do
    port=$((instance * 10000 + 8003))
    cat > "$sandbox/$instance.cfg" <<CONF
defaults
    mode http
    timeout connect 1s
    timeout client 5s
    timeout server 5s
frontend synthetic
    bind 127.0.0.1:$port
    http-request return status 200 content-type application/json string '{"status":"ok","instancia":"$instance"}'
CONF
done
haproxy -db -f "$sandbox/1.cfg" > "$sandbox/one.log" 2>&1 &
first=$!
haproxy -db -f "$sandbox/2.cfg" > "$sandbox/two.log" 2>&1 &
second=$!
haproxy -db -f /simulation/haproxy.cfg > "$sandbox/haproxy.log" 2>&1 &
proxy=$!
trap 'kill "$first" "$second" "$proxy" 2>/dev/null || true' EXIT
sleep 3
seen1=0; seen2=0
for n in 1 2 3 4 5 6; do
    response=$(wget -q -O - http://127.0.0.1:8005/health)
    case "$response" in *'"instancia":"1"'*) seen1=1;; *'"instancia":"2"'*) seen2=1;; *) exit 65;; esac
done
[ "$seen1" = 1 ] && [ "$seen2" = 1 ]
kill "$first"
wait "$first" 2>/dev/null || true
sleep 6
for n in 1 2 3 4 5; do
    response=$(wget -q -O - http://127.0.0.1:8005/health)
    case "$response" in *'"instancia":"2"'*) :;; *) exit 66;; esac
done
haproxy -db -f "$sandbox/1.cfg" > "$sandbox/one.log" 2>&1 &
first=$!
sleep 6
seen1=0
for n in 1 2 3 4 5 6; do
    response=$(wget -q -O - http://127.0.0.1:8005/health)
    case "$response" in *'"instancia":"1"'*) seen1=1;; esac
done
[ "$seen1" = 1 ]
printf '%s\n' 'APROBADA: dos backends sintéticos, caída de uno, 5 respuestas del otro y recuperación. Sin FastAPI/PostgreSQL/LAN real.'
