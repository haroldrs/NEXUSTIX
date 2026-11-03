# Demostración distribuida NEXUSTIX

Esta guía usa exclusivamente el staging aislado (`nexustix-auth-staging`, puertos
18080–18085). No ejecutes los comandos sobre el Compose principal. Las pruebas
HTTP crean cuentas y compras sintéticas en las bases de staging.

## 1. Preparación y estado

Confirma que la configuración apunta al directorio de secretos de staging y
valida sin mostrar la configuración expandida:

```sh
export NEXUSTIX_STAGING_SECRETS_DIR="$HOME/.config/nexustix-auth-staging-secrets"
docker compose -p nexustix-auth-staging -f docker-compose.staging.yml config --quiet
docker compose -p nexustix-auth-staging -f docker-compose.staging.yml ps
```

Las bases de datos deben mostrar healthy y las tres APIs, las dos réplicas,
Nginx y el frontend deben estar iniciados. No uses `down --volumes` para
reiniciar la demostración.

## 2. Pruebas automatizadas

Las pruebas unitarias usan SQLite temporal y dobles HTTP para Eventos; no
acceden a bases de datos ni contenedores existentes:

```sh
python -m pytest -q tests/test_authentication.py
```

Las pruebas HTTP crean compradores sintéticos, realizan compras en staging y
verifican autenticación, sobreventa, idempotencia en ambas réplicas, requests
simultáneos, historial, auditoría y credencial entre servicios:

```sh
NEXUSTIX_STAGING_HTTP=1 python -m pytest -q -s tests/staging/test_auth_http.py
```

Resultado esperado para el caso entre réplicas: una llamada a Compras1 y su
repetición en Compras2 devuelven el mismo `id` y `operacion_id`; la segunda
incluye `Idempotency-Replayed: true`. En la carrera, ambas respuestas deben
referir el mismo registro y exactamente una debe marcarse replay. La prueba de
sobreventa solicita una cantidad superior al stock observado, recibe HTTP 409
y comprueba que el stock no cambió.

Para la prueba de ventas globales y auditoría se puede proporcionar el correo
del administrador sintético y la ruta al archivo local de contraseña, sin
pasarla por la línea de comandos:

```sh
NEXUSTIX_STAGING_HTTP=1 \
NEXUSTIX_STAGING_ADMIN_EMAIL=qa-admin-staging@example.com \
NEXUSTIX_STAGING_ADMIN_PASSWORD_FILE="$NEXUSTIX_STAGING_SECRETS_DIR/qa-admin-password" \
python -m pytest -q -s tests/staging/test_auth_http.py
```

No copies tokens, cookies, contraseñas ni encabezados `Authorization` a las
capturas. Registra salida de pruebas, códigos HTTP, IDs sintéticos (no datos de
usuarios reales), `docker compose ps` y los cuerpos redactados de auditoría.

## 3. Caída y retorno de una réplica

Hazlo solo en una ventana de demostración de staging. No detengas otros
servicios. Antes de la caída, confirma ambas réplicas healthy y guarda las
salidas de las pruebas de idempotencia. Detén Compras1:

```sh
docker compose -p nexustix-auth-staging -f docker-compose.staging.yml stop compras1
curl --fail --silent http://localhost:18085/health
docker compose -p nexustix-auth-staging -f docker-compose.staging.yml ps compras1 compras2 compras_lb
```

La respuesta debe identificar la réplica superviviente. Nginx reparte nuevas
solicitudes; un POST que ya alcanzó la aplicación no se vuelve a enviar
automáticamente como operación no idempotente. Si el cliente recibe timeout,
debe repetir el mismo cuerpo y `Idempotency-Key`; no genere otra clave hasta
resolver el resultado. Compras1 se restaura así:

```sh
docker compose -p nexustix-auth-staging -f docker-compose.staging.yml start compras1
docker compose -p nexustix-auth-staging -f docker-compose.staging.yml ps compras1 compras2 compras_lb
```

La réplica reconstruye su conexión al PostgreSQL compartido. Comprueba que el
resultado de una clave repetida siga siendo el original y revisa la auditoría
administrativa. Esto prueba una caída de proceso/réplica, no tolerancia ante la
caída de PostgreSQL o Eventos; ambos siguen siendo dependencias únicas.

## 4. Resultado ambiguo y recuperación

Un timeout de reserva o respuesta 5xx de Eventos deja la fila idempotente en
`processing` con el mismo `operacion_id`. Reintentar con la misma clave vuelve a
consultar/reservar idempotentemente esa operación. Compras no libera la reserva
por un timeout. La ruta administrativa `POST /compras/recuperar-servidor`
concilia reservas pendientes contra compras persistidas; si otra solicitud aún
tiene bloqueada la reclamación, la omite y reporta
`reservas_omitidas_por_operacion_en_curso`. No interpretes un resultado
`recuperacion_incompleta` como integridad restaurada.

La ruta de recuperación sí puede confirmar o liberar reservas y requiere rol
administrador. Ejecútala únicamente en staging y después de revisar las filas
pendientes y las compras correlacionadas. La ruta de reconciliación de stock en
Eventos es una operación separada; su modo de vista previa no escribe, y
`aplicar=true` no forma parte de esta demostración.

## 5. Auditoría y evidencia

Con sesión administradora, consulta:

```sh
read -r -s -p "Access token: " ACCESS_TOKEN; echo
curl --silent -H "Authorization: Bearer ${ACCESS_TOKEN}" http://localhost:18085/auditoria/consistencia
curl --silent -H "Authorization: Bearer ${ACCESS_TOKEN}" http://localhost:18085/compras/journal
unset ACCESS_TOKEN
```

`ACCESS_TOKEN` debe vivir solo en el entorno de la terminal actual y no debe
imprimirse ni guardarse en el historial. La auditoría debe
mostrar stock esperado, ventas confirmadas, reservas pendientes, operaciones
huérfanas y la comparación de compras confirmadas con registros `COMMIT` del
journal. El journal es best-effort y se informa separadamente; no reemplaza a
`compras_db` ni al WAL de PostgreSQL.

La evidencia de la demostración debe incluir: versión de código/imagen,
revisiones Alembic de staging, estado de servicios antes/durante/después,
resultados individuales de pytest, replay entre Compras1/Compras2, carrera
concurrente, intento de sobreventa sin cambio de stock y auditoría final. No se
declara tolerancia a fallos integral: Usuarios, Eventos, las bases PostgreSQL y
el host/Compose siguen siendo puntos únicos de fallo.

## 6. Respaldo y restauración aislada

El procedimiento existente está en [RESPALDO_RESTAURACION.md](RESPALDO_RESTAURACION.md).
El respaldo de las tres bases principales requiere una autorización y ventana
separadas porque lee datos de producción. Para ensayar integridad/restauración,
usa `verify` con la copia existente o con una copia autorizada; el destino de
restauración es temporal y aislado. Nunca proporciones una URL de base principal
al verificador ni restaures sobre una base principal. Un respaldo solo se
considera verificado si `verification.json` termina en `verified` y todas las
comparaciones de estadísticas coinciden.
