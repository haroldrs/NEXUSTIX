# Despliegue externo opcional (plantilla inactiva)

Nada de esta carpeta se aplica de forma automática ni altera los despliegues local o LAN.
Usa el mismo código y el `docker-compose.yml` existente en una VM o servidor propio.
No registra dominios, no genera certificados y no abre puertos.

1. Copia `externo.env.example` fuera del repositorio y completa los valores.
2. Conserva `NEXUSTIX_BIND_ADDRESS=127.0.0.1`: las APIs quedan en loopback y PostgreSQL no se publica.
3. Instala un proxy TLS a partir de `proxy-tls.example.cfg`, con certificados emitidos para tu dominio.
4. Adapta `frontend-config.example.js` como configuración pública (`frontend_config`). No admite secretos.
5. Publica únicamente 80 y 443. Genera los secretos con los scripts del proyecto, en el servidor.

El cifrado cubre navegador–proxy. El tráfico entre microservicios, PostgreSQL y WAL no queda
cifrado por este paso; en una sola máquina permanece dentro de su red Docker.

**Límites.** Un servidor único no tolera su propia pérdida. Varios servidores reparten carga y
réplicas, pero no dan failover PostgreSQL seguro. La alta disponibilidad operativa exige
coordinación, fencing, redundancia y pruebas: ver `despliegue-nodos/COORDINACION.md`. Estar en
la nube no la proporciona por sí mismo.
