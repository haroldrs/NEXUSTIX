#!/usr/bin/env bash
set -euo pipefail

# DESTRUCTIVO: elimina el historial de compras y reservas para empezar una
# demostración limpia. No elimina los volúmenes ni las bases de datos completas.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

cat <<'EOF'
ATENCIÓN: este procedimiento elimina las compras guardadas y el registro de
reservas de prueba, y restablece el stock de los cinco eventos.
No lo ejecute si necesita conservar evidencias o datos actuales.
EOF
read -r -p 'Escriba RESTABLECER para continuar: ' confirmacion
if [[ "$confirmacion" != "RESTABLECER" ]]; then
  echo "Cancelado; no se modificaron datos."
  exit 0
fi

echo "Deteniendo los servicios que modifican compras y stock..."
docker compose stop compras1 compras2 eventos

echo "Limpiando historial de compras..."
docker compose exec -T compras_db psql -v ON_ERROR_STOP=1 -U postgres -d compras_db -c 'TRUNCATE TABLE compras RESTART IDENTITY;'

echo "Limpiando reservas idempotentes y restableciendo stock..."
docker compose exec -T eventos_db psql -v ON_ERROR_STOP=1 -U postgres -d eventos_db -c "TRUNCATE TABLE reservas_stock; INSERT INTO eventos (id, nombre, stock) VALUES (1, 'Concierto Rock en Trujillo', 500), (2, 'Festival de Jazz', 20), (3, 'Concierto Pop Lima', 300), (4, 'Festival Electrónico', 250), (5, 'Concierto Internacional', 400) ON CONFLICT (id) DO UPDATE SET nombre = EXCLUDED.nombre, stock = EXCLUDED.stock;"

echo "Iniciando nuevamente los servicios..."
docker compose start eventos compras1 compras2

echo "Esperando unos segundos a que las APIs estén disponibles..."
sleep 5
curl -fsS http://localhost:8002/health >/dev/null
curl -fsS http://localhost:8003/health >/dev/null
curl -fsS http://localhost:8004/health >/dev/null

echo "Entorno de pruebas restablecido. Ejecute scripts/prueba_punto1_consistencia.py."
