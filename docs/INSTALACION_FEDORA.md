# Instalación desde cero en Fedora Linux

Procedimiento para una PC donde NEXUSTIX nunca se ha ejecutado. No depende de
imágenes, volúmenes ni secretos de otra máquina: todo se construye y genera
localmente. Pensado para Fedora 40–43 con Docker CE. **Este procedimiento
todavía no se ha ejecutado en Fedora**: la máquina donde se preparó la entrega
no tenía Docker (ver `PRUEBAS_RESILIENCIA.md`). Registre cualquier desviación.

## 1. Requisitos

- Fedora 40 o superior, 4 GB de RAM (8 GB recomendados), 5 GB libres.
- Docker Engine con Docker Compose v2 **≥ 2.20** (se usa `depends_on.required`).
- `python3` (incluido en Fedora), `openssl` y `git`/`unzip`.
- Acceso a Internet la primera vez (imágenes `python:3.11-slim`,
  `postgres:15-alpine`, `nginx:1.28-alpine` y paquetes de PyPI).
- Opcional para pruebas: `nodejs` (prueba del frontend) y un entorno virtual
  de Python.

## 2. Instalar y verificar Docker

```bash
sudo dnf -y install dnf-plugins-core openssl unzip python3 nodejs
# Fedora 41 o posterior (dnf5):
sudo dnf config-manager addrepo --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo
# Fedora 40 (dnf4):  sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"     # cierre sesión y vuelva a entrar (o: newgrp docker)
docker version && docker compose version
```

> Si ya usa `moby-engine` de Fedora con SELinux activo en Docker, también
> funciona: `generar_secretos.py` etiqueta la carpeta de secretos con
> `container_file_t`, y el proyecto no monta archivos del repositorio (todo
> se copia en las imágenes).

## 3. Preparar el proyecto

```bash
mkdir -p ~/proyectos && cd ~/proyectos
unzip ~/Descargas/NEXUSTIX.zip      # crea ~/proyectos/NEXUSTIX
cd NEXUSTIX
python3 scripts/nx.py demo preflight
```

`preflight` comprueba Python, Docker, Compose ≥ 2.20, acceso al daemon,
SELinux, openssl, secretos, puertos libres y espacio en disco. La primera vez
indicará que faltan los secretos.

## 4. Variables de entorno

No hace falta un `.env`: `scripts/nx.py` fija proyecto, archivos Compose y
carpeta de secretos. Las variables opcionales están documentadas en
`config/demo.env.example` (p. ej. `NEXUSTIX_BIND_ADDRESS=0.0.0.0` para
abrir los puertos a la red).

## 5. Generar claves JWT y secretos

```bash
python3 scripts/nx.py demo secretos          # ~/.config/nexustix/demo-secrets (0700/0600)
```

Genera claves RS256 de 3072 bits, contraseñas distintas por base, la
credencial interna Compras→Eventos, la contraseña de replicación, la del
administrador inicial y `frontend-config.js`. No muestra valores ni
sobrescribe una carpeta existente. Para acceso desde otra PC use
`--public-host <IP-de-esta-PC>`.

## 6–10. Construir, inicializar, migrar, arrancar y comprobar salud

```bash
python3 scripts/nx.py demo up
```

Esto ejecuta `docker compose -p nexustix -f docker-compose.yml up -d --build --wait`:

1. construye las seis imágenes desde el código (`nexustix/*:local`);
2. PostgreSQL inicializa los tres volúmenes vacíos con las contraseñas generadas;
3. Usuarios y Compras aplican sus migraciones Alembic protegidas antes de
   arrancar; Eventos crea su esquema y siembra los cinco eventos base;
4. el orden lo marcan los healthchecks (`service_healthy`);
5. termina cuando los nueve contenedores están *healthy* y muestra `status`.

Comprobación manual:

```bash
python3 scripts/nx.py demo status           # contenedores, health HTTP y rol de cada PostgreSQL
python3 scripts/nx.py demo verificar        # solo lectura: salud de servicios y catálogo
```

## 11. Frontend y administrador

```bash
python3 scripts/nx.py demo admin --email admin@example.com
```

La contraseña del administrador está en
`~/.config/nexustix/demo-secrets/admin-password` (léala con `cat` solo cuando
la necesite). Abra `http://localhost:8080`, cree una cuenta compradora con
"Crear cuenta" y compre desde "Reserva tu lugar". Con la cuenta
administradora se habilitan "Administración" y "Observabilidad".

## 12. Compra de prueba automatizada (entorno de pruebas)

Las verificaciones que escriben datos se hacen en el entorno de pruebas
aislado, no en demo:

```bash
python3 scripts/nx.py pruebas secretos
python3 scripts/nx.py pruebas up
python3 scripts/nx.py pruebas verificar --compra
```

`verificar --compra` registra un comprador sintético, compra por el
balanceador, repite la petición (replay), reutiliza la clave con otro cuerpo
(409) y comprueba en `compras_db` y `eventos_db` una sola compra, la clave
`completed`, la reserva `confirmada` y el stock −1.

## 13–14. Pruebas automatizadas y de resiliencia

```bash
python3 -m venv ~/.venvs/nexustix-test
source ~/.venvs/nexustix-test/bin/activate
python -m pip install -r requirements-test.txt
source config/pruebas.env.example                 # variables del entorno de pruebas
python scripts/ejecutar_pruebas.py --capas unit postgres resiliencia staging
```

- `unit`: unitarias y estáticas (sin Docker).
- `postgres`: integración sobre un PostgreSQL 15 desechable que el script crea
  y elimina (solo ese contenedor, etiquetado `org.nexustix.purpose=pg-tests`).
  Las pruebas de replicación real y de `pg_dump`/`pg_restore` necesitan además
  los binarios en el host: `sudo dnf install postgresql-server` y
  `export NEXUSTIX_TEST_PG_BIN=/usr/bin` (si no, quedan BLOQUEADAS con su motivo).
- `resiliencia`: caídas reales sobre `nexustix-auth-staging` (ver
  `PRUEBAS_RESILIENCIA.md`). Para `test_r17` levante pruebas con
  `NEXUSTIX_STAGING_RECOVERY_INTERVAL_SECONDS=10`; para `test_r21`, ejecute
  antes `python3 scripts/db_ha.py pruebas enable`.
- `staging`: suites HTTP históricas (necesitan el entorno sin réplicas).

Los resultados quedan en `resultados/<fecha>/RESUMEN.md` (APROBADA, FALLIDA,
BLOQUEADA, PENDIENTE) junto con JUnit XML y la salida completa. La suite
termina comprobando que el entorno demo no cambió.

## 15. Respaldo y restauración

```bash
python3 scripts/nx.py demo respaldo --destino ~/nexustix-backups/demo-$(date +%Y%m%d-%H%M)
```

Pausa escrituras unos segundos, vuelca las tres bases, verifica en un
PostgreSQL temporal aislado y deja `manifest.json`, `SHA256SUMS` y
`verification.json`. Restauración ante pérdida de datos: `BASES_DE_DATOS.md` §4.

## 16. Parada segura

```bash
python3 scripts/nx.py demo stop                     # conserva contenedores y volúmenes
python3 scripts/nx.py demo start                    # o "up" para reconstruir
python3 scripts/nx.py demo down --confirmar nexustix   # elimina contenedores, NO volúmenes
```

Nunca use `docker compose down -v` ni `docker system prune --volumes`.

## Instalación inicial vs actualización de una instalación existente

| | Instalación inicial | Actualización |
|---|---|---|
| Secretos | `nx.py demo secretos` | Demo anterior (contraseña `postgres`): `python3 scripts/generar_secretos.py --entorno demo --legacy-db-password`. Pruebas anterior (`~/.config/nexustix-auth-staging-secrets` ya existe): `python3 scripts/nx.py pruebas secretos --completar` añade solo los archivos nuevos y reutiliza las contraseñas que ya tienen los volúmenes |
| Volúmenes | `nexustix_*` nuevos | Reutilice los antiguos con `export NEXUSTIX_VOLUME_PREFIX=<prefijo>` (p. ej. `project` si la carpeta se llamaba `Project`) |
| Antes de arrancar | — | **Respalde** con la versión anterior y verifique el respaldo |
| Migraciones | crean el esquema | `upgrade` aditivo automático; si la base no tiene `alembic_version` se detiene hasta que defina `NEXUSTIX_ALLOW_LEGACY_BASELINE=1` |
| Eventos | siembra los 5 eventos | añade `stock_inicial` y lo rellena solo para los eventos base |
| Contenedores antiguos | — | Los de la versión anterior tenían `container_name` fijo (`usuarios_db`, …); deténgalos (`docker stop usuarios_db …`) antes de `up` para liberar puertos. No borre sus volúmenes. |

## Problemas conocidos

- **Permiso denegado al montar secretos** (SELinux): `chcon -R -t container_file_t ~/.config/nexustix`.
- **Puerto en uso**: `preflight` lo indica; detenga el proceso o la instalación anterior.
- **`docker: permission denied`**: falta el grupo `docker` (paso 2) o cerrar sesión.
- **Compose < 2.20**: el overlay de replicación falla (`required`); actualice `docker-compose-plugin`.
- **Firewall en despliegue de red**: `sudo firewall-cmd --add-port=8080/tcp --add-port=8001-8005/tcp` (y `--permanent`).
- **Firefox y cookies**: abra el frontend con el mismo host que figura en
  `FRONTEND_ORIGINS` (por defecto `localhost`); con otro host el refresco de
  sesión falla y hay que volver a iniciar sesión tras 10 minutos.
