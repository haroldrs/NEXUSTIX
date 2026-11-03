#!/usr/bin/env bash
# scripts/diagnostico_compras2_crash.sh
# Herramienta de observabilidad para diagnosticar deterministamente el estado
# de la réplica Compras2 en staging sin recrear el contenedor en finally.
set -euo pipefail

PROJECT_NAME="nexustix-auth-staging"
COMPOSE_FILE="docker-compose.staging.yml"
TARGET_SERVICE="compras2"

if [ ! -f "$COMPOSE_FILE" ]; then
    echo "ERROR: Debe ejecutarse desde la raíz del proyecto donde reside $COMPOSE_FILE" >&2
    exit 1
fi

export NEXUSTIX_STAGING_SECRETS_DIR="${NEXUSTIX_STAGING_SECRETS_DIR:-$HOME/.config/nexustix-auth-staging-secrets}"
if [ ! -d "$NEXUSTIX_STAGING_SECRETS_DIR" ]; then
    echo "ERROR: No existe el directorio de secretos staging: $NEXUSTIX_STAGING_SECRETS_DIR" >&2
    exit 1
fi

TIMESTAMP=$(date -u +"%Y%m%dT%H%M%SZ")
OUTPUT_FILE="diagnostico_compras2_${TIMESTAMP}.log"

echo "=== DIAGNÓSTICO DETERMINISTA DE COMPRAS2 (STAGING) ===" | tee "$OUTPUT_FILE"
echo "Fecha UTC: $(date -u)" | tee -a "$OUTPUT_FILE"
echo "Proyecto Compose: $PROJECT_NAME" | tee -a "$OUTPUT_FILE"
echo "Archivo Compose: $COMPOSE_FILE" | tee -a "$OUTPUT_FILE"
echo "------------------------------------------------------" | tee -a "$OUTPUT_FILE"

# 1. Obtener ID del contenedor compras2
CONTAINER_ID=$(docker compose --project-name "$PROJECT_NAME" -f "$COMPOSE_FILE" ps -q "$TARGET_SERVICE" || true)

if [ -z "$CONTAINER_ID" ]; then
    echo "ALERTA: No se encontró contenedor activo o listado para el servicio $TARGET_SERVICE" | tee -a "$OUTPUT_FILE"
    docker compose --project-name "$PROJECT_NAME" -f "$COMPOSE_FILE" ps --all | tee -a "$OUTPUT_FILE"
    exit 0
fi

echo "Contenedor ID: $CONTAINER_ID" | tee -a "$OUTPUT_FILE"

# 2. Inspección detallada de estado y ciclo de vida de Docker
echo "--- Estado del contenedor (docker inspect) ---" | tee -a "$OUTPUT_FILE"
docker inspect "$CONTAINER_ID" --format '
Nombre: {{.Name}}
Imagen: {{.Config.Image}} (ID: {{.Image}})
Estado: {{.State.Status}}
Running: {{.State.Running}}
ExitCode: {{.State.ExitCode}}
RestartCount: {{.RestartCount}}
StartedAt: {{.State.StartedAt}}
FinishedAt: {{.State.FinishedAt}}
RestartPolicy: {{.HostConfig.RestartPolicy.Name}} (MaxRetries: {{.HostConfig.RestartPolicy.MaximumRetryCount}})
OOMKilled: {{.State.OOMKilled}}
Pid: {{.State.Pid}}
' | tee -a "$OUTPUT_FILE"

# 3. Variables de entorno activas dentro del contenedor
echo "--- Variables de entorno relevantes ---" | tee -a "$OUTPUT_FILE"
docker inspect "$CONTAINER_ID" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E "NEXUSTIX|INSTANCE_ID|UVICORN" | tee -a "$OUTPUT_FILE" || true

# 4. Proceso PID 1 dentro del contenedor
echo "--- Procesos activos en el contenedor (top) ---" | tee -a "$OUTPUT_FILE"
docker top "$CONTAINER_ID" 2>/dev/null || echo "Contenedor no está en ejecución para docker top" | tee -a "$OUTPUT_FILE"

# 5. Últimas 50 líneas de log
echo "--- Últimos logs del contenedor ---" | tee -a "$OUTPUT_FILE"
docker logs --tail 50 --timestamps "$CONTAINER_ID" 2>&1 | tee -a "$OUTPUT_FILE" || true

echo "------------------------------------------------------" | tee -a "$OUTPUT_FILE"
echo "Diagnóstico guardado en: $OUTPUT_FILE"
