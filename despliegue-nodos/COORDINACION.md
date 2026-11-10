# Coordinación, acceso estable y límites de alta disponibilidad

La implementación conserva un único primario autorizado por base y el
[procedimiento manual de recuperación](RECUPERACION.md). El reparto de peticiones
entre réplicas de aplicación y la exclusión de backends que fallan son
automáticos en HAProxy; la promoción PostgreSQL continúa siendo manual. Compose
no descubre computadoras, mueve contenedores ni decide dónde crear réplicas.

Esta guía prepara la evolución de `despliegue-nodos`, no un segundo despliegue.
No se instala Patroni, etcd, un exportador ni un servicio DNS. Ningún botón del
Centro de Control puede promover bases, aislar computadoras o ejecutar Docker.

Una plantilla aislada e inactiva se encuentra en
[coordinacion-inactiva/](coordinacion-inactiva/README.md).

## Decisión sobre Patroni y consenso

Patroni con etcd es una opción técnicamente adecuada para coordinar los tres
grupos PostgreSQL. La propuesta es un grupo Patroni independiente por base
lógica (`usuarios`, `eventos`, `compras`), cada uno con identidad y espacio de
claves propios en un mismo almacén etcd redundante. Las API seguirían usando sus
bases lógicas, las mismas claves JWT y los contratos actuales.

Para tres computadoras físicas independientes, un futuro etcd de tres miembros
necesita dos votos; puede continuar con un miembro perdido. Con cinco miembros
necesita tres. Una minoría aislada no puede confirmar cambios de consenso; perder
la mayoría impide progresar. Tres contenedores en una sola PC no dan tolerancia
a su fallo físico. No es necesario convertir cada nodo de aplicaciones en
votante: un despliegue de 32 computadoras puede mantener tres o cinco votantes
independientes. [Comportamiento oficial de etcd ante fallos](https://etcd.io/docs/v3.6/op-guide/failures/).

Son tres decisiones distintas:

| Mecanismo | Qué demuestra | Qué no demuestra |
|---|---|---|
| Quórum WAL (`ANY 1`, por ejemplo) | Las confirmaciones WAL exigidas por la política del commit | Quién puede ser el siguiente líder |
| Quórum de consenso | Acuerdo sobre la autoridad del líder dentro del coordinador | Que un PostgreSQL anterior ya dejó de aceptar escrituras |
| Fencing | Aislamiento efectivo y persistente del anterior escritor | Que el candidato conserve todos los commits |

Patroni intenta detener PostgreSQL si pierde el permiso de líder. Una pausa del
proceso, sobrecarga o apagado lento puede impedirlo a tiempo. Su soporte de
watchdog usa la interfaz Linux; `mode: required` rechaza el liderazgo si no puede
activar ese mecanismo. La temporización debe impedir que el escritor sobreviva
a su permiso, incluyendo pausas del proceso. [Garantías y condiciones del
watchdog de Patroni](https://patroni.readthedocs.io/en/latest/watchdog.html).

Docker Desktop ejecuta el motor dentro de una VM Linux y transporta tráfico a
través del host. Por ello, **no se infiere** que un `/dev/watchdog` visible dentro
de WSL o de un contenedor controle toda la PC Windows o todas las rutas del
PostgreSQL anterior. Reiniciar Ubuntu, perder ping o perder un gateway tampoco
demuestra ese aislamiento. Es una conclusión de diseño sobre el alcance del
host/VM, no una prueba física realizada. [Arquitectura de red de Docker
Desktop](https://docs.docker.com/desktop/features/networking/).

## Condiciones que bloquean la activación automática

El escenario Fedora + dos Windows/WSL todavía no aporta evidencia de un
mecanismo de fencing independiente. El perfil `alta_disponibilidad` y tres
réplicas por base no eliminan ese bloqueo. Para una integración posterior hacen
falta todas estas condiciones, con responsables y evidencias registradas:

1. **Coordinador real:** tres o cinco miembros de consenso en dominios físicos
   independientes, almacenamiento persistente, direcciones estables, respaldo
   y recuperación del coordinador. La incorporación y retirada de votantes se
   hace de forma serializada y manteniendo mayoría; editar el inventario de
   aplicaciones no cambia esa membresía. [Reconfiguración de etcd](https://etcd.io/docs/v3.6/op-guide/runtime-configuration/).
2. **Identidad y transporte:** certificados individuales, mTLS entre miembros y
   clientes, CA y claves fuera del repositorio, roles de mínimo permiso y acceso
   administrativo protegido. El observador sólo recibe permisos de lectura;
   no recibe credenciales de promoción o de fencing. [Seguridad del transporte
   de etcd](https://etcd.io/docs/v3.6/op-guide/security/).
3. **Fencing verificable por candidato:** watchdog que realmente detenga el
   escritor completo, o control externo de energía/hipervisor/red capaz de
   aislar todas sus rutas. La integración debe bloquear la promoción si no
   obtiene prueba de aislamiento. Debe funcionar también cuando Windows,
   Docker Desktop, la VM o el propio controlador quedan suspendidos.
4. **Un solo controlador:** Patroni debe asumir en exclusiva el ciclo de vida de
   cada PostgreSQL. No puede convivir con un entrypoint que arranque libremente
   el antiguo primario o con promociones manuales simultáneas. Hace falta una
   transición explícita de la imagen y del estado ya existente, conservando
   copias y generaciones anteriores.
5. **Elegibilidad y durabilidad:** mismo identificador de sistema, timelines
   compatibles, candidato con WAL adecuado y política de pérdida de datos
   explícita. `synchronous_mode_strict` es una opción futura para evitar reducir
   silenciosamente la durabilidad, a costa de bloquear escrituras sin réplicas.
   Ni ese ajuste ni un límite de lag justifican una promesa general de RPO cero.
   [Modos de replicación de Patroni](https://patroni.readthedocs.io/en/latest/replication_modes.html).
6. **Reconexión y reincorporación:** las conexiones multi-host actuales pueden
   buscar otra base que acepte escritura, pero no distinguen dos primarios
   simultáneos. Deben combinarse con la autoridad única del coordinador y
   fencing. El anterior primario vuelve aislado, como standby, tras reclonado o
   recuperación controlada; no basta cambiar su etiqueta.
7. **Comprobación física posterior:** partición de LAN, pérdida de mayoría,
   suspensión Windows/VM, reinicio del antiguo primario, pérdida del mecanismo
   de fencing y retorno de réplicas. Registrar que nunca hay dos escritores
   autorizados. Estas comprobaciones requieren otra fase autorizada.

Hasta completar esa integración, un coordinador se muestra como **no
configurado**, su quórum como **no observado** y el failover automático como
**bloqueado**. No se fabrican votos a partir de respuestas HTTP de las API.
Una recuperación manual conserva las condiciones de
[aislamiento y selección WAL](RECUPERACION.md#aislar-primero-promover-después).

Una futura sonda Patroni debe usar el endpoint adecuado: `/primary` exige
PostgreSQL primario y permiso de líder; `/health` sólo indica que PostgreSQL está
funcionando. El navegador no debe tener acceso a endpoints de escritura de
Patroni. [API oficial de Patroni](https://patroni.readthedocs.io/en/latest/rest_api.html).

## Acceso estable para clientes de la LAN

Los gateways existentes pueden balancear todos los servicios y conservan sus
URL alternativas. Una dirección estable necesita infraestructura adicional
real, además de un nombre en el YAML. Alternativas compatibles con el objetivo:

| Alternativa | Preparación necesaria | Límite que debe mostrarse |
|---|---|---|
| DNS redundante con supervisión de salud | Nombre estable, servidores DNS independientes y actualización coordinada hacia gateways alcanzables desde la LAN | Cachés y conexiones existentes retrasan el cambio; puede requerirse reintento manual |
| Entrada redundante externa | Al menos dos puntos de entrada independientes, con conmutación real de su dirección y rutas hacia varios gateways | Un único proxy o router añadido sigue siendo un punto único de fallo |
| URLs de gateways | Lista disponible fuera del portal principal | El usuario cambia la URL y puede necesitar iniciar sesión de nuevo |

No se implementa una VIP Linux sobre Windows/WSL. Una pareja de equipos Linux o
dispositivos de red podría proporcionar una dirección virtual si su red y
conmutación lo soportan, pero eso sería infraestructura externa que debe
prepararse y comprobarse. No se configura automáticamente en estas PC.

El nombre estable debe conducir tanto al frontend como a Usuarios, Eventos y
Compras. En el diseño actual son puertos separados del mismo host; enrutar sólo
8080 deja las API fuera del acceso estable. Si un proxy futuro termina HTTPS,
debe cubrir también las API, CORS y cookies; esta entrega no convierte el perfil
HTTP LAN en HTTPS. Un certificado y un proxy único tampoco aportan redundancia.

En WSL, NAT y mirrored son modos diferentes y el tráfico entrante puede depender
del firewall Hyper-V/Windows. Publicar un puerto dentro de Docker Desktop no
demuestra que otra PC llegue a él. No se cambia la red, firewall o modo WSL desde
el generador. [Red de WSL](https://learn.microsoft.com/en-us/windows/wsl/networking).

Antes de depender de DNS o entrada estable, debe comprobarse desde clientes
reales: cambio de gateway, todas las API, sesiones, reintento idempotente de una
compra interrumpida y pérdida de un punto de entrada. Las URLs alternativas se
mantienen incluso cuando exista acceso estable.

## Métricas del host sin dar control del host al portal

La fuente futura propuesta es un exportador de sólo lectura instalado con
autorización del administrador de cada PC. En Fedora puede evaluarse
[`node_exporter`](https://github.com/prometheus/node_exporter); para medir la PC
Windows completa, [`windows_exporter`](https://github.com/prometheus-community/windows_exporter).
Un exportador dentro de Ubuntu/WSL sólo debe etiquetar sus mediciones como WSL/VM,
no como CPU, RAM o disco completos de Windows.

La integración requiere identidad individual del exportador, TLS autenticado,
origen configurado por el operador, restricciones de red y colectores mínimos
de CPU, memoria y disco. El usuario del exportador sólo obtiene los permisos
necesarios para esas lecturas; el portal no recibe Docker socket, SSH, acceso
administrativo Windows ni una URL arbitraria para sondear.

Las muestras deben incluir nodo, alcance (`host`, `vm` o `container`), momento,
unidades y versión del agente, con límite de tamaño y caducidad. Mientras no
exista esa fuente autorizada e integrada, el Centro de Control muestra las
capacidades **declaradas** y marca las mediciones reales como **no disponibles**.
No se deduce uso de CPU o RAM a partir de los límites de Compose.
