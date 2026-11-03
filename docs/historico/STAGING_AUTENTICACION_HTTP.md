# Staging HTTP aislado para autenticación

Este procedimiento es para la validación HTTP de autenticación. **No se ha iniciado staging ni se han ejecutado migraciones**. El Compose dedicado usa el proyecto `nexustix-auth-staging`, puertos loopback `18080–18085`, tres volúmenes nombrados `nexustix_auth_staging_*`, contraseñas de DB únicas y claves/credenciales externas al repositorio. No define `container_name` ni reutiliza los volúmenes del Compose normal.

La URL de conexión de cada app/Alembic se lee desde `DATABASE_URL_FILE`; la contraseña no se coloca en una variable de entorno. El Compose normal conserva compatibilidad con su `DATABASE_URL` existente. La clave privada JWT solo se monta en Usuarios. El config JS del staging contiene únicamente URLs públicas locales y se sirve por una ruta Nginx específica. El Nginx staging mapea `/app.js` a `frontend/app.staging.js`; `frontend/app.js` e `index.html` se mantienen para el portal principal que comparte el montaje de archivos.

## Preparación

Usa una ubicación persistente protegida fuera del proyecto para las claves y contraseñas. El generador se niega a escribir dentro del repositorio, no sobreescribe directorios existentes, fija permisos `0700/0600` y no imprime valores:

```sh
python3 scripts/prepare_staging.py --directory /ruta/segura/nexustix-auth-staging-secrets
export NEXUSTIX_STAGING_SECRETS_DIR=/ruta/segura/nexustix-auth-staging-secrets
```

No guardes el directorio en Git, no lo montes en el frontend y no copies sus valores a la terminal. La configuración de staging usa HTTP solo en loopback, con `Secure=false` para la cookie de prueba; no es configuración de producción.

## Comandos al autorizar el staging

Primero valida Compose y construye las etiquetas exclusivas de staging. `build` no arranca servicios:

```sh
docker compose -p nexustix-auth-staging -f docker-compose.staging.yml config --quiet
docker compose -p nexustix-auth-staging -f docker-compose.staging.yml build usuarios eventos compras1 compras2
```

Arranca únicamente las tres bases vacías de staging y espera su healthcheck:

```sh
docker compose -p nexustix-auth-staging -f docker-compose.staging.yml up -d --wait usuarios_db eventos_db compras_db
```

Las dos migraciones se ejecutan con el Compose aislado, sobre sus bases propias:

```sh
docker compose -p nexustix-auth-staging -f docker-compose.staging.yml run --rm --no-deps usuarios alembic -c alembic.ini upgrade head
docker compose -p nexustix-auth-staging -f docker-compose.staging.yml run --rm --no-deps compras1 alembic -c alembic.ini upgrade head
```

El administrador sintético de staging ya existe (`qa-admin-staging@example.com`). No ejecutes el bootstrap ni crees otro administrador para repetir estas pruebas. La contraseña permanece en el archivo local protegido `qa-admin-password`; pásalo a la suite mediante `NEXUSTIX_STAGING_ADMIN_PASSWORD_FILE` para evitar mostrarla.

Después inicia las APIs, el balanceador y el frontend:

```sh
docker compose -p nexustix-auth-staging -f docker-compose.staging.yml up -d usuarios eventos compras1 compras2 compras_lb frontend
```

Comprueba salud sin imprimir secretos:

```sh
curl --fail --retry 20 --retry-connrefused http://localhost:18081/health
curl --fail --retry 20 --retry-connrefused http://localhost:18082/health
curl --fail --retry 20 --retry-connrefused http://localhost:18085/health
```

Ejecuta la suite HTTP desde el host. El correo del administrador es una variable no secreta; la suite lee la contraseña desde el archivo protegido y no la muestra:

```sh
export NEXUSTIX_STAGING_HTTP=1
export NEXUSTIX_STAGING_ADMIN_EMAIL=qa-admin-staging@example.com
export NEXUSTIX_STAGING_ADMIN_PASSWORD_FILE="$HOME/.config/nexustix-auth-staging-secrets/qa-admin-password"
python3 -m pytest -q -s tests/staging/test_auth_http.py
```

Las pruebas generan compradores y correos `example.com` únicos, aceptados por el validador. Solo escriben en las bases staging. Una prueba expira el refresh token del usuario sintético con un `UPDATE` filtrado por el SHA-256 de su token y ejecutado en el contenedor `usuarios_db` del proyecto staging. La prueba de replay confirma que la reutilización concurrente de una cookie revoca su familia; el frontend evita que sus solicitudes concurrentes compartan esa cookie simultáneamente.

La configuración Compose normal de staging usa access tokens de 600 segundos (10 minutos) y refresh tokens/cookie de 1 día (`Max-Age=86400`). Para comprobaciones automatizadas usa `docker-compose.staging.tests.yml`, que reduce únicamente el TTL de access a 15 segundos; las pruebas de expiración siguen rápidas y el token conserva su expiración real.

Antes de ejecutar la suite con TTL corto, recrea solo la API Usuarios de staging con el override de pruebas (sin tocar bases ni volúmenes):

```sh
docker compose -p nexustix-auth-staging -f docker-compose.staging.yml -f docker-compose.staging.tests.yml up -d --no-deps --force-recreate usuarios
```

Ejecuta la suite HTTP y las pruebas de estado del frontend. Una prueba reutiliza dos solicitudes protegidas desde un mismo cliente y verifica una única rotación real contra staging; otra prueba separada comprueba que el backend detecta y revoca un replay de refresh:

```sh
NEXUSTIX_STAGING_HTTP=1 NEXUSTIX_STAGING_ADMIN_EMAIL=qa-admin-staging@example.com NEXUSTIX_STAGING_ADMIN_PASSWORD_FILE="$HOME/.config/nexustix-auth-staging-secrets/qa-admin-password" python3 -m pytest -q -s tests/staging/test_auth_http.py
node --test tests/staging/frontend_auth_state.test.js
```

Al terminar, devuelve Usuarios al TTL manual de 10 minutos, recreando exclusivamente ese servicio:

```sh
env -u NEXUSTIX_STAGING_ACCESS_TOKEN_TTL_SECONDS docker compose -p nexustix-auth-staging -f docker-compose.staging.yml up -d --no-deps --force-recreate usuarios
```

La suite genera compradores sintéticos con correos `example.com`, aceptados por el validador. Solo escribe en las bases staging. Una prueba expira el refresh token de un usuario sintético con un `UPDATE` filtrado por el hash de su token. El cierre de sesión borra la cookie y revoca el refresh; un access token ya emitido sigue válido hasta su `exp`.

Abre `http://localhost:18080` para una comprobación manual. El access token solo vive en memoria JavaScript; el refresh se transmite como cookie `HttpOnly`, `Path=/auth`, `SameSite=Strict`. Las pruebas estáticas comprueban que el frontend no usa `localStorage`, `sessionStorage` ni `document.cookie`.

## Destruir solo staging (cuando se autorice)

Verifica el proyecto exacto antes de ejecutar el comando. El `down` incluye únicamente `docker-compose.staging.yml`; `--volumes` elimina los volúmenes nombrados `nexustix_auth_staging_*` declarados allí, no `usuarios_data`, `eventos_data` ni `compras_data` del Compose de desarrollo:

```sh
docker compose -p nexustix-auth-staging -f docker-compose.staging.yml down --volumes --remove-orphans
```

Después, si también se autoriza borrar las claves externas, usa el directorio exacto que creó `prepare_staging.py`. El script requiere el nombre de proyecto como confirmación y rechaza el repositorio o un conjunto distinto de archivos:

```sh
python3 scripts/destroy_staging_secrets.py --directory "$NEXUSTIX_STAGING_SECRETS_DIR" --confirm nexustix-auth-staging
```

Opcionalmente elimina solo las tres imágenes de aplicación con etiqueta exclusiva de staging (no borres las imágenes base `postgres` o `nginx`, que pueden compartirse):

```sh
docker image rm nexustix-staging/usuarios:auth nexustix-staging/eventos:auth nexustix-staging/compras:auth
```
