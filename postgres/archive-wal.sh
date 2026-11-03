#!/bin/sh
# PostgreSQL reintenta si falla; no sobrescribe un WAL diferente.
set -eu
source_file="$1"
name="$2"
archive="${NEXUSTIX_WAL_ARCHIVE:-/var/lib/postgresql/archive}"
case "$name" in *[!A-Za-z0-9._-]*|'') exit 2;; esac
mkdir -p "$archive"
if [ -f "$archive/$name" ]; then
    cmp -s "$source_file" "$archive/$name"
else
    temp="$archive/.$name.$$"
    trap 'rm -f "$temp"' EXIT HUP INT TERM
    cp "$source_file" "$temp"
    chmod 0600 "$temp"
    # Publicación atómica sin sobrescribir un archivo concurrente.
    ln "$temp" "$archive/$name" || cmp -s "$source_file" "$archive/$name"
fi

sync
