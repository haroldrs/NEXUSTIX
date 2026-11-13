# Alta disponibilidad automática: varias PC + árbitro

Con este modo, si se apaga **cualquier** computadora (incluida la que corre todos
los servicios), el sistema sigue funcionando solo:

| Capa | Qué pasa cuando una PC se apaga | Tiempo medido en el laboratorio |
|---|---|---|
| Servicios (Usuarios, Eventos, Compras) | Los gateways de las otras PC dejan de enviarle solicitudes | ~6 s (2 sondeos fallidos cada 3 s) |
| Navegador | Las consultas siguientes se envían solas a otro gateway, sin recargar la página | Siguiente solicitud |
| Bases de datos | Patroni promueve la réplica más actualizada; los servicios se reconectan solos | ~37 s (ver tabla de resultados) |
| Coordinador del clúster | Las instancias que quedan eligen otro con el algoritmo Bully | ~6–8 s |
| Al volver la PC | Sus servicios regresan al reparto y su base se reincorpora como réplica (pg_rewind) | ~1 min |

## Cómo funciona el árbitro

Cada base tiene una **llave** guardada en **etcd**: solo quien la tiene acepta escrituras.

* La llave dura **30 s**. La base principal la renueva cada **10 s**, y para
  renovarla necesita la aprobación de la **mayoría** de votantes (2 de 3).
* Si la principal se apaga, deja de renovar. Al vencer la llave, la otra PC y el
  árbitro (2 de 3) permiten que **Patroni** promueva la réplica.
* Si la principal no se apagó sino que **quedó aislada de la red**, tiene 1 de 3
  votos: no puede renovar la llave y Patroni la pasa a **solo lectura**. Así
  nunca hay dos bases principales (*split-brain*).
* Con solo dos votantes no se puede desempatar: por eso el tercero.

El árbitro solo ejecuta etcd (unos 100 MB de RAM). Una laptop vieja basta. **No
debe ser una máquina virtual dentro de una de las otras PC**: caería junto con ella.

| Votantes | Caídas que tolera |
|---|---|
| 3 | 1 |
| 5 | 2 |

No todos los nodos votan: un clúster de 6 PC puede tener 3 votantes y el resto
solo con servicios y réplicas.

## Inventario

Ejemplo listo: [`examples/6-dos-pc-arbitro.yml`](examples/6-dos-pc-arbitro.yml).

```yaml
coordination:
  mode: patroni            # prepared_inactive = solo documentado; patroni = automático
  voters: [pc1, pc2, arbitro]
nodes:
  - {id: arbitro, address: 100.101.1.13, platform: wsl2, failure_domain: laptop-arbitro,
     resources: {cpus: 1, memory_mb: 512}, roles: []}   # sin roles: solo vota
```

Para crecer a 3 o más PC con servicios: agregue el nodo con `roles` (o a
`mirror_nodes`), póngalo como réplica en `databases` y, si reemplaza al árbitro,
cámbielo en `voters`. Reparta las principales (`primary`) entre nodos distintos
para compartir la carga de escritura.

## Conectar PC de redes distintas: Tailscale

Las PC del laboratorio están en redes diferentes. Tailscale crea una red privada
entre ellas aunque estén en edificios o casas distintas.

1. Instale Tailscale en cada PC (Fedora: `sudo dnf install tailscale && sudo systemctl enable --now tailscaled && sudo tailscale up`;
   Windows: instalador de tailscale.com) e inicie sesión con **la misma cuenta**.
2. Anote la IP `100.x.y.z` de cada PC (`tailscale ip -4`).
3. Compruebe que se ven: `ping 100.x.y.z` desde cada una.
4. Use esas IP en `address` del inventario. El código no cambia.

En Windows, Docker Desktop publica los puertos en todas las interfaces, incluida
la de Tailscale. Si el firewall de Windows pregunta, permita Docker en redes privadas.

Un compañero puede unirse desde su casa como **árbitro** sin problema. Como nodo
con base síncrona funciona, pero cada compra esperará la latencia de Internet.

## Instalar sin descargas en el laboratorio

En casa, con buena conexión:

```bash
python3 scripts/paquete_offline.py crear --salida ~/nexustix-imagenes.tar
```

Copie el archivo a un USB. En cada PC del laboratorio:

```bash
python3 scripts/paquete_offline.py cargar --entrada /ruta/del/usb/nexustix-imagenes.tar
```

Prepare la entrega con `--imagenes-offline` para que los Compose usen esas
imágenes sin construir ni descargar:

```bash
python3 despliegue-nodos/nodos.py preparar inventario.yml ~/nexustix-entrega --imagenes-offline
```

## Orden de arranque

1. En cada votante (las PC y el árbitro), primero `etcd`. etcd espera a la mayoría:
   arranque los tres en pocos minutos.
2. Las bases (`*_db`): la principal declarada inicia el clúster y las réplicas se clonan solas.
3. Servicios y gateways.

`launch-order.json` de cada nodo lista estos grupos.

## Probar el failover

* **Con Docker en una sola máquina:** `python3 tests/ha/laboratorio_patroni.py`
  simula 2 PC + árbitro, apaga la principal, la reincorpora y la aísla de la red.
  Imprime los tiempos medidos.
* **Con las PC reales:** apague la PC que tiene la base principal y observe el
  Centro de Control: en unos 40 s la otra PC figura como principal y las compras
  continúan. Al encenderla, vuelve como réplica.

## Resultados del laboratorio (Docker, 2 PC + árbitro simulados)

Ver [`docs/RESULTADOS_ALTA_DISPONIBILIDAD.md`](../docs/RESULTADOS_ALTA_DISPONIBILIDAD.md).

## Límites

* El tráfico entre nodos va sin cifrar (Tailscale sí lo cifra entre PC).
* Las solicitudes que estaban en curso en la PC que se apagó pueden fallar; las
  compras usan clave de idempotencia y se pueden reintentar sin duplicar.
* Si el navegador recarga la página desde la dirección de la PC caída, debe
  abrir la dirección de otra PC: la página ya cargada sí cambia sola.
* La renovación de sesión usa una cookie de la dirección original: tras cambiar
  de gateway, la sesión dura hasta que vence el token (10 min) y luego se pide
  iniciar sesión otra vez.
