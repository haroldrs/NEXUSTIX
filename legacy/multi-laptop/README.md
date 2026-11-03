# Configuraciones multi-laptop heredadas (NO desplegar con la app actual)

> **Obsoletas y pre-autenticación.** Estos Compose contienen credenciales de
> desarrollo, no montan las claves JWT/credenciales internas requeridas, no
> exponen la configuración de auth actual y distribuyen DB separadas. No usar
> para una demostración autenticada ni ejecutar contra datos reales. El diseño
> aprobado está en [`../docs/ARQUITECTURA_DOS_PC.md`](../docs/ARQUITECTURA_DOS_PC.md);
> requiere un Compose nuevo, backups, secretos por host y pruebas de
> consistencia antes del despliegue.

El contenido restante se conserva como referencia histórica únicamente.

---

# Notas históricas de despliegue en 3 laptops

Esto separa cada servicio en su propia máquina física, comunicándose por la
red WiFi/LAN en vez de por la red interna de Docker. Es la forma más honesta
de demostrar "sistema distribuido" — no son 3 contenedores en una laptop,
son 3 computadoras reales hablándose por IP.

## Requisito

Las 3 laptops deben estar **en la misma red** (mismo WiFi/router, o un
hotspot de celular compartido por las 3). Cada una necesita Docker instalado
(ver la guía de instalación en Fedora que ya tienen).

## Paso 1 — Averiguar la IP de cada laptop

En cada laptop, corre:

```bash
ip a | grep "inet " | grep -v 127.0.0.1
```

Anota la IP de la laptop 1 (usuarios) y la laptop 2 (eventos). Ejemplo:
`192.168.1.101` y `192.168.1.102`.

## Paso 2 — Abrir los puertos en el firewall de cada laptop (Fedora)

```bash
# Laptop 1 (usuarios)
sudo firewall-cmd --permanent --add-port=8001/tcp && sudo firewall-cmd --reload

# Laptop 2 (eventos)
sudo firewall-cmd --permanent --add-port=8002/tcp && sudo firewall-cmd --reload

# Laptop 3 (compras + balanceador + frontend)
sudo firewall-cmd --permanent --add-port={8003,8004,8005,8080}/tcp && sudo firewall-cmd --reload
```

## Paso 3 — Levantar cada servicio en su laptop

**Laptop 1:**
```bash
cd Project
docker compose -f multi-laptop/docker-compose.usuarios.yml up --build
```

**Laptop 2:**
```bash
cd Project
docker compose -f multi-laptop/docker-compose.eventos.yml up --build
```

**Laptop 3:** primero copia `.env.example` a `.env` y pon ahí las IPs
reales que anotaste en el Paso 1:

```bash
cd Project/multi-laptop
cp .env.example .env
nano .env   # reemplaza las IPs por las reales de laptop 1 y 2
cd ..
docker compose -f multi-laptop/docker-compose.compras.yml --env-file multi-laptop/.env up --build
```

## Paso 4 — Apuntar el frontend a las IPs reales

El navegador corre en tu laptop (probablemente la laptop 3), así que
`localhost` ya no sirve para llegar a usuarios/eventos que están en otras
máquinas. Edita `frontend/config.js` **antes de levantar la laptop 3**:

```js
const CONFIG = {
  USUARIOS_URL: "http://192.168.1.101:8001",   // IP real de laptop 1
  EVENTOS_URL: "http://192.168.1.102:8002",    // IP real de laptop 2
  COMPRAS_R1_URL: "http://localhost:8003",     // compras esta en la misma laptop 3
  COMPRAS_R2_URL: "http://localhost:8004",
  COMPRAS_LB_URL: "http://localhost:8005",
};
```

## Paso 5 — Probar

Abre `http://localhost:8080` en el navegador de la laptop 3. Si ves la
lista de eventos (que vive en la laptop 2) y puedes comprar (lo que valida
contra la laptop 1), el sistema distribuido de 3 máquinas está funcionando.

## Para la demo de tolerancia a fallos entre laptops

Apaga con `Ctrl+C` el contenedor de la laptop 1 (usuarios) mientras el
sistema sigue corriendo en las otras dos. Intenta comprar: la compra debe
fallar de forma controlada (HTTP 503 "Servicio de usuarios no disponible"),
no debe tumbar todo el sistema. Vuelve a levantar la laptop 1 y repite la
compra — debería funcionar de nuevo sin haber reiniciado nada más.
