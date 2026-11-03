# Entrega: publicación revisada y despliegue LAN mínimo

Fecha: 03/11/2026. Revisión para primer commit local; no se publicó en GitHub ni
se desplegó en tres PC. No se asignaron IPs ni se crearon credenciales para una
LAN real. Los únicos paquetes generados durante las pruebas son sintéticos y
no deben reutilizarse como instalación.

## Publicación

Se revisaron las 81 coincidencias originales: **11 credenciales predeterminadas,
29 datos sintéticos, 41 falsos positivos; ningún secreto de instalación real
identificado**. Se eliminaron las 11 coincidencias de riesgo y tres asignaciones
adicionales de contraseña sin comillas en Compose legacy. El historial detallado
está en [HALLAZGOS_PUBLICACION.md](HALLAZGOS_PUBLICACION.md).

Las API y migraciones requieren `DATABASE_URL_FILE` o `DATABASE_URL`. El modo
legacy de generación exige `NEXUSTIX_LEGACY_DB_PASSWORD_FILE`; no contiene una
contraseña fija y no cambia bases existentes. Se ajustaron dos llamadas de los
controladores VM al nuevo contrato del lector; no se aplicaron operaciones VM.

El escáner posterior inspeccionó **213 archivos candidatos** y conserva
**80 coincidencias revisadas**, sin pendientes ni archivos ilegibles. El índice
también se escaneó, con 213 archivos y cero pendientes; no contiene archivos
ignorados. Las excepciones son por hash de texto/ruta/tipo, con motivo;
no se excluyeron directorios de código del análisis. Cualquier cambio reabre el
hallazgo. El registro no permite aprobar bloques privados ni tokens completos.
El escaneo del índice comprueba la versión que realmente entrará al commit.
Esto es una revisión estática satisfactoria de los riesgos detectados, no una
certificación de seguridad ni un análisis de credenciales externas.

## Archivos de esta fase

| Cambio | Archivos |
|---|---|
| Conexiones obligatorias | `shared/db_engine.py`, `shared/run_migrations.py`, `usuarios/database.py`, `eventos/database.py`, `compras/database.py` |
| Compatibilidad sin defaults | `scripts/generar_secretos.py`, `config/README.md`, tres Compose de `legacy/multi-laptop/`, ejemplo histórico `docs/historico/ARQUITECTURA_DOS_PC_2026-10-24.md` |
| Adaptación de llamadas VM | `despliegue-vm/scripts/vm.py`, `despliegue-vm/scripts/verificar.py` |
| Publicación y exclusiones | `.gitignore`, `.dockerignore`, `scripts/revisar_publicacion.py`, nuevo `config/publicacion-revisiones.json` |
| Infraestructura LAN nueva | `despliegue-pc/compose.pc1.yml`, `compose.pc2.yml`, `compose.pc3.yml`, `lan.py`, `simular.py`, `README.md`, `postgres/Dockerfile`, `postgres/entrypoint.sh`, `postgres/init-app.sh` |
| Pruebas nuevas | `tests/lan/test_lan_preparacion.py`, `tests/lan/simular_balanceador.sh`; selección ampliada en `pytest.ini` y `scripts/ejecutar_pruebas.py` |
| Guías actualizadas | README raíz, `AUDITORIA_REPOSITORIO.md`, `HALLAZGOS_PUBLICACION.md`, `INSTALACION_EQUIPO.md`, `PLAN_DESPLIEGUE_3_PC.md`, `PRUEBAS_Y_EVIDENCIAS.md` y este informe |

No se borraron archivos existentes. No se cambiaron modelos, contratos de API,
reglas de compra, migraciones SQL, Compose actuales de demo/staging/laboratorio
ni la línea base `RESULTADOS_DISTRIBUIDO.md`. Las credenciales externas, datos,
volúmenes y respaldos existentes no se modificaron. Los cambios de conexión
solo afectarán imágenes que se construyan después; no se recrearon servicios.

Respaldo previo de 207 archivos e inventario SHA256 fuera de Git:
`/tmp/nexustix-publicacion-lan-illh2xv7/antes.tar.gz` y `inventario-antes.json`.
No es un backup de bases. Preservarlo en almacenamiento privado duradero antes
de reiniciar; no publicarlo. Ese directorio también conserva las pruebas.

## Resultados reales

| Verificación | Resultado | Alcance |
|---|---|---|
| Unitarias, `20261103T204021Z` | **140 Python + 1 Node aprobadas**, 0 fallidas | SQLite temporal, dobles, preparación y guardas; no bases vivas |
| Compose existentes | **13 combinaciones válidas** | Demo/staging/lab, overlays, VM y legacy con variables sintéticas |
| Compose LAN | **3 válidos**, 3/6/2 servicios por nodo | `config --format json`, sin arranque |
| Sintaxis | **103 Python, 11 shell y 5 JavaScript válidos** | AST, `sh -n`, `node --check` |
| HAProxy | `haproxy -c` código 0 | Contenedor nuevo sin red, raíz de solo lectura, configuración sintética |
| Simulación HAProxy real | APROBADA: alternancia, caída de backend, cinco respuestas del otro y readmisión | Un contenedor efímero sin red; tres procesos HAProxy, no API ni PostgreSQL |
| Secretos | 80 coincidencias revisadas, 0 pendientes | No se imprimen valores; excepciones exactas auditables |
| Estado existente | Laboratorio 12 healthy; staging 13 healthy | Mismos IDs observados; no detenciones/recreaciones |
| Tres PC / firewall / compras LAN | **PENDIENTE** | Sin IPs/accesos reales; ningún resultado inventado |

La imagen usada para validar/simular fue `haproxy:3.0-alpine`, digest observado
`sha256:f06f6cc1c25c7071e62d955d8e67a33d6935066ced8fac496cdd0ad9ffdd17f3`.
Se descargó esa imagen y solo se crearon contenedores efímeros sin red ni bases,
con `/tmp` en memoria durante la simulación. No se instalaron paquetes del host.
Los contenedores de validación se eliminaron al terminar; ningún contenedor
existente se detuvo, reinició o eliminó.

Se conservaron los intentos fallidos:

- Primera colección: el nombre nuevo `test_preparacion.py` colisionaba con VM;
  se renombró únicamente el archivo nuevo a `test_lan_preparacion.py`.
- Segunda ejecución: dos pruebas detectaron una llamada antigua al lector de
  conexión en migraciones; se corrigió el consumidor y las llamadas VM equivalentes.
  La repetición completa quedó aprobada; no se alteraron aserciones existentes.
- Primera simulación: la imagen HAProxy no incluía `httpd`; se sustituyeron esos
  backends sintéticos por procesos del propio HAProxy. La repetición aprobó.

`git diff --cached --check` señala ocho avisos de formato heredados: cinco
saltos Markdown hechos con espacios y tres líneas vacías al final de archivos.
Se conservaron; no son secretos ni errores de sintaxis/pruebas. No se presenta
esa comprobación como aprobada. El contenido del índice se comparó mediante
SHA256 con los candidatos revisados antes del commit.

## Revisar y publicar posteriormente, por el propietario

El primer commit local se prepara solo tras escaneo sin pendientes y pruebas
satisfactorias. El hash se obtiene con `git log -1 --oneline`; no se crea un
remoto ni se publica desde los scripts. Revisar:

```bash
git status --short
git show --stat --oneline HEAD
python3 scripts/revisar_publicacion.py
python3 scripts/revisar_publicacion.py --index
git ls-files -- .env resultados docs/evidencias
```

Solo debe aparecer el índice explicativo de evidencias, no artefactos ni datos.
`.gitignore` no retira secretos que alguien añada forzosamente; el escaneo del
índice sigue siendo obligatorio. Conservar privados secretos, env reales,
respaldos, WAL, discos VM, paquetes, logs y resultados en bruto.

**Comandos futuros, no ejecutados:** crear primero un repositorio privado vacío
en GitHub, sin README inicial, verificar su visibilidad y otorgar acceso a los
compañeros. Cuando la revisión final del propietario esté aprobada:

```bash
read -r -p 'URL SSH real del repositorio privado vacío: ' NEXUSTIX_REMOTE_URL
git remote
# Solo si no existe origin; si existe, revisar su destino en privado antes de continuar.
git remote add origin "$NEXUSTIX_REMOTE_URL"
git push -u origin HEAD
```

No incluir tokens en URLs ni compartir la clave SSH privada. La autorización de
esta tarea no incluye ejecutar esos comandos de publicación.

## Desplegar posteriormente

La guía exacta es [despliegue-pc/README.md](../despliegue-pc/README.md):
clonar el mismo commit, introducir IPs reales, generar una sola instalación con
secretos nuevos por rol, transferir carpetas privadas por SSH, validar y arrancar
PC 2 → PC 3 → PC 1. Parar en orden inverso. Cada comando operativo tiene modo
plan y exige `--apply` para intervenir únicamente el proyecto LAN nuevo.

El ensayo `lan.py failover` se ejecuta en PC 1, detiene solo Compras 1, verifica
cinco `/health` servidos por Compras 2 y recupera Compras 1. Opcionalmente lee
`/ordenes/mias` con un token privado; no crea compras. El script y su recuperación
se probaron con dobles, no sobre PC físicas. La simulación del balanceador es
un ensayo separado; no reemplaza la aceptación LAN de compras/QR/inventario.

## Riesgos operativos pendientes

- **PC 1** es punto único de entrada; **PC 2** es único proveedor de las bases.
  Réplicas API no garantizan tolerancia a la pérdida de cualquiera de esas PC.
- HTTP y PostgreSQL sin TLS son solo para laboratorio sintético confiable.
  TLS y filtrado por pares requieren una fase operativa autorizada.
- HBA limita rol y contraseña SCRAM, pero no IP de origen del rol app; hay que
  revisar NAT de Docker y reglas LAN antes de aceptar datos reales.
- Confirmar Linux/Docker local, IPs estables, puertos, permisos/SELinux, recursos,
  backups fuera de PC 2 y disponibilidad de los equipos.
- Los tags base pueden cambiar: fijar digests para una entrega inmutable.
- Las escrituras en vuelo pueden fallar durante un cambio de backend. Reintentar
  con la misma clave de idempotencia y comprobar resultado, sin asumir éxito.
- No hay replicación ni promoción de DB en esta versión. La segunda fase debe
  diseñar fencing, RPO/RTO, reintegración y consistencia entre los tres dominios.
