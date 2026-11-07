# Tres PC LAN — primera versión operativa preparada

**Archivos preparados y verificados estáticamente; despliegue físico pendiente.**
Solo datos sintéticos en una LAN de laboratorio confiable: esta versión usa HTTP
y PostgreSQL sin TLS. No usar credenciales reales de personas ni exponerla a
Internet. TLS es trabajo pendiente antes de datos reales.

| Nodo | Servicios | Persistencia |
|---|---|---|
| PC 1 | Compras 1, frontend Nginx, gateway HAProxy | Sin bases locales |
| PC 2 | Compras 2, Usuarios A, Eventos A, tres PostgreSQL | Únicos primarios Usuarios/Eventos/Compras |
| PC 3 | Usuarios B, Eventos B | Usa los primarios de PC 2 |

Los dos procesos de cada API comparten su misma base lógica y secretos. Cada
PostgreSQL usa un rol de aplicación propietario del esquema, sin superusuario,
sin creación de roles/bases, y contraseñas aleatorias nuevas. El superusuario
PostgreSQL solo admite conexiones por socket local dentro del contenedor.
No hay standby, promoción ni failover de bases en esta fase.

El gateway incorpora comprobaciones activas cada 2 s (`GET /health`, HTTP 200,
dos fallos para retirar y dos éxitos para readmitir). Las API consultan su DB en
ese endpoint. `retries 0` evita reenviar automáticamente operaciones ambiguas:
tras un fallo, repetir una compra con su misma clave de idempotencia. Las
peticiones en vuelo pueden fallar. `/lb-health` comprueba solo el gateway.
[Manual HAProxy 3.0](https://docs.haproxy.org/3.0/configuration.html).

Los backends son IP LAN y puertos publicados; el generador no escribe nombres
Docker como destinos remotos. Las redes Compose son locales por equipo.
[Redes Compose](https://docs.docker.com/compose/how-tos/networking/).

## 1. Requisitos y datos reales

PC 1: Fedora con Docker Engine. PC 2 y PC 3: Windows con Ubuntu **WSL 2** y
Docker Desktop, backend WSL 2 y contenedores Linux. Ejecutar `lan.py` desde
Bash en Fedora o Ubuntu, nunca desde Python de Windows. Se requieren Compose
v2 >= 2.20, Python >= 3.10, Git, OpenSSL y, en Fedora, el comando `ip`.
El controlador conserva el socket local `/var/run/docker.sock`, proporcionado
por Engine en Fedora y por la integración de Desktop en Ubuntu. No admite
Docker remoto, rootless ni variables `DOCKER_HOST`/`DOCKER_CONTEXT` activas.

En PC 2 y PC 3, comprobar desde PowerShell (solo lectura):

```powershell
wsl -l -v
Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceAlias,IPAddress
```

Ubuntu debe figurar con versión 2. En Docker Desktop habilitar **Use WSL 2 based
engine** y **Resources > WSL Integration > Ubuntu** antes del despliegue.
Usar la integración de Desktop, sin instalar otro Docker Engine dentro de
Ubuntu. Desde Ubuntu comprobar:

```bash
python3 --version
git --version
openssl version
command -v powershell.exe
docker --host unix:///var/run/docker.sock compose version
docker --host unix:///var/run/docker.sock info --format '{{.OperatingSystem}}'
```

El último comando debe identificar Docker Desktop. `powershell.exe` debe estar
disponible mediante interoperabilidad WSL: el controlador lo utiliza solo para
leer las IP de Windows y verificar que corresponden al nodo.
[Integración oficial Docker Desktop/WSL](https://docs.docker.com/desktop/features/wsl/).

Asignar IPs RFC1918 distintas y estables con el administrador de la LAN; no
se asigna ni se cambia ninguna IP desde estos scripts. Confirmar puertos libres,
comunicación entre pares, ausencia de aislamiento Wi-Fi, permisos Docker y que
no haya suspensión automática. El controlador rechaza hosts con proyectos
`nexustix`, `project`, staging, laboratorio o VM existentes. Usar los otros
hosts limpios; **no ejecutar `up` en el Fedora protegido actual**.

Para `LAN_PC2_IP` y `LAN_PC3_IP` usar las IP de los adaptadores Ethernet/Wi-Fi
de **Windows** conectados a la LAN. No usar la IP NAT de Ubuntu (`hostname -I`),
adaptadores virtuales ni `localhost`. Las URL de DB y los backends HAProxy
conservan esas IP de Windows. No se necesita configurar `portproxy` ni cambiar
el modo de red de WSL para publicar puertos mediante Docker Desktop.
[Red de Docker Desktop](https://docs.docker.com/desktop/features/networking/).

Reservas orientativas, no capacidad medida: PC 1 2 CPU/4 GiB disponibles;
PC 2/3 4 CPU/8 GiB disponibles. PC 2 necesita SSD y espacio para imágenes, tres
bases, WAL y backups. Registrar tamaños reales y crecimiento antes de operar.

Puertos predeterminados propuestos, todos configurables al preparar:

| Equipo | Puertos TCP accesibles mediante su IP LAN |
|---|---|
| PC 1 | 8080 frontend; 8001 gateway Usuarios; 8002 gateway Eventos; 8005 gateway Compras; 8003 Compras 1 |
| PC 2 | 8001 Usuarios; 8002 Eventos; 8003 Compras 2; 5432/5433/5434 bases Usuarios/Eventos/Compras |
| PC 3 | 8001 Usuarios; 8002 Eventos |

`lan.py` detecta WSL automáticamente: los Compose existentes reciben
`LAN_BIND_IP=0.0.0.0` para que Desktop publique los puertos en Windows. En
Fedora se conserva el enlace a la IP LAN específica del nodo. `0.0.0.0` es
solo la dirección de escucha; nunca se usa como destino entre servicios.
En Windows esto publica en todas las interfaces IPv4: antes de operar, debe
existir una política autorizada de Windows Defender Firewall que limite los
puertos a los equipos necesarios. VPN, perfil de red o firewall pueden impedir
el acceso desde otras PC. Estos scripts no crean ni modifican esas reglas.

Los navegadores necesitan PC 1:8080/8001/8002/8005. La vista diagnóstica de
réplicas también puede usar Compras directo en PC 1/2:8003. PC 1 necesita los
backends PC 2/3, ambas Compras necesitan el gateway Eventos de PC 1, y todas las
API necesitan su puerto DB en PC 2. Restrinja esos accesos por pares mediante
la política de red autorizada. Los scripts no cambian firewall. Docker publica
puertos mediante sus propias reglas: no asumir que una regla genérica del host
los filtra. HBA exige SCRAM para el rol de aplicación, pero acepta cualquier
IP que alcance el puerto; el filtrado por pares está pendiente de autorización.

## 2. Clonar el mismo commit

Después de que el propietario publique privadamente, ejecutar en cada PC:

En PC 2/3 usar la terminal **Ubuntu WSL 2** y un clon bajo `$HOME` de Ubuntu,
no `/mnt/c` ni `/mnt/d`. Guardar también la carpeta privada del nodo fuera del
clon, dentro del sistema de archivos Linux, para conservar permisos 0700/0600.
El comando siguiente conserva finales de línea LF sin cambiar la configuración
global de Git.

```bash
read -r -p 'URL SSH real del repositorio privado: ' REPO_URL
read -r -p 'Directorio nuevo para el clon: ' CLONE_DIR
git -c core.autocrlf=input clone -- "$REPO_URL" "$CLONE_DIR"
cd -- "$CLONE_DIR"
git rev-parse HEAD
```

Comparar el hash entre las PC. Para ejecutar las pruebas locales, seguir
[INSTALACION_EQUIPO](../docs/INSTALACION_EQUIPO.md). No copiar secretos de demo,
staging o laboratorio. No levantar los Compose monolíticos en cada PC.

## 3. Preparar configuración y secretos, una sola vez

Desde un clon y una terminal de operador, elegir un destino nuevo fuera de Git:

```bash
read -r -p 'IP LAN real PC 1: ' PC1_IP
read -r -p 'IP LAN real de Windows PC 2: ' PC2_IP
read -r -p 'IP LAN real de Windows PC 3: ' PC3_IP
read -r -p 'Identificador nuevo (3-25, minúsculas/dígitos/guion): ' LAN_ID
read -r -p 'Directorio privado NUEVO fuera del repositorio: ' LAN_PACKAGE_DIR
python3 despliegue-pc/lan.py prepare --destination "$LAN_PACKAGE_DIR" \
  --deployment "$LAN_ID" --pc1 "$PC1_IP" --pc2 "$PC2_IP" --pc3 "$PC3_IP"
```

Opciones de puertos, si los anteriores están ocupados:
`--frontend-port`, `--usuarios-port`, `--eventos-port`, `--compras-port`,
`--balancer-port`, `--pg-usuarios-port`, `--pg-eventos-port`, `--pg-compras-port`.
Se exige que sean diferentes y estén entre 1024 y 65535. Cambiarlos **al generar**,
antes de distribuir: el frontend y las URL de DB también contienen esos puertos.
No editar solo un `config.env` después del despliegue.

Se crean `master/`, `pc1/`, `pc2/`, `pc3/`. `master` es custodia privada del
operador y nunca se distribuye ni publica. Los nodos reciben solo su carpeta.
La clave privada JWT se entrega únicamente a las dos instancias Usuarios.
`config.env` no contiene contraseñas; los archivos en `secrets/` sí. El UUID
compartido protege contra reutilizar recursos de otra generación con igual nombre.

Transferencia cifrada, con destinos **inexistentes** elegidos por los responsables:

El ejemplo requiere SSH previamente disponible en cada destino. WSL no instala
ni publica un servidor SSH automáticamente. Si PC 2/3 no lo tienen, usar un
canal cifrado autorizado y copiar la carpeta recibida al `$HOME` de Ubuntu;
no configurar SSH, reenvíos de puertos ni firewall para seguir este ejemplo.

```bash
read -r -p 'SSH usuario@host real de PC 1: ' PC1_SSH
read -r -p 'Destino privado NUEVO en PC 1: ' REMOTE_PC1_DIR
scp -pr "$LAN_PACKAGE_DIR/pc1" "$PC1_SSH:$REMOTE_PC1_DIR"
read -r -p 'SSH usuario@host real de PC 2: ' PC2_SSH
read -r -p 'Destino privado NUEVO en PC 2: ' REMOTE_PC2_DIR
scp -pr "$LAN_PACKAGE_DIR/pc2" "$PC2_SSH:$REMOTE_PC2_DIR"
read -r -p 'SSH usuario@host real de PC 3: ' PC3_SSH
read -r -p 'Destino privado NUEVO en PC 3: ' REMOTE_PC3_DIR
scp -pr "$LAN_PACKAGE_DIR/pc3" "$PC3_SSH:$REMOTE_PC3_DIR"
```

No repetir la copia sobre un destino existente. Verificar que el destino contiene
`config.env` y `secrets/`, sin nivel de carpeta extra; `scp -p` conserva permisos.
Si la transferencia pasó por Windows, restaurar permisos **solo en la carpeta
nueva de este nodo**, ya copiada al sistema de archivos Linux:

```bash
read -r -p 'Carpeta privada NUEVA del nodo en Ubuntu, fuera del clon: ' LAN_NODE_DIR
chmod 700 -- "$LAN_NODE_DIR" "$LAN_NODE_DIR/secrets"
chmod 600 -- "$LAN_NODE_DIR/config.env" "$LAN_NODE_DIR"/secrets/*
```

Cada responsable declara su ruta real en su propia terminal:

```bash
read -r -p 'Carpeta privada del nodo recibida: ' LAN_NODE_DIR
python3 despliegue-pc/lan.py validate --env-file "$LAN_NODE_DIR/config.env"
```

Debe imprimir `CONFIGURACION_VALIDA_SIN_DESPLIEGUE`. No abre Docker ni PostgreSQL:
solo renderiza Compose y valida rutas/permisos. En Fedora SELinux puede requerir
etiquetar **únicamente la carpeta nueva recibida**, después de autorizar la
operación local: `chcon -R -t container_file_t "$LAN_NODE_DIR"`. No desactivar
SELinux ni etiquetar carpetas de los entornos existentes. Esta acción no se
aplicó durante la preparación.

## 4. Arranque por nodos

Todos los comandos se ejecutan desde la raíz del clon en el nodo indicado.
Sin `--apply`, los comandos operativos muestran solo el plan. No se modifican
redes físicas ni firewall; `up` sí crea la red bridge y volúmenes **nuevos** del
proyecto LAN y aplica migraciones en esas bases nuevas.

**Primero PC 2 — terminal Ubuntu WSL 2**, que contiene los primarios:

```bash
python3 despliegue-pc/lan.py up --env-file "$LAN_NODE_DIR/config.env"
python3 despliegue-pc/lan.py up --env-file "$LAN_NODE_DIR/config.env" --apply
python3 despliegue-pc/lan.py verify --env-file "$LAN_NODE_DIR/config.env" --apply
read -r -p 'Correo del administrador de este laboratorio NUEVO: ' LAN_ADMIN_EMAIL
python3 despliegue-pc/lan.py admin --env-file "$LAN_NODE_DIR/config.env" \
  --email "$LAN_ADMIN_EMAIL" --apply
```

El bootstrap usa el `admin-password` privado de PC 2; no lo muestra. Inicializa
un administrador una vez y no cambia cuentas existentes. Compras de PC 2 puede
estar sana antes de que exista el gateway Eventos: `/health` verifica su DB, no
la compra completa. No realizar compras hasta terminar PC 1.

**Después PC 3 — terminal Ubuntu WSL 2**:

```bash
python3 despliegue-pc/lan.py up --env-file "$LAN_NODE_DIR/config.env" --apply
python3 despliegue-pc/lan.py verify --env-file "$LAN_NODE_DIR/config.env" --apply
```

**Por último PC 1 — terminal Fedora** (solo si supera la guarda de host protegido):

```bash
python3 despliegue-pc/lan.py gateway-check --env-file "$LAN_NODE_DIR/config.env" --apply
python3 despliegue-pc/lan.py up --env-file "$LAN_NODE_DIR/config.env" --apply
python3 despliegue-pc/lan.py verify --env-file "$LAN_NODE_DIR/config.env" --apply
python3 despliegue-pc/lan.py status --env-file "$LAN_NODE_DIR/config.env" --apply
```

`gateway-check` inicia un contenedor efímero para `haproxy -c`; no es una prueba
LAN. `up --wait` espera healthchecks. `verify` comprueba las API locales y en
PC 1 también gateway/portal; no prueba cada backend remoto individualmente.
En WSL, `verify` consulta la IP LAN de Windows, no la dirección de escucha.
Completar con `/health` directo en PC 2 y PC 3, observando `X-Instance-ID` en las
respuestas de Usuarios/Eventos. Abrir `http://IP_REAL_PC1:PUERTO_FRONTEND` con
los valores elegidos, iniciar sesión, crear un evento sintético y realizar una
compra individual y grupal. Registrar resultado, replay, QR e inventario.

## 5. Ensayo reproducible de caída de Compras

En **PC 1**, con las dos Compras saludables, elegir evidencia fuera de Git:

```bash
read -r -p 'Archivo NUEVO de evidencia fuera del repositorio: ' LAN_EVIDENCE
python3 despliegue-pc/lan.py failover --env-file "$LAN_NODE_DIR/config.env" \
  --output "$LAN_EVIDENCE"
python3 despliegue-pc/lan.py failover --env-file "$LAN_NODE_DIR/config.env" \
  --output "$LAN_EVIDENCE" --apply
```

El plan no detiene nada. La ejecución real comprueba identidades 1/2, detiene
**solo** `compras` del proyecto LAN de PC 1, espera que el gateway entregue
respuestas de instancia 2, verifica cinco solicitudes consecutivas y finalmente
arranca Compras 1 y espera su salud. Las guardas se repiten al detener y arrancar.
No usa SSH para detener servicios ni llama scripts históricos.

Con `--token-file` y un JWT vigente guardado en un archivo privado del laboratorio
nuevo también verifica `GET /ordenes/mias` antes y durante la caída. No imprime
JWT ni respuestas privadas. **Alcance predeterminado:** HTTP `/health` con consulta
SQL de salud; no crea ni paga compras. No presentar este ensayo como compra
transaccional en vuelo ni como caída de PC completa. La aceptación funcional
manual anterior y las pruebas locales de idempotencia complementan ese alcance.
Si el operador interrumpe el proceso o falla el arranque, verificar el estado y
recuperar el nodo con `start`; el archivo conserva resultado FALLIDA si se captura
el error. No restaurar volúmenes ni recrear bases para corregir un fallo de API.

## 6. Diagnóstico y parada conservadora

En cualquier nodo, `status` muestra solo ese proyecto:

```bash
python3 despliegue-pc/lan.py status --env-file "$LAN_NODE_DIR/config.env" --apply
python3 despliegue-pc/lan.py verify --env-file "$LAN_NODE_DIR/config.env" --apply
```

Para parar el despliegue: primero PC 1, después PC 3, por último PC 2. En cada PC:

```bash
python3 despliegue-pc/lan.py stop --env-file "$LAN_NODE_DIR/config.env" --apply
```

Para reanudar los contenedores ya creados: PC 2, PC 3 y PC 1, respectivamente:

```bash
python3 despliegue-pc/lan.py start --env-file "$LAN_NODE_DIR/config.env" --apply
python3 despliegue-pc/lan.py verify --env-file "$LAN_NODE_DIR/config.env" --apply
```

`start` no construye ni recrea; esperar salud antes de avanzar. No existe comando
de eliminación en el controlador. No cambiar ID/UUID/secrets para reutilizar
volúmenes; ante un conflicto de identidad se detiene la operación.

## Límites y siguiente fase

La adaptación Fedora/WSL se verifica mediante sintaxis y renderizado de Compose;
no acredita ejecución real en Windows ni conectividad entre las tres PC.
En PC 2/3, Docker Desktop y Windows deben permanecer activos. Suspender Windows,
cerrar Docker Desktop o ejecutar `wsl --shutdown` interrumpe los servicios.
En PC 2, los datos permanecen en volúmenes del motor de Desktop: no restablecer
Desktop a valores de fábrica ni desregistrar distribuciones para diagnosticar.

PC 1 concentra portal/gateway: su pérdida interrumpe la entrada. PC 2 concentra
todas las bases: su pérdida interrumpe todas las API aunque PC 3 siga encendida.
No se afirma tolerancia a una PC completa. Faltan aceptación física, filtrado
LAN autorizado, TLS, backups verificados fuera de PC 2 y límites de recursos
medidos. Las etiquetas base de imágenes pueden actualizarse; fijar digests para
una entrega inmutable. No ejecutar esta configuración contra datos de producción.
La siguiente fase de PostgreSQL añadirá standbys, fencing y promoción de cada
clúster, con pruebas explícitas de pérdida/consistencia y sin reutilizar datos
actuales de Fedora.

## Repetir la simulación segura antes de disponer de PC

Desde la raíz, sin secretos, servicios ni bases reales:

```bash
python3 despliegue-pc/simular.py
python3 despliegue-pc/simular.py --apply
```

El segundo comando puede descargar `haproxy:3.0-alpine`. Crea un único contenedor
efímero sin red, sin volúmenes persistentes, con raíz de solo lectura y `/tmp`
en memoria. Tres procesos HAProxy representan un gateway y dos respuestas HTTP
sintéticas. Se comprueba alternancia, caída de uno, cinco respuestas del otro y
readmisión. No acredita FastAPI, compras, PostgreSQL ni comunicación LAN.
La configuración/salida se conservan en el directorio temporal que imprime.
