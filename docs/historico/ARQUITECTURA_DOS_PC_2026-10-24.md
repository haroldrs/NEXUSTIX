# Arquitectura de Despliegue Distribuido en Dos PC Físicas — NEXUSTIX

**Fecha de corte:** 2026-10-24  
**Estado:** Especificación de diseño técnico para despliegue distribuido real. No desplegado en hardware físico.  
**Advertencia de seguridad obligatoria:** La carpeta `multi-laptop/` contiene configuraciones obsoletas heredadas anteriores a la arquitectura de autenticación JWT y secretos montados. **No debe utilizarse ni ejecutarse** sin adaptarla a los contratos y variables actuales.

---

## 1. Fundamentos y Límite Teórico de Dos Nodos

En sistemas distribuidos, dos máquinas físicas **no pueden alcanzar consenso seguro ni tolerar particiones de red (CAP)** sin riesgo de inconsistencia si ambas intentan admitir escrituras de forma independiente.

### El problema del Split-Brain y la ausencia de quórum
- Si la conexión LAN entre PC-A y PC-B se interrumpe (corte de cable, fallo del switch o firewall), PC-B no puede distinguir si PC-A ha colapsado o si simplemente la red se ha particionado.
- Si PC-B promoviera automáticamente una base de datos secundaria a primaria mientras PC-A sigue activa y aceptando escrituras, se produciría un **split-brain** irrecuperable: ventas divergentes del mismo inventario con doble gasto de stock.
- Con solo 2 nodos no existe quórum de mayoría simple ($N/2 + 1 = 2$). Cualquier decisión de failover automático requiere un tercer dominio (árbitro/testigo/witness) o fencing verificable (STONITH / desconexión de energía).
- **Decisión de diseño:** Por seguridad de datos, **no se habilita failover automático de PostgreSQL** en este diseño de dos PC. Ante la caída del nodo con persistencia, el failover es estrictamente **manual y controlado**, preservando la integridad del inventario por encima de la disponibilidad ciega.

---

## 2. Topología de Distribución de Servicios

Se propone una arquitectura con **PC-A como Autoridad de Persistencia y Maestro** y **PC-B como Nodo de Carga y Réplica de Aplicación**:

```text
                                 RED LOCAL (LAN 192.168.1.0/24)
                                                │
                 ┌──────────────────────────────┴──────────────────────────────┐
                 │                                                             │
                 ▼                                                             ▼
     [ PC-A : 192.168.1.10 ]                                       [ PC-B : 192.168.1.20 ]
  ┌─────────────────────────────────┐                           ┌─────────────────────────────────┐
  │ Frontend Nginx (:8080)          │                           │ Frontend Nginx Espejo (:8080)   │
  │ Compras LB Nginx (:8005)        │                           │ Compras LB Nginx Espejo (:8005) │
  │ ├─ Compras1 (:8003)             │                           │ └─ Compras2 (:8003 local)       │
  │ └─ [hacia Compras2 en PC-B]     │                           │                                 │
  │ Usuarios API (:8001)            │                           │ (Sin APIs adicionales si se     │
  │ Eventos API (:8002)             │                           │  mantiene persistencia única)   │
  │ ─────────────────────────────── │                           │ ─────────────────────────────── │
  │ usuarios_db (PostgreSQL 5432)   │                           │                                 │
  │ eventos_db  (PostgreSQL 5433)   │                           │ Standby de réplica (opcional,   │
  │ compras_db  (PostgreSQL 5434)   │                           │ solo lectura o pasivo)          │
  └─────────────────────────────────┘                           └─────────────────────────────────┘
```

### Tabla de asignación de servicios y puertos

| Servicio | PC Asignada | Puerto Host | Puerto Contenedor | Dependencias |
|---|---|---|---|---|
| **frontend-A** | PC-A (192.168.1.10) | `8080` | `80` | Archivos estáticos |
| **frontend-B** | PC-B (192.168.1.20) | `8080` | `80` | Archivos estáticos (apunta a LB y APIs LAN) |
| **usuarios_db** | PC-A | `5432` (LAN firewall) | `5432` | Volumen `usuarios_data` en PC-A |
| **usuarios** | PC-A | `8001` | `8001` | `usuarios_db`, clave privada JWT |
| **eventos_db** | PC-A | `5433` (LAN firewall) | `5432` | Volumen `eventos_data` en PC-A |
| **eventos** | PC-A | `8002` | `8002` | `eventos_db`, clave pública JWT, token servicio |
| **compras_db** | PC-A | `5434` (LAN firewall) | `5432` | Volumen `compras_data` en PC-A |
| **compras1** | PC-A | `8003` | `8003` | `compras_db` local, `eventos` local |
| **compras_lb-A** | PC-A | `8005` | `80` | `compras1` (local) y `compras2` (remoto PC-B) |
| **compras2** | PC-B (192.168.1.20) | `8003` | `8003` | `compras_db` (192.168.1.10:5434), `eventos` (192.168.1.10:8002) |
| **compras_lb-B** | PC-B | `8005` | `80` | `compras2` (local) y `compras1` (remoto PC-A) |

---

## 3. Comunicación Entre Servicios y Frontend Remoto

1. **Compras2 (en PC-B) hacia PostgreSQL (en PC-A):**
   - Variable `DATABASE_URL`: `${DATABASE_URL:?Configurar URL privada fuera de Git}`
   - Requiere que PostgreSQL en PC-A tenga configurado `listen_addresses = '*'` y `pg_hba.conf` autorice la subred local `192.168.1.20/32`.
   - El firewall de Fedora (firewalld) en PC-A debe abrir el puerto 5434 exclusivamente a la IP de PC-B.

2. **Compras2 (en PC-B) hacia Eventos (en PC-A):**
   - Variable `EVENTOS_URL`: `http://192.168.1.10:8002`
   - Las reservas, confirmaciones y liberaciones viajan por HTTP autenticado mediante `X-Service-Token`.

3. **Balanceadores Nginx (`compras_lb`):**
   - `compras_lb-A` (en PC-A):
     ```nginx
     upstream compras_cluster {
         server 127.0.0.1:8003 max_fails=2 fail_timeout=10s;        # compras1 local
         server 192.168.1.20:8003 max_fails=2 fail_timeout=10s;     # compras2 remoto
     }
     ```
   - `compras_lb-B` (en PC-B):
     ```nginx
     upstream compras_cluster {
         server 127.0.0.1:8003 max_fails=2 fail_timeout=10s;        # compras2 local
         server 192.168.1.10:8003 max_fails=2 fail_timeout=10s;     # compras1 remoto
     }
     ```

4. **Acceso desde el navegador:**
   - El comprador puede acceder indistintamente a `http://192.168.1.10:8080` (PC-A) o `http://192.168.1.20:8080` (PC-B).
   - En `frontend/config.js` se configuran los endpoints de la API apuntando a la IP local o al balanceador local de esa máquina.

---

## 4. Gestión de Secretos y Criptografía

| Secreto | Ubicación en PC-A | Ubicación en PC-B | Justificación |
|---|---|---|---|
| Clave Privada JWT (`jwt-private.pem`) | `/run/secrets/jwt_private` (solo `usuarios`) | **NO DEBE EXISTIR** | Principio de mínimo privilegio: solo el emisor de tokens firma. |
| Clave Pública JWT (`jwt-public.pem`) | `/run/secrets/jwt_public` (`compras1`, `eventos`) | `/run/secrets/jwt_public` (`compras2`) | Verificación asimétrica RS256 local en ambas máquinas. |
| Token de Servicio Eventos (`eventos-service-token`) | En `eventos` y `compras1` | En `compras2` | Autentica peticiones de reserva entre procesos en red LAN. |
| Credenciales DB | En `usuarios_db`, `eventos_db`, `compras_db`, `compras1` | En `compras2` (solo para `compras_db`) | PC-B solo conoce credenciales de `compras_db`. |

---

## 5. Análisis de Fallos: Matriz de Consecuencias en 2 PC

### Escenario A: Se apaga o desconecta la PC-B (Nodo de cómputo)
- **Impacto directo:** `compras2`, `compras_lb-B` y `frontend-B` se vuelven inaccesibles.
- **Limitaciones y degradación real observada:**
  1. **Clientes conectados a PC-B:** Todo usuario o script que estuviera accediendo a través de la IP de PC-B (`http://192.168.1.20:8080` o balanceador `:8005`) sufre interrupción total inmediata (`Connection Refused` o timeout). Al no existir una IP Virtual (VIP) con Keepalived ni conmutación DNS, estos clientes no migran automáticamente y deben redirigirse manualmente a la IP de PC-A.
  2. **Penalización de latencia en PC-A:** `compras_lb-A` detecta el fallo de conexión hacia `192.168.1.20:8003` tras un timeout de conexión de 2 segundos. Aunque Nginx reenvía la solicitud a `compras1` de forma transparente, cada solicitud que coincide con la rotación hacia el nodo caído sufrirá una latencia añadida de ~2.3 s hasta que Nginx marque a `compras2` como inactivo por `fail_timeout=10s`.
  3. **Operaciones en vuelo interrumpidas:** Si `compras2` cae durante el procesamiento de una compra tras haber persistido una reserva en `eventos_db`, dicha reserva queda en estado `reservada` y el claim en `processing`. El stock permanece retenido y no se recupera automáticamente hasta que expire el umbral stale (120 s) y un administrador invoque `/compras/recuperar-servidor`.
- **Resultado evaluado:** **Disponibilidad parcial degradada con failover de backend en PC-A.** No es una tolerancia transparente de alta disponibilidad global: requiere que los clientes apunten a PC-A y que se gestione la reconciliación de operaciones interrumpidas.

### Escenario B: Se apaga o desconecta la PC-A (Nodo maestro de persistencia)
- **Impacto:** Caen `usuarios`, `eventos`, `compras1` y las tres bases de datos (`usuarios_db`, `eventos_db`, `compras_db`).
- **Comportamiento en PC-B:**
  - `compras2` sigue en ejecución, pero cualquier petición de compra falla inmediatamente con error 503/timeout porque `compras_db` y `eventos` no responden en `192.168.1.10`.
  - El frontend en PC-B muestra errores de red al intentar registrarse, iniciar sesión o consultar catálogo.
- **Resultado:** **Interrupción del servicio.** No es posible realizar compras ni autenticarse.
- **Por qué esto es deseable frente al split-brain:** Es infinitamente mejor rechazar transacciones temporalmente (mantener consistencia en CAP) que aceptar compras a ciegas sin base de datos ni validación de inventario.

---

## 6. Procedimiento de Recuperación al Reincorporar un Equipo

### Al recuperarse PC-B:
1. PC-B arranca sus contenedores (`compras2`, `compras_lb-B`, `frontend-B`).
2. Verifica conectividad de red con PC-A (`ping 192.168.1.10`, `nc -zv 192.168.1.10 5434`).
3. `compras2` se conecta a `compras_db` en PC-A.
4. `compras_lb-A` en PC-A detecta el retorno de `compras2` en su siguiente intento de healthcheck pasivo y reanuda el round robin.
5. **No se requiere reconciliación de datos:** La base de datos siempre residió en PC-A; no hubo divergencia.

### Al recuperarse PC-A:
1. Arrancar PostgreSQL y verificar integridad de los volúmenes en PC-A.
2. Comprobar que los tres servicios de base de datos están `healthy`.
3. Arrancar `usuarios`, `eventos`, `compras1` y balanceador.
4. Ejecutar el endpoint de auditoría: `GET /compras/auditoria/consistencia` para comprobar que reservas y compras están cuadradas.
5. Si existían operaciones en vuelo interrumpidas por la caída, invocar `POST /compras/recuperar-servidor` para resolver estados ambiguos.
6. Habilitar la entrada de tráfico en los balanceadores.

---

## 7. Puntos Únicos de Fallo (SPOF) Remanentes

1. **Host PC-A:** Aloja la persistencia única. Su pérdida detiene el sistema.
2. **Switch/Router LAN:** La pérdida de conectividad física aísla a PC-B.
3. **`eventos_db` y `compras_db`:** Sin replicación activa en PC-B, cada base de datos es SPOF.
4. **Servicio Usuarios:** Solo existe una instancia en PC-A (no hay login ni refresh si PC-A falla).
5. **Servicio Eventos:** Solo existe una instancia en PC-A (no hay reservas de stock si PC-A falla).

---

## 8. Requisitos de Infraestructura para Habilitar Alta Disponibilidad Real

Para que la caída de PC-A no detenga el sistema, se requiere:
1. **Un tercer nodo (Testigo/Witness):** Por ejemplo una Raspberry Pi o una VM ligera para alcanzar quórum 2 de 3 en algoritmos como Raft o PostgreSQL Patroni.
2. **Replicación continua de PostgreSQL:** Streaming Replication síncrona o asíncrona de `compras_db` y `eventos_db` hacia PC-B.
3. **Fencing verificado:** Mecanismo automático para aislar PC-A antes de promover PC-B.
4. **IP Virtual (VIP) con Keepalived:** Una IP única compartida que conmute entre balanceadores sin requerir cambio de URL en el navegador.

---

## 9. Modelos de Archivos Compose para Pruebas en Dos PC

### PC-A (`docker-compose.two-pc-pca.yml` - concepto)
```yaml
name: nexustix-two-pc-a
services:
  usuarios_db:
    image: postgres:15-alpine
    ports: ["192.168.1.10:5432:5432"]
    # ... volúmenes y credenciales
  eventos_db:
    image: postgres:15-alpine
    ports: ["192.168.1.10:5433:5432"]
  compras_db:
    image: postgres:15-alpine
    ports: ["192.168.1.10:5434:5432"]
  usuarios:
    build: { context: ., dockerfile: usuarios/Dockerfile }
    ports: ["192.168.1.10:8001:8001"]
  eventos:
    build: { context: ., dockerfile: eventos/Dockerfile }
    ports: ["192.168.1.10:8002:8002"]
  compras1:
    build: { context: ., dockerfile: compras/Dockerfile }
    ports: ["192.168.1.10:8003:8003"]
    environment:
      DATABASE_URL: ${DATABASE_URL:?Configurar URL privada fuera de Git}
      EVENTOS_URL: http://127.0.0.1:8002
      INSTANCE_ID: "1"
  compras_lb:
    image: nginx:alpine
    ports: ["192.168.1.10:8005:80"]
  frontend:
    image: nginx:alpine
    ports: ["192.168.1.10:8080:80"]
```

### PC-B (`docker-compose.two-pc-pcb.yml` - concepto)
```yaml
name: nexustix-two-pc-b
services:
  compras2:
    build: { context: ., dockerfile: compras/Dockerfile }
    ports: ["192.168.1.20:8003:8003"]
    environment:
      DATABASE_URL: ${DATABASE_URL:?Configurar URL privada fuera de Git}
      EVENTOS_URL: http://192.168.1.10:8002
      INSTANCE_ID: "2"
  compras_lb:
    image: nginx:alpine
    ports: ["192.168.1.20:8005:80"]
  frontend:
    image: nginx:alpine
    ports: ["192.168.1.20:8080:80"]
```
