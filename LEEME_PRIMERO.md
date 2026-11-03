# Preparación para GitHub y tres PC — 03/11/2026

La entrada vigente es [README.md](README.md), con
[instalación para compañeros](docs/INSTALACION_EQUIPO.md),
[auditoría](docs/AUDITORIA_REPOSITORIO.md) y
[plan de tres PC físicas](docs/PLAN_DESPLIEGUE_3_PC.md).
Las instrucciones siguientes se conservan como historia: sus comandos de
instalación, fallos o reset no deben ejecutarse sobre los entornos actuales.
El ejecutor de pruebas ahora exige opt-in adicional para infraestructura.

---

# Continuación en Fedora — 3 de noviembre de 2026

El nuevo entorno **nexustix-distributed-lab** usa `http://localhost:28080`,
secretos propios y volúmenes distintos. El staging que ya existía se conserva.
Consulta [EVOLUCION_DISTRIBUIDA](docs/EVOLUCION_DISTRIBUIDA.md) para el flujo de
pedidos/pagos/QR y [resultados actuales](docs/RESULTADOS_DISTRIBUIDO.md).
Los archivos de [VMs](despliegue-vm/README.md) están preparados; no se desplegaron.
No ejecutar los comandos históricos de reinicio/reset de staging de abajo
para probar esta evolución. La sección siguiente describe la entrega anterior.

---

# Léeme primero — indicaciones para continuar NEXUSTIX

Este ZIP es tu proyecto NEXUSTIX revisado y mejorado. Se conservó la
arquitectura (Usuarios, Eventos, Compras ×2 detrás de Nginx, tres PostgreSQL)
y los contratos de la API. Lo que cambió, por qué y con qué evidencia está en
[`docs/INFORME_FINAL.md`](docs/INFORME_FINAL.md).

## Qué está comprobado y qué te toca comprobar

| Ya ejecutado (en Windows, sin Docker) | Te toca ejecutar en Fedora (con Docker) |
|---|---|
| 103 pruebas unitarias y de configuración | Arranque real de los Compose y construcción de imágenes |
| 11 pruebas sobre PostgreSQL 15.19 real: saga Compras↔Eventos, concurrencia, idempotencia, migraciones, respaldo y replicación con promoción | 28 pruebas de resiliencia con caídas reales de contenedores (`tests/resiliencia`) |
| | Suites antiguas de `tests/staging`, Locust y la orquestación Docker de la replicación |

Nada que requiera Docker se ha ejecutado todavía. Si algo falla, es esperable
que sea configuración de contenedores; los resultados quedan registrados para
corregirlo.

## Lo más importante antes de tocar nada

1. **Dos entornos separados.** `demo` (puertos 8080 y 8001–8005) es lo que
   presentas. `pruebas` (18080–18085, proyecto `nexustix-auth-staging`) es
   donde se rompen cosas a propósito. Cada comando lleva el entorno escrito:
   `python3 scripts/nx.py demo …` o `python3 scripts/nx.py pruebas …`.
2. **Nunca** `docker compose down -v` ni `docker system prune --volumes`: borran
   las bases. Para parar: `python3 scripts/nx.py <entorno> stop`.
3. **Secretos fuera del proyecto.** Se generan en `~/.config/…` con
   `nx.py <entorno> secretos`. No los copies al repositorio ni los pegues en
   chats o capturas.
4. Las pruebas destructivas (caídas, promoción de base de datos) se hacen
   **solo** en `pruebas`. La suite termina comprobando que `demo` no cambió.

## Instalación en Fedora (PC nueva)

```bash
# 1. Docker (Fedora 41 o posterior; en Fedora 40: dnf config-manager --add-repo …)
sudo dnf -y install dnf-plugins-core openssl unzip python3 nodejs
sudo dnf config-manager addrepo --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo
sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"        # cierra sesión y vuelve a entrar

# 2. Proyecto
mkdir -p ~/proyectos && cd ~/proyectos
unzip ~/Descargas/NEXUSTIX_entrega_2026-10-25.zip
cd NEXUSTIX

# 3. Entorno demo
python3 scripts/nx.py demo preflight     # te dice qué falta
python3 scripts/nx.py demo secretos
python3 scripts/nx.py demo up            # construye y espera a que todo esté healthy
python3 scripts/nx.py demo admin --email admin@example.com
python3 scripts/nx.py demo status
```

Abre `http://localhost:8080`. La contraseña del administrador está en
`~/.config/nexustix/demo-secrets/admin-password`. Detalle y problemas
conocidos (SELinux, firewall, puertos): [`docs/INSTALACION_FEDORA.md`](docs/INSTALACION_FEDORA.md).

## Probar que todo funciona

```bash
# Entorno de pruebas aislado
python3 scripts/nx.py pruebas secretos
python3 scripts/nx.py pruebas up
python3 scripts/nx.py pruebas verificar --compra     # compra real + comprobación en ambas bases

# Suites automáticas
python3 -m venv ~/.venvs/nexustix-test && source ~/.venvs/nexustix-test/bin/activate
python -m pip install -r requirements-test.txt
source config/pruebas.env.example
sudo dnf -y install postgresql-server && export NEXUSTIX_TEST_PG_BIN=/usr/bin   # replicación y respaldo reales
python scripts/ejecutar_pruebas.py --capas unit postgres resiliencia
```

El resultado queda en `resultados/<fecha>/RESUMEN.md` con cada prueba como
APROBADA, FALLIDA, BLOQUEADA o PENDIENTE. Opcionales:

```bash
NEXUSTIX_STAGING_RECOVERY_INTERVAL_SECONDS=10 python3 scripts/nx.py pruebas up   # para test_r17
python3 scripts/db_ha.py pruebas enable        # para test_r21 (replicación con Docker)
python scripts/ejecutar_pruebas.py --capas resiliencia
python3 scripts/db_ha.py pruebas disable       # antes de las suites antiguas
python scripts/ejecutar_pruebas.py --capas staging
```

**Si algo sale FALLIDA:** guarda la carpeta `resultados/<fecha>/` completa y la
salida de `python3 scripts/nx.py pruebas status` y
`python3 scripts/nx.py pruebas logs <servicio>`. Revisa que no aparezcan
contraseñas antes de compartirlos. Con eso se puede corregir con precisión.

## Si en esa PC ya existía una instalación anterior

- **Entorno de pruebas anterior** (`~/.config/nexustix-auth-staging-secrets`):
  no generes secretos nuevos (no coincidirían con las contraseñas guardadas en
  los volúmenes). Completa la carpeta existente:
  `python3 scripts/nx.py pruebas secretos --completar`.
- **Entorno principal anterior** (contraseña `postgres`, volúmenes
  `<carpeta>_usuarios_data`): respalda primero, luego
  `python3 scripts/generar_secretos.py --entorno demo --legacy-db-password` y
  `export NEXUSTIX_VOLUME_PREFIX=<carpeta-en-minúsculas>`. Detén antes los
  contenedores viejos con nombre fijo (`docker stop usuarios_db …`) sin borrar
  sus volúmenes. Tabla completa en `docs/INSTALACION_FEDORA.md`.

## Para la presentación

Guion paso a paso, con comandos y evidencias que capturar:
[`docs/GUIA_DEMOSTRACION.md`](docs/GUIA_DEMOSTRACION.md). El día anterior: levanta
ambos entornos, ejecuta las pruebas y haz un respaldo de demo
(`python3 scripts/nx.py demo respaldo --destino ~/nexustix-backups/antes-de-la-demo`).

Qué afirmar y qué no:

| Sí | No |
|---|---|
| Failover entre dos réplicas de Compras con reintento seguro por clave idempotente | Alta disponibilidad de extremo a extremo |
| Saga con reserva, confirmación, compensación y recuperación | Transacción ACID distribuida |
| Replicación PostgreSQL con promoción manual y aislamiento del nodo antiguo | Failover automático de la base de datos |
| Docker reinicia procesos caídos | Tolera la caída de la computadora |

## Mapa de la documentación

| Documento | Para qué |
|---|---|
| `README.md` | Visión general y comandos frecuentes |
| `docs/ARQUITECTURA.md` | Componentes, flujo de compra, ventanas de fallo |
| `docs/BASES_DE_DATOS.md` | Migraciones, respaldo, restauración, replicación |
| `docs/PRUEBAS_RESILIENCIA.md` | Matriz de pruebas, comandos y resultados |
| `docs/GUIA_DEMOSTRACION.md` | Guion de la presentación |
| `docs/LIMITACIONES.md` | Lo que falta y los riesgos |
| `docs/ARQUITECTURA_DOS_PC.md` | Cómo se repartiría en dos equipos |
| `docs/historico/`, `legacy/` | Versiones anteriores; no usar |
