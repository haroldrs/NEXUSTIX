# Actualización: saneamiento y versión LAN mínima

La revisión posterior resolvió las 11 coincidencias con credenciales
predeterminadas y otras tres asignaciones sin comillas de los Compose históricos.
Las 81 coincidencias originales están clasificadas individualmente en
[HALLAZGOS_PUBLICACION.md](HALLAZGOS_PUBLICACION.md). La entrega actual, pruebas,
riesgos y estado del commit se registran en [PUBLICACION_Y_LAN.md](PUBLICACION_Y_LAN.md).

El resto de este documento conserva la instantánea de la fase anterior;
sus contadores y pendientes describen ese momento, no el estado actual.

---

# Auditoría del repositorio — 03/11/2026

Organización terminada para revisión humana previa a GitHub privado. Se
conservaron las rutas y no se cambió lógica de negocio. **La publicación sigue
pendiente de revisar los hallazgos de credenciales heredadas**. No se añadieron
archivos al índice, no se hicieron commits/push ni se creó un remoto.

## Inventario y respaldo previo

Antes de editar se inventariaron **7041 entradas** (archivos, directorios y
enlaces), incluyendo metadatos Git, entornos Python, resultados y cachés. Se
respaldaron los **196 archivos fuera de esos árboles generados**. Respaldo e
inventario completos fuera del árbol publicable:

`/tmp/nexustix-auditoria-20261103T200820Z-ft5xms08`

- `inventario-antes.json`: rutas, tipo, tamaño/permisos y SHA256 de fuentes.
- `inventario-clasificado.json`: categoría A–G por entrada de la instantánea.
- `fuentes-antes.tar.gz`: fuentes y documentación previas; no es backup de bases.
- SHA256: `57347b74be3cdd8d979f9b90e3afac3e0846c7a57512ba228315e2ba05cf6118`.
- El respaldo está en `/tmp`: preservarlo en almacenamiento privado duradero si
  se requiere conservarlo después de la sesión; no incorporarlo a Git.

La clasificación principal del inventario previo fue A: 59, B: 44, C: 44,
D: 44, E: 6822 y F: 28. No apareció un archivo secreto independiente por nombre
en el árbol de fuentes; **G también se aplica al contenido sensible potencial**
dentro de archivos A/B/F, detallado en [HALLAZGOS_PUBLICACION.md](HALLAZGOS_PUBLICACION.md).
Esto no describe las carpetas externas de secretos, que no se inspeccionaron.
[INVENTARIO_REPOSITORIO.tsv](INVENTARIO_REPOSITORIO.tsv) enumera 253 entradas de fuentes,
directorios y raíces generadas, incluidos los nuevos; el inventario exhaustivo de cachés y
artefactos se conserva fuera del repositorio para no versionar miles de entradas.

## Clasificación y decisión

| Categoría | Rutas / ámbito | Dependencia comprobada | Decisión |
|---|---|---|---|
| A — ejecución | `usuarios/`, `eventos/`, `compras/`, `shared/` | Imports de servicios y `COPY` de Dockerfiles; Alembic usa `migrations/`, `.ini`, `.mako` | Conservar íntegros; revisar fallback de conexión señalado como G |
| A — interfaz | `frontend/`, `compras_lb/` | `frontend/index.html` carga JS/CSS; Dockerfiles copian config y assets; upstreams Compose locales | Conservar; configuración LAN futura separada |
| A — dependencias | `requirements.txt`, `*/requirements.txt` | Imágenes instalan requisitos específicos; el archivo raíz es orientación de desarrollo anterior | Conservar y documentar diferencias; no unificar versiones aquí |
| B — topologías | `docker-compose*.yml` | Base demo, base staging, laboratorio, overlay HA y overlay tests | Conservar sin modificar; overlays no se ejecutan solos |
| B — PostgreSQL | `postgres/` | Entrypoints, healthcheck, init y WAL copiados por imagen base y VM | Conservar; no ejecutar durante la auditoría |
| B — VM | `despliegue-vm/` | Generadores, guardas, paquetes por rol, Compose y recuperación | Conservar intacto; scripts de VM no sirven directamente para PC físicas |
| B — ejemplos | `config/`, archivos `*.env.example` | Referencias de variables y rutas sin secretos reales | Conservar; no usar los ejemplos como asignaciones reales ni activar staging |
| C — locales | Nueve módulos raíz seleccionados, `tests/vm/`, `conftest.py`, `pytest.ini` | SQLite temporal, mocks, comandos de sintaxis y preparación temporal | Conservar; son selección predeterminada |
| C — integración | `tests/postgres/`, `tests/test_backup_restore_isolated.py` | PostgreSQL/Docker nuevos, creación y retirada de recursos | Conservar; opt-in, no ejecutar aquí |
| C — fallos | `tests/resiliencia/`, `tests/staging/` Python | HTTP, Docker y datos staging; imports entre suites | Conservar rutas; guardas adicionales; no usar sobre entorno protegido |
| C — frontend | `tests/staging/frontend_auth_state.test.js` | Lee `frontend/app.js` con DOM/HTTP simulados; el runner lo llama | Conservar en ruta actual; es seguro pese al nombre de carpeta |
| D — carga | `locustfile.py`, `simular_trafico.py`, `requirements-load.txt`, `scripts/preparar_carga.py` | `scripts/locust_safety.py` valida destinos; produce compras/escrituras | Conservar separado lógicamente; ejecución pendiente de autorización |
| D/B — administración | `scripts/` | `nx.py`/`db_ha.py` usan `entornos.py`/`pg_docker.py`; generadores y backups comparten utilidades | Conservar; leer ayuda/guías antes de operar |
| D — documentación | `README.md`, `LEEME_PRIMERO.md`, `docs/` actual | Guías, pruebas y evidencias históricas con límites | Conservar; README es la entrada vigente |
| F — histórico | `legacy/`, `docs/historico/` | No hay dependencia runtime de estas rutas; la documentación sí las referencia | Conservar para revisión; no ejecutar ni tomar como arquitectura LAN vigente |
| F/E — documento externo | PDF académico en raíz | No es entrada de build ni import; autoría/permiso por revisar | Excluir de Git sin borrar |
| E — generado | `.venv/`, cachés, `.git/`, `resultados/`, logs, paquetes, discos, evidencias crudas | No son fuentes; resultados sí respaldan informes históricos | Excluir; conservar localmente; compartir evidencia revisada por canal privado |
| G — local/sensible | `.env*` reales, `config.env`, claves, URLs de DB, tokens, carpetas de secretos y config local | Inyectados al desplegar, no código publicable | Excluir; los ejemplos seguros permanecen |
| G — contenido heredado | Conexiones por defecto y manifiestos/documentación históricos señalados por escáner | Algunos están en fuentes necesarias | No borrar ni cambiar secretos; revisar antes de publicar; no usarlos en LAN |

No se movió ni borró ningún archivo. La organización lógica conserva imports,
Dockerfiles, Compose y llamadas entre scripts. `requirements-test.txt` permanece
separado de producción y carga. El ejecutor unitario incluye ahora las 22 pruebas
VM ya existentes, sin reimplementar pruebas funcionales.

## Cambios realizados

Modificados, todos respaldados antes:

- `.gitignore`: env/config real, secretos, datos, cachés, resultados/evidencias,
  logs, paquetes, discos VM y archivo académico externo; ejemplos conservados.
- `.dockerignore`: lista positiva de siete directorios de aplicación/infra y
  PostgreSQL VM, seguida de exclusiones sensibles; contexto de build reducido.
- `README.md` y `LEEME_PRIMERO.md`: entrada vigente y aviso sobre órdenes históricas.
- `scripts/ejecutar_pruebas.py`: unit por defecto, incorpora pruebas VM; capas con
  infraestructura exigen `--permitir-infraestructura` y propagan el opt-in pytest.
- `scripts/probar_local.py`: opt-in antes de crear directorios/procesos PostgreSQL;
  mantiene compatibilidad con la guarda nueva de pytest.

Creados:

- `pytest.ini`, `conftest.py`, `scripts/revisar_publicacion.py`.
- `docs/AUDITORIA_REPOSITORIO.md`, `docs/INVENTARIO_REPOSITORIO.tsv`,
  `docs/HALLAZGOS_PUBLICACION.md`.
- `docs/INSTALACION_EQUIPO.md`, `docs/ARQUITECTURA_DISTRIBUIDA.md`,
  `docs/PLAN_DESPLIEGUE_3_PC.md`, `docs/PRUEBAS_Y_EVIDENCIAS.md`.
- `docs/evidencias/README.md` explica la exclusión de artefactos en los clones.

No cambió ningún archivo de servicios, modelos, migraciones, frontend, Nginx,
PostgreSQL ni `despliegue-vm/`. Los Compose existentes y
`docs/RESULTADOS_DISTRIBUIDO.md` conservan sus SHA256 previos. El índice Git tiene
0 archivos y no hay commits alcanzables; la revisión de cambios se hizo contra
el respaldo porque `git diff` no muestra archivos todavía no registrados.

## Inspección de secretos

Se usó `scripts/revisar_publicacion.py`, solo local, sobre los candidatos de
`git ls-files --cached --others --exclude-standard`. No se enviaron archivos a
servicios externos. La lectura también incluye archivos ya registrados si los
hubiera: `.gitignore` no los retiraría del índice.

La última pasada inspeccionó **200 archivos candidatos**, sin archivos ilegibles,
y devolvió código 1 para revisión. Se encontraron **81 coincidencias**: URLs con credenciales por defecto, literales
de prueba, plantillas, rutas y texto histórico. Se documenta ruta/línea/tipo y
`REDACTADO` en [HALLAZGOS_PUBLICACION.md](HALLAZGOS_PUBLICACION.md). Entre los riesgos
reales de configuración están `usuarios/database.py:6`, `eventos/database.py:6`,
`compras/database.py:6` y los Compose `legacy/multi-laptop/` (líneas 32/48, 30 y 32).
No se han alterado esos valores ni comprobado contra bases vivas.

No se encontraron claves privadas ni tokens completos según las reglas del
escáner. No se garantiza que no existan otros secretos. El código de salida 1
es esperado mientras haya hallazgos; no se silenció ni se convirtió en éxito.
Los antiguos defaults de conexión deben sustituirse o deshabilitarse en un
cambio posterior revisado, sin modificar las credenciales de bases actuales.
La publicación queda para revisión humana, no se declara libre de riesgos.

## Verificaciones reales de esta tarea

Evidencia local en `/tmp/nexustix-auditoria-20261103T200820Z-ft5xms08`:

| Comprobación | Resultado real | Límite |
|---|---|---|
| Runner por defecto, `pruebas/20261103T201410Z/` | 127 Python + 1 Node aprobadas; 0 fallidas | SQLite/dobles/temporales; no PostgreSQL real ni HTTP a servicios |
| `pytest --collect-only` por defecto | 127 casos recogidos | Misma selección segura; no ejecuta fixtures |
| Colección explícita staging sin flag | Rechazada, código 4 | Guarda comprobada antes de fixtures |
| Runner `--capas postgres` sin opt-in | Rechazado, código 2 | No creó PostgreSQL ni recursos Docker |
| `probar_local.py` sin opt-in | Rechazado, código 2 | No creó temporales ni procesos PG |
| Python AST | 100 archivos válidos | Análisis en memoria, sin importar servicios ni crear bytecode |
| Shell y JavaScript | 13 archivos válidos, `bash -n` / `node --check` | Sin ejecutar scripts |
| Compose | 13 combinaciones válidas con `config --quiet` | Sin daemon, build, contenedores ni lectura de secretos reales |
| Rejoin VM, primer intento | Rechazó ausencia de `VM_REJOIN_GENERATION`; repetición válida con identificador sintético | Se conserva el intento fallido; no fue un despliegue |
| `.gitignore` | 15 exclusiones y 10 inclusiones críticas verificadas con `git check-ignore` | Incluye ejemplos, migración, config frontend y resultados narrados |
| Enlaces de las guías nuevas | 25 destinos locales existentes | Los artefactos históricos excluidos pueden faltar en un clon |
| `.dockerignore` / Dockerfiles | 25 entradas `COPY` de 7 Dockerfiles existen y quedan admitidas; 10 rutas sensibles/ajenas excluidas en revisión estática | No se construyó imagen; patrones revisados, no ensayo de BuildKit |
| Preservación de fuentes | 6 archivos existentes cambiados, ninguno ausente; hashes de entornos/servicios iguales | Respaldo de fuentes, no una comparación del contenido vivo de bases |

Las 13 combinaciones incluyen tres Compose principales, overlay HA con demo y
staging, overlay de pruebas staging, cuatro manifiestos VM y tres históricos.
La validez sintáctica de legacy **no** significa compatibilidad con APIs actuales.
Para renderizar se usó loopback y un directorio vacío sintéticos, sin conectarse
a ellos. Las tres capas pendientes en el resumen unitario fueron deliberadamente
omitidas: PostgreSQL, resiliencia y staging. No se repitió la línea base funcional.

## Estado Docker observado, solo lectura

Dos consultas `docker ps -a` antes y después encontraron los mismos IDs:

| Proyecto | Estado observado |
|---|---|
| `nexustix-distributed-lab` | 12 contenedores ejecutándose y saludables |
| `nexustix-auth-staging` | 13 contenedores ejecutándose y saludables |
| `project` histórico | 9 contenedores ya detenidos, conservados |
| Sin etiqueta Compose | 6 contenedores ya detenidos, conservados |
| `nexustix` demo por ese nombre | No apareció en la lista; no se infiere ausencia de volúmenes |

El acceso inicial al socket fue bloqueado por sandbox; la consulta de lectura
se ejecutó después con escalación autorizada. No hubo comandos de cambio sobre
Docker, DB, red, firewall, VirtualBox ni sistema operativo. No se inicializaron,
reiniciaron ni eliminaron bases o volúmenes. La salud es observación del
healthcheck, no una nueva aceptación funcional. Staging tiene más réplicas que
su Compose base versionable; no se intentó reconciliar ni recrear ese entorno.

## Revisión del primer commit, sin crearlo aquí

Ejecutar solo comprobaciones de lectura desde la raíz:

```bash
git status --short --untracked-files=all
git ls-files --others --exclude-standard
git status --short --ignored
python3 scripts/revisar_publicacion.py
git check-ignore -v -- .env despliegue-vm/config.env resultados/prueba.log
git diff --check
```

Con índice vacío, `git diff` no revisa las fuentes nuevas; usar el inventario,
el respaldo y la inspección de candidatos. Revisar que no entren evidencias,
configuraciones locales ni credenciales. Después de que el propietario prepare
por su cuenta el primer commit, revisar `git diff --cached --stat` y
`git diff --cached --check`; revisar su contenido localmente sin pegar secretos.
No se proporcionan órdenes que añadan, confirmen o publiquen cambios en esta fase.

## Próximos pasos y límites

1. Revisar los 81 hallazgos y el contenido legacy; decidir qué ejemplos conservar
   y aprobar separadamente la eliminación de defaults inseguros del código.
2. Revisar primer commit y publicar privadamente por cuenta del propietario;
   después conceder acceso y seguir [INSTALACION_EQUIPO.md](INSTALACION_EQUIPO.md).
3. Clonar el mismo commit en las otras PC y ejecutar solo las pruebas locales.
4. Completar recursos/IPs/puertos reales y desarrollar los manifiestos propuestos
   de [PLAN_DESPLIEGUE_3_PC.md](PLAN_DESPLIEGUE_3_PC.md); no reutilizar controladores
   VM eliminando sus guardas. Datos de LAN nuevos y aislados.
5. Autorizar por separado instalación/red/despliegue y pruebas de fallo en PC.

Permanecen pendientes: gateway LAN para Usuarios/Eventos, upstreams entre hosts,
secretos por rol físico, preflight específico de PC, TLS, backups fuera del host,
fencing verificable y ensayos LAN. PC 1 es un punto único de entrada; antes del
standby, PC 2 es también único proveedor de bases. Replicación asíncrona tiene
riesgo de pérdida y promover tres bases no garantiza coherencia entre dominios.
No se afirma alta disponibilidad de una PC completa ni despliegue físico probado.
