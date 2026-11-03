# Guía Oficial de Demostración Académica — NEXUSTIX
## Sistemas Operativos II — Demostración Práctica en Fedora Linux

**Fecha de corte:** 2026-10-25  
**Plataforma de pruebas recomendada:** Entorno de Staging Aislado (`nexustix-auth-staging`)  
**URL Frontend Staging:** `http://localhost:18080`  
**Balanceador de Compras (Nginx):** `http://localhost:18085`

---

## 1. Preparación del Entorno

La demostración se realiza en una máquina con Fedora Linux utilizando **exclusivamente el entorno de staging aislado** para proteger las bases de datos principales y garantizar repetibilidad sin riesgo de corrupción.

### A. Ubicación del proyecto
Abra una terminal y navegue a la raíz del repositorio:
```bash
cd "$HOME/Documents/Informatica/VIII CICLO/2.Sistemas operativos 2/Unit 2/Project"
```

### B. Cargar variables de entorno de Staging
```bash
export NEXUSTIX_STAGING_SECRETS_DIR="$HOME/.config/nexustix-auth-staging-secrets"
export NEXUSTIX_STAGING_HTTP=1
```

Verifique que el directorio de secretos de staging exista:
```bash
ls -la "$NEXUSTIX_STAGING_SECRETS_DIR"
```
*Resultado esperado:* Debe listar los archivos `jwt-private.pem`, `jwt-public.pem`, `eventos-service-token`, `compras-db-password`, `eventos-db-password`, `usuarios-db-password`, etc.

---

## 2. Comprobación de Espacio y Requisitos

Antes de iniciar contenedores o pruebas, verifique que el sistema cuente con espacio suficiente en disco y que los servicios requeridos (Docker) estén activos:

```bash
# 1. Comprobar espacio en disco (mínimo 5 GB libres recomendados)
df -h /

# 2. Comprobar estado de Docker
systemctl is-active docker || sudo systemctl start docker

# 3. Comprobar sintaxis del Compose de staging (modo silencioso, no imprime secretos)
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml config --quiet
echo "Compose de staging: SINTAXIS VÁLIDA"
```

---

## 3. Arranque de la Plataforma (Staging)

Inicie los 9 servicios del clúster de staging (3 bases de datos PostgreSQL 15, 3 APIs FastAPI, 2 réplicas de Compras, 1 balanceador Nginx y 1 servidor web Frontend):

```bash
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml up -d --wait
```

Compruebe el estado de los 9 contenedores:
```bash
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml ps
```
*Resultado esperado:* 9 contenedores en estado `running` o `Up (healthy)`.

---

## 4. Verificación de Salud de los Servicios

Compruebe el endpoint `/health` de cada uno de los microservicios y del balanceador:

```bash
echo "=== COMPROBACIÓN DE ENDPOINTS HEALTH ==="
for port in 18081 18082 18083 18084 18085; do
  echo -n "Puerto $port: "
  curl -s "http://127.0.0.1:$port/health" | jq -c '{servicio: .servicio, status: .status, db: .db, instancia: .instancia}' 2>/dev/null || curl -s "http://127.0.0.1:$port/health"
  echo ""
done

# Frontend estático
curl -s -I http://127.0.0.1:18080/ | head -n 1
```

*Resultado esperado:*
- Puertos 18081 (Usuarios), 18082 (Eventos), 18083 (Compras1), 18084 (Compras2) y 18085 (Compras LB) devuelven HTTP 200 con `"db": "connected"`.
- Puerto 18080 devuelve `HTTP/1.1 200 OK`.

---

## 5. Registro e Inicio de Sesión

### A. Desde la Terminal (Demostración de API)
Registre un comprador de prueba y obtenga su token JWT asimétrico (RS256):

```bash
EMAIL_DEMO="alumno-demo-$(date +%s)@example.com"
PASS_DEMO="ClaveSegura2026!"

echo "1. Registrando usuario comprador..."
REGISTRO_RESP=$(curl -s -X POST http://127.0.0.1:18081/auth/register \
  -H "Content-Type: application/json" \
  -d "{\"nombre\": \"Estudiante SO2\", \"email\": \"$EMAIL_DEMO\", \"password\": \"$PASS_DEMO\"}")

echo "$REGISTRO_RESP" | jq '{user: .user, token_type: .token_type, expires_in: .expires_in}'

TOKEN=$(echo "$REGISTRO_RESP" | jq -r .access_token)
echo "JWT Token recibido (longitud ${#TOKEN} chars)"
```

### B. Desde el Navegador Web (UI)
1. Abra su navegador en `http://localhost:18080`.
2. Haga clic en el botón superior **"Iniciar Sesión / Registro"**.
3. Seleccione la pestaña **"Registrarse"**, ingrese nombre, correo y contraseña.
4. El frontend guardará el token en memoria (`ACCESS_TOKEN`) y la cookie HttpOnly rotatoria de sesión refresh.

---

## 6. Consulta de Catálogo y Stock Inicial

Consulte los eventos disponibles a través de la API de Eventos:

```bash
curl -s http://127.0.0.1:18082/eventos | jq .
```

O consulte el stock de un evento específico (ejemplo Evento #1):
```bash
curl -s http://127.0.0.1:18082/eventos/1/stock | jq .
```
*Resultado esperado:* Muestra `evento_id: 1`, `stock` actual y `reloj_logico_T` (Lamport).

---

## 7. Compra de Entrada y Comprobación de Persistencia

### A. Ejecutar compra individual autenticada con Idempotencia
```bash
IDEM_KEY="demo-key-$(uuidgen)"
echo "Idempotency-Key generada: $IDEM_KEY"

COMPRA_RESP=$(curl -s -X POST http://127.0.0.1:18085/compras \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $IDEM_KEY" \
  -d '{"evento_id": 1, "cantidad": 1}')

echo "$COMPRA_RESP" | jq .
```

*Resultado esperado:*
- Respuesta HTTP 200 con `estado: "confirmada"`, `confirmacion_reserva: "confirmada"`, `operacion_id` (UUID único).
- Campo `atendido_por` muestra qué réplica atendió (`instancia 1` o `instancia 2`).

### B. Comprobar decremento de stock en Eventos
```bash
curl -s http://127.0.0.1:18082/eventos/1/stock | jq .
```
*Resultado esperado:* El stock del evento #1 se redujo exactamente en 1 unidad.

### C. Comprobar persistencia cruzada en PostgreSQL (Staging)
```bash
# 1. Registro en compras_db
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml exec -T compras_db \
  psql -U postgres -d compras_db -c "SELECT id, usuario_id, evento_id, cantidad, estado, operacion_id FROM compras ORDER BY id DESC LIMIT 1;"

# 2. Registro en eventos_db (Reserva confirmada con el mismo operacion_id)
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml exec -T eventos_db \
  psql -U postgres -d eventos_db -c "SELECT operacion_id, evento_id, cantidad, estado FROM reservas_stock ORDER BY creado_en DESC LIMIT 1;"
```
*Resultado esperado:* El `operacion_id` en `compras_db` coincide exactamente con el `operacion_id` en `eventos_db`, certificando la correlación de la saga.

---

## 8. Demostración de Idempotencia y Protección ante Reintentos

### A. Reintento con la MISMA clave y MISMO cuerpo (Replay seguro)
Simule que el cliente perdió la conexión tras enviar la compra y reenvía exactamente la misma petición:

```bash
echo "Reenviando exactamente la misma petición..."
REPLAY_RESP=$(curl -s -i -X POST http://127.0.0.1:18085/compras \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $IDEM_KEY" \
  -d '{"evento_id": 1, "cantidad": 1}')

echo "$REPLAY_RESP" | grep -E "HTTP/|Idempotency-Replayed"
echo "$REPLAY_RESP" | tail -n 1 | jq '{id: .id, operacion_id: .operacion_id, estado: .estado}'
```

*Resultado esperado:*
- Código HTTP 200.
- Header presente: `Idempotency-Replayed: true`.
- Devuelve exactamente la misma compra original con el mismo `id` y `operacion_id`.
- **El stock NO se vuelve a descontar.**

### B. Reintento con la MISMA clave pero DISTINTO cuerpo (Detección de Conflicto)
Simule un ataque o error de cliente que reutiliza una clave para comprar otro evento:

```bash
curl -s -i -X POST http://127.0.0.1:18085/compras \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $IDEM_KEY" \
  -d '{"evento_id": 2, "cantidad": 5}' | head -n 1
```

*Resultado esperado:*
- Código **HTTP 409 Conflict** (`Idempotency-Key ya fue utilizada con otro usuario o contenido`).

---

## 9. Verificación de Balanceo y Réplicas (Compras1 y Compras2)

El balanceador Nginx en el puerto `18085` reparte las peticiones entre `compras1:8003` y `compras2:8003` mediante Round-Robin.

Ejecute 4 peticiones seguidas al balanceador:
```bash
for i in {1..4}; do
  curl -s http://127.0.0.1:18085/health | jq -r '.instancia'
done
```

*Resultado esperado:* Las respuestas alternan equitativamente entre `1` y `2` (ej. `1, 2, 1, 2`).

---

## 10. Concurrencia y Prevención de Sobreventa

Para demostrar que dos compradores concurrentes no pueden comprar la última entrada disponible, crearemos un evento sintético con stock = 1 y lanzaremos dos compras simultáneas:

```bash
# 1. Insertar evento sintético con stock 1 en eventos_db
EVENTO_SINTETICO=999999
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml exec -T eventos_db \
  psql -U postgres -d eventos_db -c "INSERT INTO eventos(id, nombre, stock) VALUES ($EVENTO_SINTETICO, 'Evento Carrera Stock 1', 1) ON CONFLICT (id) DO UPDATE SET stock = 1;"

# 2. Registrar segundo comprador
TOKEN_B=$(curl -s -X POST http://127.0.0.1:18081/auth/register \
  -H "Content-Type: application/json" \
  -d "{\"nombre\": \"Comprador B\", \"email\": \"comprador-b-$(date +%s)@example.com\", \"password\": \"ClaveSegura2026!\"}" | jq -r .access_token)

# 3. Lanzar dos compras en paralelo absoluto contra el balanceador
KEY_A="carrera-A-$(uuidgen)"
KEY_B="carrera-B-$(uuidgen)"

echo "Lanzando compras concurrentes..."
curl -s -X POST http://127.0.0.1:18085/compras -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: $KEY_A" -H "Content-Type: application/json" -d "{\"evento_id\": $EVENTO_SINTETICO, \"cantidad\": 1}" -w "\nResultado Comprador A: HTTP %{http_code}\n" &
PID_A=$!

curl -s -X POST http://127.0.0.1:18085/compras -H "Authorization: Bearer $TOKEN_B" -H "Idempotency-Key: $KEY_B" -H "Content-Type: application/json" -d "{\"evento_id\": $EVENTO_SINTETICO, \"cantidad\": 1}" -w "\nResultado Comprador B: HTTP %{http_code}\n" &
PID_B=$!

wait $PID_A $PID_B

# 4. Verificar stock final (debe ser exactamente 0, nunca negativo)
curl -s "http://127.0.0.1:18082/eventos/$EVENTO_SINTETICO/stock" | jq .
```

*Resultado esperado:*
- Exactamente una petición devuelve **HTTP 200** (compra exitosa).
- La otra petición devuelve **HTTP 409 Conflict** (`Stock insuficiente`).
- El stock final en Eventos es exactamente `0` (garantía de exclusión mutua mediante `SELECT ... FOR UPDATE` en PostgreSQL).

---

## 11. Demostración de Tolerancia a Fallos: Failover Ordenado de Compras1

Demuestre que si se apaga `compras1`, el balanceador conmuta transparentemente a `compras2`:

```bash
# 1. Detener réplica compras1
echo "Deteniendo compras1..."
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml stop compras1

# 2. Enviar compra a través del balanceador Nginx (puerto 18085)
IDEM_FAILOVER="failover-$(uuidgen)"
echo "Enviando compra al balanceador mientras compras1 está caída..."
curl -s -X POST http://127.0.0.1:18085/compras \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $IDEM_FAILOVER" \
  -d '{"evento_id": 2, "cantidad": 1}' | jq '{estado: .estado, atendido_por: .atendido_por}'

# 3. Reactivar compras1
echo "Restaurando compras1..."
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml start compras1
```

*Resultado esperado:*
- La compra se completa exitosamente (`HTTP 200`).
- El campo `atendido_por` certifica: `"compras (instancia 2)"`.
- Al restaurar `compras1`, ambas réplicas vuelven a estar activas en el clúster.

---

## 12. Recuperación de Operaciones Pendientes (`/compras/recuperar-servidor`)

Si una compra se interrumpe después de que Eventos reservó stock pero antes del commit final de Compras, el inventario no se libera a ciegas ni se pierde:

```bash
# El endpoint de recuperación requiere credenciales de administrador
# Consultar diagnóstico del servidor de compras
curl -s -X POST http://127.0.0.1:18083/compras/recuperar-servidor \
  -H "Authorization: Bearer $TOKEN" | jq .
```

*Resultado esperado:*
- Si el usuario no es administrador, responde **HTTP 403 Forbidden** (protección de privilegios).
- Con usuario administrador, ejecuta la reconciliación idempotente consultando el estado durable de Eventos (`/eventos/reservas/{operacion_id}`) y actualizando las respuestas de replay sin duplicar efectos.

---

## 13. Demostración de Carga Controlada con Locust

El script `locustfile.py` contiene protecciones de seguridad estrictas que le impiden ejecutarse contra el entorno principal o contra IPs externas:

```bash
# 1. Activar venv de pruebas
source /tmp/nexustix-idempotency-test-venv/bin/activate

# 2. Configurar variables seguras de carga (solo staging loopback)
export NEXUSTIX_STAGING_HTTP=1
export NEXUSTIX_LOCUST_USERS_URL="http://127.0.0.1:18081"
export NEXUSTIX_LOCUST_EMAIL="$EMAIL_DEMO"
export NEXUSTIX_LOCUST_PASSWORD="$PASS_DEMO"
export NEXUSTIX_LOCUST_EVENT_ID=1

# 3. Ejecutar prueba pequeña (2 usuarios, spawn 1/s, duración 15 segundos)
locust -f locustfile.py --host http://127.0.0.1:18085 --headless \
  --users 2 --spawn-rate 1 --run-time 15s --stop-timeout 5 --only-summary
```

*Resultado esperado:*
- Locust ejecuta compras autenticadas con `Idempotency-Key` única por tarea.
- Muestra métricas de solicitudes por segundo (RPS), latencia (p50, p95) y tasa de éxito.
- Los rechazos HTTP 409 cuando se agota el stock se clasifican como rechazos de negocio legítimos, diferenciándolos de fallos de red 5xx.

---

## 14. Limpieza Segura de Datos Sintéticos

Para dejar el entorno limpio sin destruir volúmenes ni configuraciones:

```bash
# Eliminar el evento sintético de prueba #999999 y sus reservas
docker compose --project-name nexustix-auth-staging -f docker-compose.staging.yml exec -T eventos_db \
  psql -U postgres -d eventos_db -c "DELETE FROM reservas_stock WHERE evento_id = $EVENTO_SINTETICO; DELETE FROM eventos WHERE id = $EVENTO_SINTETICO;"

echo "Limpieza de fixtures sintéticos completada con éxito."
```

---

## 15. Solución de Errores Frecuentes

| Síntoma | Causa Probable | Solución Inmediata |
|---|---|---|
| `error while interpolating secrets... NEXUSTIX_STAGING_SECRETS_DIR is missing` | Variable no exportada en la subshell actual | Ejecutar `export NEXUSTIX_STAGING_SECRETS_DIR="$HOME/.config/nexustix-auth-staging-secrets"` |
| `HTTP 503 db: unavailable` en `/health` | Contenedor de PostgreSQL aún en fase de inicio o no saludable | Esperar 10 segundos a que pase el healthcheck (`pg_isready`) o verificar con `docker compose ps` |
| `HTTP 401 Unauthorized` al comprar | Token expirado (TTL normal = 600 s) o ausencia de header Authorization | Iniciar sesión nuevamente para obtener un JWT fresco |
| `HTTP 409 Idempotency-Key ya fue utilizada` | Se envió la misma clave con cantidad o evento distinto | Generar un nuevo UUID para compras lógicas distintas (`uuidgen`) |
| `write /home/.../agentapi: no space left on device` | Partición de `/home` saturada en Fedora | Ejecutar `df -h` y limpiar archivos temporales de `/tmp` o logs antiguos en `~/.cache` |
