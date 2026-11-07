# NEXUSTIX por inventario: 1, 2, 3 o más computadoras

Ruta nueva e independiente de `docker-compose.yml`, `despliegue-pc` y
`despliegue-vm`. **Implementación preparada, no ejecutada ni probada en esta
entrega.** No se iniciaron contenedores, no se modificaron bases, firewall ni
hosts; no se tocaron staging ni laboratorio. Los comandos siguientes son para
que el operador los ejecute cuando autorice la preparación o el despliegue.

## Perfiles y alcance real

| Perfil | Requisitos de inventario | Límite |
|---|---|---|
| `local` | Un nodo | Sin tolerancia a fallo físico |
| `distribuido` | Dos o más nodos; al menos una API replicada | Puede tener gateways, servicios o bases sin redundancia |
| `alta_disponibilidad` | Tres dominios físicos como mínimo; gateways, frontend y cada API en dos dominios; cada base en tres dominios, con WAL síncrono y margen de réplicas | **HA preparada con recuperación manual**, no disponibilidad física certificada ni failover automático |

El inventario admite de 1 a 32 nodos. Un `failure_domain` representa una máquina
física: dos Ubuntu WSL o VM sobre el mismo equipo deben compartir ese dominio.
El presupuesto `resources` es CPU/memoria disponible para estos contenedores,
no el total del host. Se comprueba contra límites conservadores de contenedores;
no se mide capacidad real ni se garantiza absorber tráfico tras un fallo.
Se puede escalar agregando nodos y distribuyendo roles sin cambiar negocio.

Cada dominio conserva **una base lógica**: su primario y sus copias físicas
WAL contienen el mismo conjunto de datos, con posible retraso. Las réplicas API
comparten esa base; no son bases independientes que después se fusionan.

## Preparar archivos para 1, 2, 3 y 4 nodos

Requisitos para preparar: Python 3.10+, PyYAML 6 y OpenSSL (o `cryptography`).
Desde la raíz del repositorio, usando un entorno Python elegido por el operador:

```bash
python3 -m pip install -r despliegue-nodos/requirements.txt
```

Copie **un** ejemplo a su inventario editable, sustituya las IP y capacidades
por las reales y ejecute el comando de la fila correspondiente. Son ejemplos
de **instalaciones nuevas diferentes**, no pasos de ampliación de una misma
instalación. El destino debe ser nuevo y quedar fuera del repositorio.

| Nodos | Copia de partida | Preparación, después de editar el YAML |
|---|---|---|
| 1 | `cp despliegue-nodos/examples/1-local.yml inventario.yml` | `python3 despliegue-nodos/nodos.py preparar inventario.yml "$HOME/.config/nexustix/nodos-local-v1"` |
| 2 | `cp despliegue-nodos/examples/2-distribuido.yml inventario.yml` | `python3 despliegue-nodos/nodos.py preparar inventario.yml "$HOME/.config/nexustix/nodos-dos-v1"` |
| 3 | `cp despliegue-nodos/examples/3-ha-preparada.yml inventario.yml` | `python3 despliegue-nodos/nodos.py preparar inventario.yml "$HOME/.config/nexustix/nodos-tres-v1"` |
| 4 | `cp despliegue-nodos/examples/4-ha-heterogenea.yml inventario.yml` | `python3 despliegue-nodos/nodos.py preparar inventario.yml "$HOME/.config/nexustix/nodos-cuatro-v1"` |

El YAML no contiene contraseñas. `preparar` sólo lee configuración y escribe una
entrega nueva: crea claves JWT, credenciales de bases/replicación/administrador y
credencial interna; no crea usuarios de aplicación, eventos ni compras. No
ejecuta Docker, SSH, migraciones ni consultas de red o DB. Si falla, conserva
`INCOMPLETE`; no distribuya esa entrega ni borre el marcador para utilizarla.

Contenido de la entrega:

```text
entrega/
  inventory.yml               # inventario normalizado
  installation.json           # identidad e historial de generaciones
  access-points.json           # URL preferida y alternativas
  master/                     # claves de la instalación: NO distribuir
  equipo-a/
    compose.yml
    launch-order.json         # lista declarativa de primarios, standbys y aplicaciones
    config/                   # HAProxy, frontend e inventario del laboratorio
    secrets/                  # sólo los secretos necesarios para este nodo
  equipo-b/ ...
```

Conserve `master`, `installation.json` e inventarios en el equipo administrador,
con respaldo privado. Cada PC recibe **sólo su carpeta**, mediante un canal
seguro, conservando directorios `0700` y secretos `0600`. No copie la entrega
completa al repositorio, no envíe secretos por chat y no use volúmenes anteriores.
El código actualizado debe estar disponible en cada PC; imágenes y Compose no
se envían ni se actualizan automáticamente.

## Fedora y Windows/Ubuntu WSL 2

- **Fedora:** `address` es la IP LAN propia del equipo. Los puertos se vinculan
  a esa IP; el ejemplo local usa `127.0.0.1` y se abre con esa dirección exacta.
  Docker Engine/Compose deben estar preparados. Los montajes públicos piden
  etiquetado SELinux compartido `:z` al desplegar. Los archivos de `secrets/`
  requieren un contexto que permita lectura al contenedor; los secretos
  Compose no aplican `:z`. Su preparación/etiquetado debe autorizarla el
  administrador del equipo: el generador no ejecuta `chcon` ni desactiva SELinux.
- **WSL 2 + Docker Desktop:** ejecute los comandos desde Ubuntu, con integración
  Docker Desktop habilitada, repositorio y entrega en el filesystem Linux
  (`/home/...`, no `/mnt/c/...`). `address` es la IP LAN de **Windows**, no la
  IP NAT de Ubuntu. Los puertos se publican en `0.0.0.0` dentro de Docker Desktop;
  eso no demuestra que sean accesibles desde otras PC. NAT, modo mirrored,
  VPN, firewall Windows y política de Docker Desktop deben comprobarse allí.
  No se crean `portproxy`, IP virtuales ni reglas automáticamente.
- Use direcciones estables. Cada `docker_subnet` debe evitar las redes LAN,
  VPN y otros puentes Docker del equipo. El generador comprueba solapamientos
  declarados, no inspecciona rutas del host. Para destinos locales usa DNS
  Docker; no depende del hairpin Windows para acceder al propio PostgreSQL/API.
- PostgreSQL autoriza por defecto las IP LAN `/32` del inventario y el puente
  local. Si NAT cambia el origen, identifique primero la IP real y declare en
  **el nodo servidor** `postgres_client_cidrs: ["192.168.50.25/32"]`, por ejemplo.
  Sólo se aceptan rangos privados `/24` a `/32`; prefiera `/32`. Regenerar no
  cambia firewall ni aplica permisos a un servidor ya ejecutándose.

## Puertos y acceso alternativo

| Uso | Predeterminado | Dónde se publica |
|---|---|---|
| Portal | 8080 | Nodos gateway |
| Usuarios / Eventos / Compras | 8001 / 8002 / 8005 | Nodos gateway |
| Frontend directo | 8180 | Nodos frontend |
| Réplicas API directas | 18001 / 18002 / 18003 | Nodos con la API correspondiente |
| PostgreSQL Usuarios / Eventos / Compras | 5432 / 5433 / 5434 | Nodos con una copia de esa base |

No publique los puertos internos a Internet. Este perfil LAN usa HTTP y SCRAM,
sin TLS de transporte: una LAN no confiable requiere otra preparación de red y
cifrado. No se expone una consola administrativa de HAProxy.

`ports` permite cambiar cualquiera de esos valores. Si otra instalación ya
ocupa 8080 u otros puertos, **no la detenga**: use IP de otro equipo o puertos
libres. Por ejemplo, agregue al inventario local para coexistir:

```yaml
ports:
  public: 38080
  usuarios: 38001
  eventos: 38002
  compras: 38005
  frontend_backend: 38180
  usuarios_backend: 38101
  eventos_backend: 38102
  compras_backend: 38103
  usuarios_db: 35432
  eventos_db: 35433
  compras_db: 35434
```

HAProxy genera pools desde los roles del inventario, balancea `roundrobin` y
retira/reincorpora backends mediante `/health`. Descubrimiento **declarativo al
regenerar**, no un registro dinámico externo. Cada nodo de API tiene además un
router interno para llamadas entre servicios sin depender del gateway principal.
No hay reintento automático de escrituras en HAProxy; se conserva la idempotencia
del negocio. Las peticiones en vuelo pueden fallar.

Si falla el gateway preferido, el usuario abre explícitamente otra URL de
`access-points.json`, por ejemplo `http://192.168.50.12:8080`. Frontend y API
utilizan el nuevo host. No hay VIP, redirección desde una máquina apagada ni
transferencia de cookies entre hosts: puede necesitar iniciar sesión de nuevo.
Conserve las URL también fuera del portal, porque el portal principal puede caer.

## Desplegar posteriormente, sólo con autorización del operador

Estos comandos **sí inician contenedores e inicializan bases nuevas** cuando
se ejecutan. No forman parte de `preparar` y no se ejecutaron en esta entrega.

En cada PC, ajuste las rutas reales, manteniendo el mismo código fuente:

```bash
export NEXUSTIX_SOURCE_ROOT="/ruta/absoluta/al/repositorio/NEXUSTIX"
cd "/ruta/privada/a/la/entrega/equipo-a"
```

No use `-p nexustix`, nombres de volúmenes anteriores ni `down -v`. El Compose
genera proyecto/red/volúmenes propios `nexustix-nodes-<deployment>-<nodo>`.

Primero inicie **únicamente los primarios**, según el ejemplo escogido:

| Ejemplo | Equipo A | Equipo B | Equipo C | Equipo D |
|---|---|---|---|---|
| 1 | `usuarios_db eventos_db compras_db` | — | — | — |
| 2 | `usuarios_db compras_db` | `eventos_db` | — | — |
| 3 | `usuarios_db` | `eventos_db` | `compras_db` | — |
| 4 | Sin DB | `usuarios_db` | `eventos_db` | `compras_db` |

Use los nombres de su celda; para el equipo A del ejemplo de dos nodos:

```bash
docker compose up -d --build --wait usuarios_db compras_db
```

Después de que **todos los primarios** estén listos, en cada nodo con DB de
estos ejemplos inicie sus copias restantes (las ya iniciadas se conservan):

```bash
docker compose up -d --build --wait usuarios_db eventos_db compras_db
```

No aplique esa lista a un inventario personalizado que carezca de alguna base:
consulte `launch-order.json`. Si una clonación falla, no borre datos; consulte
[recuperación](RECUPERACION.md). Finalmente, con las copias requeridas por el
quórum WAL disponibles, en cada nodo:

```bash
docker compose up -d --build --wait
```

Las API existentes ejecutarán sus migraciones protegidas sobre **las nuevas
bases** al arrancar. Este despliegue desactiva el sembrado de eventos. El
administrador tampoco se crea automáticamente: el bootstrap existente queda
como acción separada del operador; la clave inicial se prepara en el secreto
`admin_password` de los nodos Usuarios, sin publicarla en documentación.

## Agregar, retirar nodos y regenerar

Edite **el inventario de la misma instalación**, sin cambiar `deployment` ni
los IDs de nodos existentes, y prepare una entrega nueva conservando identidad:

```bash
python3 despliegue-nodos/nodos.py preparar inventario.yml \
  "$HOME/.config/nexustix/entrega-v2" \
  --previous "$HOME/.config/nexustix/entrega-v1"
```

`--previous` es obligatorio para la evolución de una instalación real. Sin él
se generan claves distintas: **no use una entrega nueva sobre volúmenes
existentes**. La regeneración conserva claves JWT, contraseñas y nombres de
volúmenes; no sobrescribe la entrega anterior ni aplica cambios al clúster.
El volumen de estado registra la identidad de instalación y nodo: rechaza
credenciales de una entrega nueva y datos existentes sin esa identidad.
Las generaciones retiradas se recuerdan para impedir su reutilización accidental.
Una entrega regenerada tampoco inicializa un primario cuyo volumen está vacío.

Actualice primero accesos/slots del primario al agregar standbys, después las
réplicas y finalmente los routers/gateways/API. Redistribuya las carpetas de
todos los nodos afectados, establezca `NEXUSTIX_SOURCE_ROOT` y recree los
servicios afectados desde su **nueva** carpeta. Un reinicio simple no incorpora
cambios de imagen/configuración. Compruebe manualmente la convergencia antes de
retirar un nodo; el generador no detiene contenedores ni borra slots/volúmenes.
No utilice dos carpetas de entregas simultáneamente para administrar el mismo nodo.

Cambios de primario, dirección de copias, durabilidad o generaciones exigen
`--acknowledge-db-transition usuarios` (o `eventos` / `compras`, repetible),
después de resolver el procedimiento de [recuperación](RECUPERACION.md).
Esa opción no hace ni certifica fencing o promoción. Si queda una topología
degradada que no cumple HA, declare temporalmente `distribuido` y documente el
riesgo; no eluda la comprobación inventando nodos disponibles.

## Laboratorio de Resiliencia

Las API montan `config/operations-inventory.json` y activan
`NEXUSTIX_OPERATIONS_INVENTORY_FILE`; sin esa variable se conserva la topología
anterior. El panel administrativo muestra nodos, roles, presupuesto declarado,
respuestas HTTP, primarios/standbys configurados y canales WAL observables.
Las proyecciones por nodo y dominio físico indican réplicas restantes,
dependencias, escritura bloqueada por quórum y recuperación manual pendiente.
Son cálculos sobre el inventario, **no simulaciones ni pruebas ejecutadas**.

No hay agente de host: un timeout no demuestra que una PC esté apagada, las
copias dedicadas a DB no se sondean directamente y el diagnóstico API no
certifica la ubicación física del primario conectado. El estado desconocido
se conserva como tal. Las sondas tienen presupuesto de inicio y concurrencia
acotada; una captura grande puede ser parcial. Las evidencias guardadas siguen
dependiendo de Compras y su DB; no son un observador independiente con HA.

## Compatibilidad, migración y pendientes físicos

Los Compose LAN/VM/demo actuales siguen intactos. Esta ruta no transforma sus
volúmenes ni copia datos automáticamente. Para conservar datos antiguos, haga
un traslado lógico planificado con respaldo/restauración, roles compatibles,
mantenimiento y corte coordinado de las tres bases; no monte un volumen antiguo
en esta imagen ni use `scripts/db_ha.py` contra estos nombres. No se ha ejecutado
esa migración ni se han generado registros de demostración.

Pendiente de comprobar **en las PC reales**, con autorización posterior:
conectividad entre nodos y clientes (especialmente Docker Desktop/WSL), permisos
SELinux, puertos/subredes libres, mismas imágenes, recursos bajo carga,
replicación/lag/slots, respaldo y restauración, sesiones por gateway alternativo,
fallos y recuperación con aislamiento comprobable. No se declara HA operativa,
RPO cero ni RTO garantizado. El quórum WAL no es un quórum de elección de líder.

Referencia técnica del diagnóstico: las estadísticas completas de otros roles
requieren permisos de monitorización; aquí se otorga `pg_monitor` únicamente a
la cuenta de aplicación de cada clúster nuevo ([PostgreSQL 15](https://www.postgresql.org/docs/15/monitoring-stats.html)).
