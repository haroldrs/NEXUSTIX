# Centro de Control del Clúster

El apartado **Centro de Control del Clúster** está integrado en la administración
existente. Usa el inventario generado por `despliegue-nodos` y observaciones de
las API. Requiere sesión de administrador y no ofrece acciones para ejecutar
Docker, modificar inventarios, detener nodos o promover PostgreSQL.

El generador sigue siendo el único paso que convierte roles declarados en
Compose y pools HAProxy. La guía de preparación, modo espejo y ampliaciones está
en [despliegue por nodos](../despliegue-nodos/README.md).

## Qué muestra y qué significa

| Información | Fuente | Interpretación |
|---|---|---|
| Identificador, dirección, roles y dominio físico | Inventario confiable generado | Configuración declarada, no comprobación del host |
| CPU y RAM asignables, límites de contenedores | Inventario/generador | Presupuesto declarado, no uso real |
| Respuesta de API, frontend o gateway | Sondeo HTTP permitido por el inventario | Componente alcanzable desde ese observador |
| Rol de la DB conectada y canales WAL disponibles | Diagnóstico PostgreSQL de la API | Evidencia de esa conexión; la ubicación física puede seguir sin confirmarse |
| Primario y standbys asignados | Inventario | Roles previstos; no prueba de promoción o streaming |
| CPU, RAM y almacenamiento del host | Agente autorizado, si se integra posteriormente | No disponibles mientras falte esa fuente |
| Versión de despliegue | Metadatos declarados y publicados por la instancia | Identifica la entrega; no certifica que todos sus contenedores tengan la misma imagen |
| Resultado al perder un nodo | Cálculo de dependencias del inventario | Proyección; no fallo ejecutado ni resultado experimental |

La vista muestra resumen del clúster, tarjetas de nodos, detalle de componentes,
relaciones de replicación y cambios observados. Los refrescos no eliminan nodos
que dejan de responder. Se conservan su última respuesta conocida y el momento
de observación; un dato envejecido no pasa a ser una medición actual.

`No disponible` significa que los componentes sondeados no respondieron como se
esperaba desde ese observador. **No demuestra apagado físico.** Un sondeo omitido,
inventario inválido, falta de autorización, diagnóstico incompleto o fuente
ausente conserva estado desconocido. Un nodo dedicado exclusivamente a DB no se
declara sano por carecer de API a la que consultar.

El clúster no se declara operativo sólo porque responda el portal. Se consideran
las API necesarias, acceso y evidencia de dependencias; sin evidencia suficiente
se conserva un estado desconocido o degradado. La capacidad de escritura indicada
es diagnóstica: no se insertan registros de prueba para medirla. La disponibilidad
de conexiones tampoco garantiza completar todas las compras en curso.

## Observación redundante y límites

`GET /admin/operaciones/cluster` está preparado en Usuarios, Eventos y Compras.
Cada API verifica el JWT administrativo y obtiene su observación sin necesitar
guardar primero una fila en Compras. La interfaz puede consultar otro tipo de
API si falla el observador preferido. Esto reduce la dependencia de la captura
de Compras; no crea un plano de control independiente del despliegue.

En los **nuevos gateways generados**, las rutas `/admin/operaciones/cluster`
y sus subrutas, junto con `/diagnostico/instancia`, tienen un pool separado por
API. Comprueba `/health/observador` (sólo proceso, sin datos del clúster) y mantiene
afinidad por IP de cliente para favorecer la caché; esa afinidad no autentica ni
garantiza el mismo observador después de una caída. El tráfico de negocio sigue
en sus pools `roundrobin`, con `/health` dependiente de DB. No se han cambiado los
gateways demo/PC/VM existentes: para usar este encaminamiento hay que regenerar
y desplegar la nueva entrega por nodos cuando se autorice.

La autorización de estas rutas de observación y del buzón de anuncios valida
firma, emisor, audiencia, rol y caducidad del JWT **sin consultar Usuarios**.
Una cuenta revocada puede conservar ese acceso hasta que expire su access token
(600 segundos en la configuración generada). No se altera la autorización de
las rutas de negocio ni se habilita una sesión nueva cuando Usuarios está caído.
Los diagnósticos SQL conservan sus dependencias y permisos originales; si no
responden, sus datos se muestran desconocidos.

Los sondeos se limitan a destinos del inventario montado por el operador, con
timeouts, concurrencia, tamaño de respuesta y presupuesto de captura acotados.
El navegador no proporciona URLs arbitrarias de sondeo. Los endpoints de consulta
no instalan servicios, no modifican bases y no ejecutan comandos del sistema.

El refresco del navegador ocurre 30 segundos después de cada consulta y se
detiene al ocultar la sección, cambiar de vista o cerrar sesión. Cada observador
admite una captura HTTP simultánea y reutiliza la caché durante 25 segundos;
una captura explícita del Laboratorio puede coexistir. Hay ocho trabajadores
por captura, presupuesto de inicio de sondas de 20 segundos y respuestas de
hasta 128 KiB por destino, sin redirecciones ni proxies del entorno. Las sondas
omitidas permanecen desconocidas; se rota el orden inicial para inventarios
grandes. La interfaz limita cada petición a 25 segundos y puede recurrir a
otro observador. Una muestra de más de 90 segundos se marca como antecedente,
no como estado actual.

Una réplica API que no pudo iniciar por su DB tampoco puede ser observador. Si
ninguna API administrativa responde, no hay captura nueva; si Usuarios no está
disponible, iniciar sesión o renovar la autorización puede impedirse aunque
otro observador siga vivo. El frontend, el gateway y la conectividad del cliente
continúan siendo dependencias. La propuesta de una fuente de host independiente
está en [Coordinación](../despliegue-nodos/COORDINACION.md#métricas-del-host-sin-dar-control-del-host-al-portal).

Los historiales recientes y últimas respuestas se mantienen **en memoria por
observador**, con límites. Pueden ser parciales, diferir entre réplicas y perderse
al reiniciar el proceso. Cada evento identifica su origen; repetir el mismo estado
en otro sondeo no añade una transición nueva en ese observador. Cambiar de
observador no equivale a que haya ocurrido un fallo o recuperación del clúster.

La captura explícita de evidencias del Laboratorio sigue usando su almacenamiento
existente en Compras. Sólo esas capturas guardadas tienen la persistencia de la
DB de Compras; el refresco de pantalla no es un archivo histórico permanente.
No hay consenso ni replicación entre las memorias de los observadores.

Se retienen hasta 256 transiciones por proceso y 200 por sesión del navegador.
Un nodo antes observado necesita dos capturas completas sin respuesta para
clasificarse como no disponible; sin respuesta previa, sigue desconocido. Los
cambios de identidad SQL del primario se registran como observados, nunca como
failover seguro. `cluster_name` identifica la copia configurada; el identificador
de sistema y timeline dependen de los permisos de diagnóstico. Los permisos
añadidos al bootstrap sólo se aplicarán en bases nuevas: no se ejecutó un `GRANT`
ni se actualizó una base existente.

## Anuncios e incorporación de computadoras

El descubrimiento disponible es **un anuncio firmado importado**, no un escaneo
de la LAN ni incorporación automática. Una computadora genera una identidad
Ed25519 privada fuera del repositorio y firma un documento con sus datos
declarados. El operador traslada ese documento al administrador por un canal
apropiado; no distribuye la clave privada ni reutiliza secretos JWT o DB.

La importación administrativa usa
`POST /admin/operaciones/cluster/anuncios`. Valida formato, firma, fechas y
repetición del anuncio, con almacenamiento temporal acotado. No conecta a la
dirección anunciada. Una firma válida sólo demuestra posesión de la clave
incluida: **no demuestra que el equipo sea confiable**.

La incorporación mantiene etapas separadas:

1. **Detectado:** anuncio válido recibido. Los datos de hardware, dirección y
   plataforma todavía son declaraciones del solicitante.
2. **Identidad validada:** el administrador compara por otro canal la huella
   SHA-256 de los bytes públicos Ed25519 con la identidad del equipo esperado.
3. **Autorizado:** fija la huella verificada en `identity_fingerprint` del nodo
   dentro del YAML confiable. No existe aprobación de identidad basada sólo
   en la IP ni en pulsar un botón del anuncio.
4. **Configurado:** asigna roles, recursos, dominio físico y copias PostgreSQL
   en ese inventario. El generador valida compatibilidad y presupuesto declarado.
5. **Incorporado al despliegue:** regenera conservando la instalación mediante
   `--previous` y distribuye la carpeta correspondiente por un canal seguro.
6. **Activación autorizada y respuesta:** el operador ejecuta el despliegue en
   esa PC y los observadores reciben evidencia válida. Una fila de inventario
   o un anuncio nunca activa contenedores por sí mismos.

Los nodos preexistentes que no tienen huella fijada siguen siendo destinos
configurados por el operador; no se presentan como identidades criptográficas
verificadas. Una nueva huella exige otra comprobación externa y actualización
del inventario. La coincidencia de dirección con un nodo conocido no sustituye
esa comprobación.

Un anuncio caducado o un candidato perdido de la memoria puede necesitar una
nueva importación. No se trata de un registro duradero de autorizaciones. Las
autorizaciones persistentes son las huellas del inventario confiable, no los
anuncios del navegador. Incorporar una computadora de aplicaciones nunca añade
un votante al consenso ni cambia el primario de una base.

### Preparar un anuncio fuera del repositorio

Son comandos para uso posterior del operador; no se ejecutaron en esta entrega.
Instale las dependencias de `despliegue-nodos/requirements.txt` en el entorno
Python elegido. En cada computadora, desde Fedora o Ubuntu WSL (filesystem Linux),
cree **una sola identidad individual**, nunca compartida con otra PC:

```bash
python3 despliegue-nodos/anunciar.py identidad "$HOME/.config/nexustix/identidad-pc4"
```

El comando escribe `node-identity.pem` (privado, 0600) y `node-public.json` en un
directorio nuevo 0700. Muestra únicamente la huella pública. Para proponer `pc4`
al inventario del ejemplo `3-espejo.yml`, ajuste dirección, dominio y recursos a
los de esa PC y firme un archivo nuevo:

```bash
python3 despliegue-nodos/anunciar.py firmar \
  --key "$HOME/.config/nexustix/identidad-pc4/node-identity.pem" \
  --output "$HOME/.config/nexustix/identidad-pc4/anuncio-1.json" \
  --deployment espejo-tres --node-id pc4 --address 192.168.50.14 \
  --platform wsl2 --failure-domain maquina-4 \
  --cpus 4 --memory-mb 4096 --release demo-espejo-v1 --ttl 900
```

Transfiera **sólo `anuncio-1.json`**, e impórtelo en Administración → Centro de
Control → Anuncios. Es un archivo público firmado, no una credencial que permita
operar infraestructura. La dirección debe ser IPv4 privada LAN; `platform` admite
`fedora`, `wsl2` o `linux`. La firma dura como máximo 15 minutos y tolera hasta
60 segundos de adelanto de reloj. CPU se firma como millicores enteros para
evitar diferencias de serialización JSON. No se miden recursos ni se contacta al
nodo durante estos comandos.

Compare presencialmente o por otro canal confiable la huella pública y añádala
como `identity_fingerprint: "<64 caracteres hexadecimales>"` a la entrada de
ese nodo en **su inventario fuente**, junto con su asignación autorizada. No
copie la huella de un anuncio desconocido como si ya fuera confiable. Regenerar
con `--previous` conserva la instalación, según la guía de nodos; no ejecute una
preparación nueva si pretende ampliar una existente. Después de distribuir y
activar la entrega autorizada, un nuevo anuncio permite contrastar el pin.

El buzón acepta hasta 16 KiB, conserva 64 candidatos y 512 identificadores
recientes de anuncio por proceso. Reimportar el mismo anuncio vigente es
idempotente mientras permanezca en esa memoria. No es un registro global contra
replay: otra réplica o un reinicio puede recibirlo otra vez dentro de su vigencia,
sin conceder autoridad. Su expiración, firma y coincidencia con el inventario se
comprueban de nuevo. No se permite reutilizar una huella para varios nodos.

Los endpoints siguen el transporte HTTP del perfil LAN: la firma protege el
anuncio, **no cifra el JWT ni la sesión de administración**. Sólo usarlo en una
LAN confiable hasta preparar TLS en todos los puertos correspondientes.

## Resiliencia y recuperación

Las proyecciones del Laboratorio reutilizan el inventario y muestran servicios
afectados, réplicas supervivientes, dependencias y si una pérdida requiere
recuperación manual. La evidencia HTTP y WAL disponible acompaña esas
proyecciones sin convertirlas en resultados de un fallo real.

Sin un coordinador configurado y observado, el quórum de elección no existe como
medición disponible. El número de standbys WAL no se usa como voto de consenso.
El cambio del primario **declarado** no prueba failover; un diagnóstico de otra
conexión tampoco certifica por sí solo el aislamiento del anterior escritor.

HAProxy sí retira y reincorpora réplicas HTTP automáticamente. La promoción
PostgreSQL y la elección de una URL alternativa siguen el procedimiento manual
cuando no existe infraestructura externa de acceso estable comprobada. Consulte
[recuperación](../despliegue-nodos/RECUPERACION.md) y
[condiciones de coordinación segura](../despliegue-nodos/COORDINACION.md).

No se ejecutaron pruebas, fallos, migraciones, anuncios en red ni despliegues
para preparar estos cambios. Quedan por comprobar las PC Fedora/Windows WSL,
sus rutas y permisos, la replicación y la continuidad del observador y del
acceso de clientes ante fallos reales autorizados.
