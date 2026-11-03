# Estado actualizado — versión LAN mínima preparada

La primera versión ya tiene Compose por nodo, HAProxy con salud activa,
secretos por rol, guardas y ensayo de continuidad. PostgreSQL permanece
centralizado en PC 2; no se ha desplegado entre equipos.

La guía ejecutable es [despliegue-pc/README.md](../despliegue-pc/README.md).
Los roles coinciden con la propuesta siguiente. La replicación/promoción es
una segunda fase y los apartados de esa propuesta sobre standby no deben
aplicarse al despliegue mínimo. Resultados en [PUBLICACION_Y_LAN.md](PUBLICACION_Y_LAN.md).

---

## Propuesta original conservada como contexto

# Plan de distribución en tres PC físicas

**Diseño pendiente de implementación y prueba. No desplegado.** No hay IPs,
credenciales ni capacidades confirmadas de los equipos. Los nombres de variables
de este documento son un contrato propuesto, no variables que los Compose
actuales interpreten automáticamente. No se cambió ningún manifiesto actual.

## Roles y decisión sobre PostgreSQL

| Equipo | Rol solicitado | Propuesta condicionada a recursos |
|---|---|---|
| PC 1 | Frontend, Nginx de entrada y Compras A | Mantener sin bases para no unir el punto de entrada con la persistencia |
| PC 2 | Compras B, Usuarios A, Eventos A | Alojar inicialmente los tres primarios PostgreSQL, si tiene SSD, RAM y disponibilidad suficientes |
| PC 3 | Usuarios B, Eventos B | Incorporar después los tres standbys PostgreSQL, con capacidad de asumir las escrituras de PC 2 |

Se propone concentrar los primarios en PC 2 por administración y dependencia
cercana de los servicios, no por mediciones inexistentes. PC 2 concentra riesgo;
antes de desplegar se debe confirmar o revisar esta asignación según recursos.
No usar PC 3 simultáneamente como primario y su propio standby. Un standby por
base no es una copia de seguridad independiente.

Presupuesto inicial orientativo para un laboratorio pequeño, **no capacidad
medida**: PC 1 reservar 2 CPU lógicas y 4 GiB disponibles; PC 2 y PC 3, 4 CPU y
8 GiB disponibles cada una. Reservar inicialmente 20 GiB libres para imágenes y
código de PC 1; 40 GiB libres más datos, WAL y backups para cada PC de bases.
Medir y ajustar: disco necesario = imágenes + datos de tres clústeres + WAL
retenido + espacio de clonado/reintegración + backups + margen de crecimiento.
No asignar toda la RAM del SO ni prometer rendimiento con estos mínimos propuestos.

| Dato a completar | PC 1 | PC 2 | PC 3 |
|---|---|---|---|
| Responsable y acceso administrativo | Pendiente | Pendiente | Pendiente |
| SO, arquitectura y versiones Docker/Compose | Pendiente | Pendiente | Pendiente |
| CPU/RAM disponibles y SSD/libre | Pendiente | Pendiente | Pendiente |
| IP LAN reservada / DNS que resuelve en contenedores | Pendiente | Pendiente | Pendiente |
| Disponibilidad, suspensión automática, energía | Pendiente | Pendiente | Pendiente |

## Contrato de configuración por rol

Proponer nuevos archivos `despliegue-pc/compose.pc1.yml`, `compose.pc2.yml`,
`compose.pc3.yml` y una plantilla Nginx específica, aún no creados. Deben usar
imágenes y volúmenes nuevos, prefijo propuesto `nexustix-lan`, sin reutilizar
`nexustix`, `project`, `nexustix_auth_staging`, `nexustix-distributed-lab` ni VM.
El identificador de instalación debe estar también en nombres explícitos de
volúmenes/redes: cambiar solo `COMPOSE_PROJECT_NAME` no basta.

| Variable propuesta | Valor pendiente / función |
|---|---|
| `LAN_PC1_HOST`, `LAN_PC2_HOST`, `LAN_PC3_HOST` | IP/DNS reales reservados; ningún default inventado |
| `LAN_BIND_IP`, `LAN_DEPLOYMENT_ID`, `LAN_ROLE` | Identidad local verificada antes de operar |
| `LAN_FRONTEND_PORT`, `LAN_GATEWAY_PORT` | Entrada al portal y APIs; TLS pendiente de certificados/DNS |
| `LAN_COMPRAS_PORT`, `LAN_USUARIOS_PORT`, `LAN_EVENTOS_PORT` | Puertos publicados de las API por equipo, sin colisión local |
| `LAN_PG_USUARIOS_PORT`, `LAN_PG_EVENTOS_PORT`, `LAN_PG_COMPRAS_PORT` | Tres puertos diferentes en PC 2 y PC 3, dirigidos al 5432 interno respectivo |
| `LAN_SECRETS_DIR` | Directorio externo privado, por equipo y rol |
| `DATABASE_URL_FILE`, `EVENTOS_URL`, `FRONTEND_ORIGINS` | Variables existentes que recibirán endpoints LAN/balanceados |
| `JWT_PRIVATE_KEY_FILE`, `JWT_PUBLIC_KEY_FILE`, `EVENTOS_SERVICE_TOKEN_FILE` | Archivos existentes; distribuir por necesidad, nunca por Git |

Proponer gateway de Usuarios/Eventos en PC 1 además del balanceador Compras.
Es una adaptación de configuración Nginx pendiente, no una capacidad actual.
Para que Compras siga llegando a Eventos si cae PC 2, su `EVENTOS_URL` deberá
apuntar al gateway con backends PC 2 y PC 3. El navegador usará el gateway,
no `localhost` (que sería la PC del comprador). La alternativa de gateways
locales por PC y un segundo punto de entrada exige un diseño adicional.

| Flujo | Destino permitido |
|---|---|
| Navegadores LAN | PC 1: puertos frontend/gateway elegidos |
| Gateway PC 1 | Compras A local y Compras B en PC 2; Usuarios/Eventos en PC 2 y PC 3 |
| Compras en PC 1/2 | Gateway Eventos; primario de Compras en PC 2 o futuro promovido en PC 3 |
| Usuarios/Eventos PC 2/3 | Primario del dominio en PC 2 o futuro promovido en PC 3 |
| Standbys PC 3 | PC 2: puertos PostgreSQL correspondientes, rol replicación |
| Reintegración futura PC 2 | PC 3: nuevo primario de cada dominio |
| Operador autorizado | Acceso administrativo confirmado por equipo |

Publicar solo en IP LAN elegida; limitar accesos a bases y APIs a sus pares.
No se aplican reglas aquí. Verificar el origen que PostgreSQL ve después de
NAT de Docker para redactar HBA/firewall correctos. No asumir que editar
`/etc/hosts` del host hace resolver nombres dentro de contenedores. CORS y
cookies se deben probar desde otro navegador; HTTP LAN expone credenciales y
tráfico, por lo que TLS es un pendiente previo a datos reales.

Los `depends_on` solo ordenan servicios del mismo Compose. La disponibilidad
remota requiere preflight y reintentos acotados. Healthcheck de contenedor no
quita automáticamente un backend de Nginx: el Nginx actual usa detección pasiva.
Comprobar `/health` de cada API y una operación sintética por el gateway, además
de `/lb-health`. Evitar reintentos inseguros de operaciones no idempotentes.

## Secretos y replicación

Las réplicas Usuarios comparten la clave de firma y la base de sesiones; las
otras API reciben solo la clave pública. Ambas Compras comparten URL de base,
credencial interna de Eventos y políticas de operación. Los secretos se crean
una vez para este despliegue nuevo y se entregan por canal privado, en carpetas
0700/archivos 0600, según rol. No generar bases independientes por compañero.

Cada primario necesita WAL de réplica, un slot por standby, rol replicador y
HBA limitado; cada standby se clona a volumen nuevo con `pg_basebackup` y
`standby.signal`. Usar la misma versión PostgreSQL 15 y configuración compatible.
Tomar como referencia `despliegue-vm/postgres/`, conservando guardas de clones
parciales y fencing. Los scripts `vm.py`, instalación y red **rechazan hosts
físicos**: no quitar sus guardas; implementar posteriormente un controlador PC
con identidad/recursos protegidos explícitos.

Verificar por base: primario `pg_is_in_recovery() = false`; standby `true`;
`pg_stat_replication.state = streaming`; receptor WAL activo, lag/LSN y slots
sin acumulación excesiva. Una prueba que acepta un solo host accesible no
acredita replicación. Controlar disco de WAL y archivado también tras promover.

Ante caída o partición: suspender nuevas escrituras si la autoridad es incierta;
confirmar fencing real del antiguo primario por un canal independiente (energía
o aislamiento verificable y persistente), registrar operador/hora/evidencia y
solo entonces promover. Un ping fallido no prueba fencing. Verificar que existe
un único escritor por base. Las URL multihost con `target_session_attrs=read-write`
permiten reconectar a PC 3; repetir solicitudes con su clave y comprobar efectos,
no dar por terminadas operaciones cuya respuesta se perdió.

Con replicación asíncrona existe RPO mayor que cero; medirlo. Promoción parcial
de los tres dominios exige diagnóstico y reconciliación, no reintento ciego.
Reintegrar el antiguo primario con nuevo volumen/generación desde el nuevo
primario, conservando el volumen anterior sin arrancarlo. No hay failback
automático ni garantía de cero pérdida. Diseñar también una segunda conmutación.

## Orden futuro y criterios para avanzar

1. Revisar publicación, clonar mismo commit, registrar recursos/IPs/puertos y
   confirmar acceso administrativo/fencing, respaldo externo y ventana de prueba.
2. Implementar manifiestos y gateway nuevos; validar Compose sin arrancar;
   comparar nombres/volúmenes/secretos con todos los recursos existentes.
3. Generar/distribuir secretos nuevos por rol, verificar permisos y TLS/red con
   autorización expresa. No usar datos ni volúmenes actuales de Fedora.
4. Iniciar primarios en PC 2 con volúmenes nuevos; verificar salud, HBA y escritor
   único. Iniciar Usuarios/Eventos A y comprobar migraciones antes de continuar.
5. Iniciar Usuarios/Eventos B en PC 3 contra los mismos primarios. Configurar
   gateway PC 1 y comprobar cada backend desde donde lo consumirá el sistema.
6. Iniciar Compras A/B, frontend y comprobar login, catálogo, compra/replay y
   correlación. El paso debe funcionar sin standby antes de añadir replicación.
7. Añadir los tres standbys en PC 3; verificar streaming y datos por dominio.
8. Con autorización posterior, probar fallos sintéticos con evidencia antes y
   después; medir RTO/RPO y recuperación de pedidos/QR/inventario.

| Ensayo pendiente | Resultado honesto esperado |
|---|---|
| Caída Compras A/B | Otra réplica responde por gateway; mismo pedido no se duplica |
| Caída PC 1 | Portal/gateway indisponibles: **punto único de fallo** |
| Caída PC 2 sin standbys | Bases indisponibles; réplicas API no solucionan pérdida de persistencia |
| Caída PC 2 con standbys | Recuperación manual tras fencing/promoción; verificar los tres dominios y Compras A |
| Caída PC 3 | Continúa PC 2, sin redundancia de datos; vigilar WAL/slots |
| Partición PC 2–PC 3 | No promover por timeout; evitar dos escritores; medir retraso y reconciliar |
| Retorno PC 2 | Antiguas bases siguen aisladas; nuevas réplicas solo lectura |
| Pérdida de LAN/energía común | Switch/router/electricidad son fallos compartidos; tres PC no eliminan ese riesgo |

No hay comandos `up`, fencing ni firewall listos para ejecutar en PC en esta
entrega: faltan manifiestos físicos, inventario real y autorización operativa.
La tolerancia a una PC completa no está demostrada y la entrada en PC 1 es única.
