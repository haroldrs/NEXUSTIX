#!/usr/bin/env bash
# Une una computadora al clúster NEXUSTIX con un solo comando.
#
# En la PC administradora (la que ejecutó `nodos.py preparar`):
#   bash despliegue-nodos/unir.sh enviar ~/nexustix-entrega pc2      # empaqueta y envía por Tailscale
#   bash despliegue-nodos/unir.sh empaquetar ~/nexustix-entrega pc2  # solo crea pc2.tgz (para USB)
#
# En la PC que se une (Fedora, Linux o Ubuntu de WSL2):
#   tailscale file get ~/nexustix-recibido      # trae el paquete y este script
#   bash ~/nexustix-recibido/unir.sh recibir [--imagenes nexustix-imagenes.tar] [--admin-email correo]
#   bash unir.sh iniciar pc2.tgz|carpeta [--imagenes archivo.tar] [--admin-email correo]
#   bash unir.sh estado  [carpeta]
#   bash unir.sh detener [carpeta]      # detiene sin borrar datos
#
# `iniciar` descomprime la carpeta del nodo, carga las imágenes si se indican y
# enciende en orden: votante etcd -> bases de datos -> servicios. Al final
# muestra el estado y la dirección del portal. No borra volúmenes nunca.
set -euo pipefail

DESTINO_BASE="${NEXUSTIX_NODO_DIR:-$HOME/nexustix-nodo}"
ESPERA_VOTANTES="${NEXUSTIX_ESPERA_VOTANTES:-300}"
ESPERA_BASES="${NEXUSTIX_ESPERA_BASES:-420}"

verde() { printf '\033[32m%s\033[0m\n' "$*"; }
amarillo() { printf '\033[33m%s\033[0m\n' "$*"; }
rojo() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
paso() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
falla() { rojo "ERROR: $*"; exit 1; }

requisitos() {
    command -v docker >/dev/null || falla "Docker no está instalado. En Windows, active la integración de Docker Desktop con Ubuntu (WSL2)."
    docker info >/dev/null 2>&1 || falla "Docker no responde. Inicie Docker (Fedora: sudo systemctl start docker; Windows: abra Docker Desktop)."
    docker compose version >/dev/null 2>&1 || falla "Falta Docker Compose v2 (comando 'docker compose')."
    command -v python3 >/dev/null || falla "Falta python3."
}

# ---------- PC administradora ----------
empaquetar() {
    local entrega="${1:?Indique la carpeta de la entrega}" nodo="${2:?Indique el nodo, por ejemplo pc2}"
    [ -f "$entrega/$nodo/compose.yml" ] || falla "No existe $entrega/$nodo/compose.yml. ¿Ejecutó nodos.py preparar?"
    [ ! -e "$entrega/INCOMPLETE" ] || falla "La entrega está incompleta; vuelva a preparar."
    local salida="$PWD/$nodo.tgz"
    # La carpeta master (claves maestras) nunca sale de esta PC.
    tar czf "$salida" -C "$entrega" "$nodo"
    chmod 600 "$salida"
    verde "Paquete creado: $salida"
    echo "$salida"
}

enviar() {
    local entrega="${1:?}" nodo="${2:?}" equipo="${3:-$2}"
    command -v tailscale >/dev/null || falla "Tailscale no está instalado; use 'empaquetar' y copie el archivo por USB."
    local archivo
    archivo="$(empaquetar "$entrega" "$nodo" | tail -1)"
    paso "Enviando $nodo.tgz a '$equipo' por Tailscale"
    tailscale file cp "$archivo" "$equipo:"
    cp "$0" "$PWD/unir.sh" 2>/dev/null || true
    tailscale file cp "$PWD/unir.sh" "$equipo:" 2>/dev/null || true
    verde "Enviado. En '$equipo' ejecute:"
    echo "  mkdir -p ~/nexustix-recibido && tailscale file get ~/nexustix-recibido"
    echo "  bash ~/nexustix-recibido/unir.sh recibir [--imagenes nexustix-imagenes.tar]"
}

# ---------- PC que se une ----------
ubicar() {
    # Devuelve la carpeta del nodo: la indicada, o la única carpeta en DESTINO_BASE.
    local origen="${1:-}"
    if [ -n "$origen" ] && [ -f "$origen/compose.yml" ]; then (cd "$origen" && pwd); return; fi
    local encontrados=() archivo
    for archivo in "$DESTINO_BASE"/*/compose.yml; do
        case "$archivo" in *.anterior-*/compose.yml) continue;; esac   # respaldos de configuración
        [ -f "$archivo" ] && encontrados+=("$archivo")
    done
    [ "${#encontrados[@]}" -gt 0 ] || falla "No hay ningún nodo en $DESTINO_BASE. Use: bash unir.sh iniciar paquete.tgz"
    [ "${#encontrados[@]}" -eq 1 ] || falla "Hay varios nodos en $DESTINO_BASE; indique la carpeta."
    dirname "${encontrados[0]}"
}

extraer() {
    local paquete="$1"
    case "$PWD$paquete" in /mnt/[a-z]/*) amarillo "Aviso: en WSL conviene trabajar en el disco de Ubuntu (~), no en /mnt/c.";; esac
    mkdir -p "$DESTINO_BASE"
    chmod 700 "$DESTINO_BASE"
    local nodo
    nodo="$(tar tzf "$paquete" | head -1 | cut -d/ -f1)"
    [ -n "$nodo" ] || falla "Paquete vacío o dañado: $paquete"
    if [ -d "$DESTINO_BASE/$nodo" ]; then
        # Paquete nuevo de la misma instalación: se actualiza la configuración.
        # Los datos viven en volúmenes de Docker y no se tocan.
        local respaldo="$DESTINO_BASE/$nodo.anterior-$(date +%Y%m%d-%H%M%S)"
        mv "$DESTINO_BASE/$nodo" "$respaldo"
        amarillo "Configuración anterior guardada en $respaldo (los datos se conservan)."
    fi
    tar xzf "$paquete" -C "$DESTINO_BASE"
    echo "$DESTINO_BASE/$nodo"
}

cargar_imagenes() {
    local archivo="$1"
    [ -f "$archivo" ] || falla "No existe el archivo de imágenes: $archivo"
    paso "Cargando imágenes desde $archivo (puede tardar unos minutos)"
    docker load -i "$archivo" | tail -3
}

orden() {
    # Grupo de servicios de launch-order.json: coordination | primary | standby | applications
    python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1])).get(sys.argv[2], [])))' "$1/launch-order.json" "$2"
}

esperar_sanos() {
    # Espera a que los servicios indicados estén "healthy", sin bloquear para siempre.
    local carpeta="$1" limite="$2" mensaje="$3"; shift 3
    local inicio=$SECONDS servicio id estado
    while :; do
        local quedan=()
        for servicio in "$@"; do
            id="$(cd "$carpeta" && docker compose ps -q "$servicio" 2>/dev/null || true)"
            estado=ausente
            [ -n "$id" ] && estado="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || echo ausente)"
            [ "$estado" = healthy ] || quedan+=("$servicio:$estado")
        done
        if [ "${#quedan[@]}" -eq 0 ]; then printf '\n'; return 0; fi
        if [ $((SECONDS - inicio)) -ge "$limite" ]; then
            printf '\n'; amarillo "Tras ${limite}s siguen pendientes: ${quedan[*]}"
            return 1
        fi
        printf '\r  %s (%ss) — pendientes: %s   ' "$mensaje" "$((SECONDS - inicio))" "${quedan[*]}"
        sleep 5
    done
}

iniciar() {
    local origen="" imagenes="" admin=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --imagenes) imagenes="${2:?}"; shift 2;;
            --admin-email) admin="${2:?}"; shift 2;;
            *) origen="$1"; shift;;
        esac
    done
    requisitos
    if [ -n "$imagenes" ]; then cargar_imagenes "$imagenes"; fi
    local carpeta
    if [ -n "$origen" ] && [ -f "$origen" ]; then carpeta="$(extraer "$origen" | tail -1)"; else carpeta="$(ubicar "$origen")"; fi
    [ -f "$carpeta/compose.yml" ] || falla "No encuentro compose.yml en $carpeta"
    local nodo; nodo="$(basename "$carpeta")"
    paso "Nodo $nodo ($carpeta)"
    cd "$carpeta"
    if grep -q '^ *build:' compose.yml; then
        [ -n "${NEXUSTIX_SOURCE_ROOT:-}" ] || falla "Esta entrega construye imágenes: defina NEXUSTIX_SOURCE_ROOT con la ruta del repositorio, o prepare la entrega con --imagenes-offline y cargue el paquete de imágenes."
    fi

    local votante bases aplicaciones
    votante="$(orden "$carpeta" coordination)"
    bases="$(orden "$carpeta" primary) $(orden "$carpeta" standby)"
    aplicaciones="$(orden "$carpeta" applications)"

    if [ -n "${votante// /}" ]; then
        paso "1/3 Votante etcd"
        docker compose up -d $votante
        echo "  El votante necesita a la mayoría: encienda también los demás votantes (otras PC / árbitro)."
        esperar_sanos "$carpeta" "$ESPERA_VOTANTES" "Esperando a los demás votantes" $votante \
            && verde "  Votación lista (hay mayoría)." \
            || amarillo "  Aún sin mayoría: el resto se inicia igual y se completará cuando lleguen los otros votantes."
    fi

    if [ -n "${bases// /}" ]; then
        paso "2/3 Bases de datos: $bases"
        docker compose up -d $bases
        esperar_sanos "$carpeta" "$ESPERA_BASES" "Iniciando bases (la réplica se clona desde la principal)" $bases \
            && verde "  Bases listas." \
            || amarillo "  Alguna base sigue iniciando. Revise: bash unir.sh estado"
    fi

    if [ -n "${aplicaciones// /}" ]; then
        paso "3/3 Servicios: $aplicaciones"
        docker compose up -d --wait --wait-timeout 300 || amarillo "  Algún servicio no quedó sano todavía."
    fi

    if [ -n "$admin" ] && grep -qE '^  usuarios:' compose.yml; then
        paso "Administrador"
        docker compose exec -T usuarios python bootstrap_admin.py --email "$admin" --password-file /run/secrets/admin_password \
            || amarillo "  No se creó (si ya existía, es normal)."
    fi
    estado "$carpeta"
}

estado() {
    requisitos
    local carpeta; carpeta="$(ubicar "${1:-}")"
    cd "$carpeta"
    paso "Estado de $(basename "$carpeta")"
    docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
    python3 - "$carpeta" <<'PY'
import json, sys, urllib.request
from pathlib import Path
folder = Path(sys.argv[1])
node = folder.name
try:
    document = json.loads((folder / "config/operations-inventory.json").read_text())
except (OSError, ValueError):
    sys.exit(0)
ports = document.get("ports", {})
me = next((n for n in document.get("nodes", []) if n.get("id") == node), {})
print()
if me.get("arbiter"):
    print("Este equipo es ÁRBITRO: solo vota para elegir la base principal.")
for point in document.get("access_points", []):
    mark = "  <- esta PC" if point.get("node") == node else ""
    print(f"Portal: {point['url']}{mark}")
coordination = document.get("coordination")
if coordination == "patroni_etcd_quorum":
    print("Failover automático de bases: activo (mayoría de votantes etcd).")
for domain in ("usuarios", "eventos", "compras"):
    if domain in me.get("roles", []):
        # En Fedora/Linux los servicios escuchan en la IP de la PC; en WSL2, en todas.
        status = "sin respuesta todavía"
        for host in ("127.0.0.1", me.get("address")):
            try:
                with urllib.request.urlopen(f"http://{host}:{ports.get(domain + '_backend')}/health", timeout=3) as answer:
                    status = json.loads(answer.read()).get("status")
                    break
            except Exception:
                continue
        print(f"  {domain}: {status}")
PY
}

detener() {
    requisitos
    local carpeta; carpeta="$(ubicar "${1:-}")"
    cd "$carpeta"
    docker compose stop
    verde "Detenido. Los datos se conservan; para volver: bash unir.sh iniciar"
}

recibir() {
    command -v tailscale >/dev/null || falla "Tailscale no está instalado."
    local bandeja="$HOME/nexustix-recibido"
    mkdir -p "$bandeja"
    tailscale file get "$bandeja"
    local paquete
    paquete="$(ls -t "$bandeja"/*.tgz 2>/dev/null | head -1)"
    [ -n "$paquete" ] || falla "No llegó ningún paquete .tgz. Pida que lo envíen con: bash unir.sh enviar <entrega> <nodo>"
    iniciar "$paquete" "$@"
}

ayuda() { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; }

accion="${1:-ayuda}"; shift || true
case "$accion" in
    empaquetar) empaquetar "$@";;
    enviar) enviar "$@";;
    recibir) recibir "$@";;
    iniciar) iniciar "$@";;
    estado) estado "$@";;
    detener) detener "$@";;
    *) ayuda;;
esac
