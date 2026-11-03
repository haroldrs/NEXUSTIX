# NEXUSTIX: autenticación y despliegue

## Estado de esta entrega

Para validación HTTP antes de tocar el entorno actual, sigue exclusivamente [STAGING_AUTENTICACION_HTTP.md](STAGING_AUTENTICACION_HTTP.md): define proyecto, puertos, volúmenes, bases y secretos separados. Los comandos de esta guía apuntan al Compose normal y no deben usarse para probar staging.

El código requiere la revisión Alembic `20261022_01` en `usuarios_db` y `20261023_02` en `compras_db` (autenticación más persistencia de `Idempotency-Key`). La migración nueva de Compras es aditiva y conserva las compras históricas. El arranque ya no crea ni altera automáticamente el esquema de Usuarios o Compras: falla cerrado si la revisión requerida no está aplicada. Las reservas de Eventos requieren además el secreto entre servicios. **La revisión nueva no se ha aplicado a las bases actuales; no reinicies ni despliegues Compras hasta completar los pasos aprobados.**

Las cuentas que ya existían se migran con `role=comprador`, `auth_status=legacy_unclaimed` y `password_hash=NULL`. No pueden iniciar sesión. Las compras existentes conservan `usuario_id` y reciben `auth_subject=NULL`; no se les atribuye identidad autenticada. El flujo anterior de selección de ID no prueba quién realizó esas compras.

## Preparación de secretos

Genera secretos fuera del repositorio y almacénalos en el gestor aprobado. No copies las llaves o tokens en `.env`, Compose, logs ni control de versiones. Compose requiere estas variables de ruta en el entorno de despliegue:

- `NEXUSTIX_JWT_PRIVATE_KEY_FILE`: clave privada RSA usada solo por Usuarios para firmar.
- `NEXUSTIX_JWT_PUBLIC_KEY_FILE`: clave pública que verifican Usuarios, Compras y Eventos.
- `NEXUSTIX_EVENTOS_SERVICE_TOKEN_FILE`: secreto aleatorio compartido solo por Compras y Eventos.

El despliegue debe montar los archivos como secretos de solo lectura. La clave privada nunca se monta en Compras o Eventos. Ejemplo de generación en un host seguro:

```sh
umask 077
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out nexustix-jwt-private.pem
openssl pkey -in nexustix-jwt-private.pem -pubout -out nexustix-jwt-public.pem
openssl rand -hex 32 > nexustix-eventos-service-token
```

Revisa propietario y permisos de cada archivo; entrega a Compose únicamente sus rutas. En producción, TLS es obligatorio. `NEXUSTIX_AUTH_COOKIE_SECURE=false` (mapeado a `AUTH_COOKIE_SECURE` en Usuarios) solo debe usarse en desarrollo local sobre HTTP.

## Preflight antes de migrar

1. Captura y verifica copias de seguridad de las tres bases/volúmenes.
2. Comprueba duplicados de correo sin modificar filas:

   ```sql
   SELECT lower(trim(email)) AS email_normalizado, count(*)
   FROM usuarios
   GROUP BY lower(trim(email))
   HAVING count(*) > 1;
   ```

   La migración de Usuarios crea un índice único case-insensitive y se detendrá si detecta colisiones. Resuelve cualquier caso con una decisión explícita antes de aplicarla; esta entrega no normaliza ni cambia correos históricos.
3. Confirma que `usuarios_db` y `compras_db` corresponden a los esquemas verificados de la línea base. La migración baseline usa `CREATE TABLE IF NOT EXISTS` y no elimina ni vacía tablas.
4. Carga los tres archivos de secreto y las variables de ruta antes de validar la configuración de Compose.

## Orden de despliegue, aún no ejecutado

1. Construir las imágenes de Usuarios, Compras y Eventos con sus nuevas dependencias y el módulo compartido.
2. Cuando se autorice modificar esquema, aplicar una vez las migraciones desde los servicios ya conectados a la red Compose:

   ```sh
   docker compose run --rm --no-deps usuarios alembic -c alembic.ini upgrade head
   docker compose run --rm --no-deps compras1 alembic -c alembic.ini upgrade head
   ```

3. Crear el primer administrador de forma interactiva, después de migrar Usuarios:

   ```sh
   docker compose run --rm --no-deps usuarios python bootstrap_admin.py --email admin@dominio-aprobado.example
   ```

   El script solicita la contraseña dos veces sin eco, exige mínimo 12 caracteres, guarda Argon2id, bloquea la creación concurrente con advisory lock y se niega a crear un segundo administrador. No corre al iniciar el servicio. No puede reclamar una cuenta legacy ni asignarle rol.
4. Solo después de verificar migraciones, secretos y backup, desplegar/recrear los servicios. No uses `down -v`.
5. Ejecutar las pruebas de aceptación contra un entorno de staging aislado antes de habilitar el portal.

## Pruebas automatizadas aisladas

`requirements-test.txt` fija las dependencias de prueba sin instalar el controlador PostgreSQL, ya que la suite usa SQLite en memoria. En un entorno virtual aislado ejecuta `python -m pip install -r requirements-test.txt` y `python -m pytest -q tests/test_authentication.py`. La suite llama directamente a los handlers/dependencias de autorización; no levanta servidores ni verifica transporte HTTP. El host disponible usa Python 3.14, por lo que Pydantic/SQLAlchemy de prueba son más nuevos que los fijados para las imágenes Python 3.11. No lee credenciales de Compose ni usa las bases actuales; crea llaves temporales y una credencial de servicio desechable.

## Tokens y autorización

Usuarios emite access JWT RS256 de 10 minutos por defecto. La cookie refresh es `HttpOnly`, `Secure` por defecto, `SameSite=Strict`, se guarda como hash en `refresh_sessions` y rota en cada uso. Refresh/logout validan Origin permitido y `X-CSRF-Protection: 1`. La lista CORS usa `FRONTEND_ORIGINS` explícitos. Compras valida el JWT localmente y Eventos valida los JWT para tareas de administrador; llamadas de inventario entre Compras y Eventos usan `X-Service-Token` con comparación de tiempo constante.

El cierre de sesión revoca el refresh token, pero un access token emitido sigue siendo válido hasta su expiración (10 minutos por defecto). No hay aún rate limiting distribuido para login/registro, recuperación de contraseña, verificación de correo ni alertas de credential stuffing; deben agregarse antes de exponer el registro a Internet a escala.

`POST /compras` requiere `Idempotency-Key` (1–128 caracteres ASCII visibles). Compras reserva la clave globalmente en PostgreSQL y almacena el hash del cuerpo y la respuesta junto con la compra; la misma clave, identidad y cuerpo reproduce la respuesta sin repetir la reserva. Reutilizarla con otro sujeto o cuerpo devuelve `409`. No elimines filas de `purchase_idempotency`: al borrarlas se permitiría ejecutar nuevamente una clave antigua. El frontend conserva la clave pendiente solo en memoria; si se recarga la página después de un timeout, el usuario debe consultar su historial antes de iniciar otra compra, porque el cliente no puede recuperar esa clave anterior.

## Reversión

Las migraciones son aditivas. Cada downgrade de autenticación comprueba primero que no existan sesiones, cuentas autenticadas o compras correlacionadas y se detiene si los borraría. El baseline nunca elimina tablas. Si ya hay uso real, vuelve a la versión anterior de aplicación manteniendo las columnas/tablas nuevas; no reviertas eliminando identidad o historial.
