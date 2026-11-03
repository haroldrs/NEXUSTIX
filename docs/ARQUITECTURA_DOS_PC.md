# Evolución y topología de tres VMs

La preparación vigente está en [despliegue-vm/README.md](../despliegue-vm/README.md):
VM app, VM de tres primarios, VM de tres standbys, puertos y secretos explícitos.
Los manifiestos pasan validación Compose; no se desplegaron VMs ni se configuró
la red. La aplicación ya usa URLs parametrizadas y libpq multi-host.

El diseño de dos PC que sigue permanece como alternativa conceptual. No usar
sus comandos sobre el Fedora actual durante esta fase. Ninguna de las dos
propuestas tolera perder el único equipo físico que aloja todas las VMs.

---

# Despliegue posible en dos PC físicas

**Estado:** diseño. No se ha desplegado en dos equipos. La versión principal
funciona y se reconstruye en **una** PC; este documento describe cómo
repartirla y qué ofrecería. (Versión anterior: `historico/ARQUITECTURA_DOS_PC_2026-10-24.md`.)

## 1. Límite de dos nodos

Con dos máquinas no hay quórum: si se corta la red, cada PC ve a la otra
"caída" y no puede distinguir una caída real de una partición. Si ambas
aceptaran escrituras se venderían las mismas entradas dos veces. Por eso:

- PostgreSQL tiene **un solo primario** por base; la promoción del standby es
  **manual** (`scripts/db_ha.py promote`) y exige que el operador confirme que
  el primario antiguo está apagado o aislado (`--primario-aislado-manualmente`).
- Para failover automático se necesita un tercer voto (otra PC, una VM o una
  Raspberry Pi con etcd / el monitor de `pg_auto_failover`) y fencing
  verificable.

## 2. Distribución propuesta

```
            LAN 192.168.1.0/24  (IPs de ejemplo, configurables)
   PC-A 192.168.1.10                         PC-B 192.168.1.20
 ┌──────────────────────────────┐        ┌──────────────────────────────┐
 │ frontend :8080               │        │ frontend :8080 (espejo)      │
 │ compras_lb :8005 → c1, c2    │        │ compras_lb :8005 → c2, c1    │
 │ usuarios :8001  eventos :8002│        │ compras2 :8004               │
 │ compras1 :8003               │        │                              │
 │ usuarios_db  eventos_db      │ WAL →  │ usuarios_db_replica          │
 │ compras_db   (primarios)     │ ─────► │ eventos_db_replica           │
 │                              │        │ compras_db_replica (standby) │
 └──────────────────────────────┘        └──────────────────────────────┘
```

| Puerto | PC | Abrir en firewalld para | Notas |
|---|---|---|---|
| 8080 | A y B | clientes | frontend |
| 8001, 8002 | A | clientes y PC-B | Usuarios, Eventos |
| 8003 / 8004 | A / B | el balanceador de la otra PC | réplicas de Compras |
| 8005 | A y B | clientes | balanceador |
| 5432–5434 | A | **solo** IP de PC-B | PostgreSQL (replicación y conexiones de compras2) |
| 5432–5434 | B | **solo** IP de PC-A | standbys (para reincorporar A tras una promoción) |

```bash
sudo firewall-cmd --permanent --add-port=8080/tcp --add-port=8001-8005/tcp
sudo firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=192.168.1.20/32 port port=5432-5434 protocol=tcp accept'
sudo firewall-cmd --reload
```

## 3. Qué cambia respecto a una PC

1. **Red entre equipos.** Los Compose actuales usan una red bridge local; los
   nombres `compras_db`, `eventos` no resuelven en la otra PC. Opciones:
   - Docker Swarm con red *overlay* cifrada (los nombres de servicio siguen
     funcionando; requiere `docker stack` y secretos de Swarm), o
   - publicar los puertos en la LAN y usar IPs en las URL: en
     `*-database-url-ha` `host=192.168.1.10:5434&host=192.168.1.20:5434`, en
     `EVENTOS_URL` `http://192.168.1.10:8002`, en `compras_lb/nginx.conf`
     `server 192.168.1.20:8004`.
2. **Frontend:** `python3 scripts/nx.py demo secretos --public-host 192.168.1.10`
   genera `config.js` con la IP; `NEXUSTIX_FRONTEND_ORIGINS` debe incluir
   `http://192.168.1.10:8080` y `http://192.168.1.20:8080`;
   `NEXUSTIX_BIND_ADDRESS=0.0.0.0`.
3. **Secretos:** la misma clave pública JWT y el mismo token de servicio en
   ambas PC; la clave privada solo donde corre Usuarios; contraseñas de base y
   de replicación iguales en los dos nodos de cada base. Copiarlos con `scp`
   a una carpeta 0700 y etiquetarlos para SELinux.
4. **Replicación entre hosts:** el overlay se adapta poniendo
   `NEXUSTIX_PG_UPSTREAM_HOST=192.168.1.10` y el puerto correspondiente en
   los standbys de PC-B. Con latencia LAN se puede activar la replicación
   síncrona (`db_ha.py sync <base> on`) a costa de bloquear escrituras si cae PC-B.
5. **Tráfico sin cifrar:** en una LAN de laboratorio es aceptable; fuera de
   ella se requiere TLS (Nginx con certificados, `sslmode=require` en PostgreSQL)
   y `NEXUSTIX_AUTH_COOKIE_SECURE=true`.

## 4. Qué ocurre si falla cada PC

| Falla | Efecto inmediato | Recuperación | ¿Alta disponibilidad? |
|---|---|---|---|
| Se apaga **PC-B** | Se pierden compras2, su balanceador/frontend espejo y los standbys. PC-A sigue vendiendo: su balanceador marca compras2 caída y usa compras1. Con replicación síncrona, las escrituras se bloquean hasta `db_ha.py sync <base> off`. | Al volver PC-B: los standbys se ponen al día por WAL (slot); si estuvieron fuera más de 1 GiB de WAL, `rejoin`. | Parcial: servicio continúa en PC-A. |
| Se apaga **PC-A** | Caen Usuarios, Eventos, compras1 y los tres primarios: **no se puede comprar ni iniciar sesión** aunque PC-B siga encendida. | Operador confirma que PC-A está apagada, `promote` de las tres bases en PC-B (`--primario-aislado-manualmente`) y arranca Usuarios/Eventos en PC-B (requiere la clave privada JWT allí). Al volver PC-A, **antes** de arrancar sus bases: `rejoin` como standby. Pérdida posible: lo no replicado (modo asíncrono). | No: hay intervención manual y minutos de corte. |
| Se corta la **red** entre PCs | Cada PC ve a la otra caída. PC-B no tiene bases primarias: sus compras fallan (no hay escrituras divergentes). | Restablecer red; los standbys se ponen al día. | — |
| Caen los dos balanceadores o el switch | Los clientes no llegan al sistema. | Restaurar red / arrancar Nginx. | No. |

## 5. Limitaciones de esta arquitectura

- Usuarios y Eventos siguen siendo instancias únicas (en PC-A).
- Sin IP virtual: un cliente que use el balanceador de la PC caída debe
  cambiar manualmente a la otra URL.
- Promoción manual; sin quórum no es seguro automatizarla.
- No se ha probado en hardware real: tiempos de recuperación y RPO medidos en
  una sola PC (prueba `test_r21`) no son extrapolables a la LAN.

**Conclusión:** dos PC añaden redundancia de datos y de la réplica de Compras,
pero **no** alta disponibilidad de extremo a extremo: la caída de la PC con
los primarios, Usuarios y Eventos interrumpe el servicio hasta una
intervención manual.
