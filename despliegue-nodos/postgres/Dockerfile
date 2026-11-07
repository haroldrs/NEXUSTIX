# Imagen separada: no cambia los despliegues local, staging, VM ni LAN de tres PC.
FROM postgres:15-alpine
COPY postgres/archive-wal.sh /usr/local/bin/archive-wal.sh
COPY postgres/nexustix-healthcheck.sh /usr/local/bin/nexustix-healthcheck.sh
COPY despliegue-nodos/postgres/entrypoint.sh /usr/local/bin/nexustix-nodes-entrypoint.sh
COPY despliegue-nodos/postgres/init-cluster.sh /docker-entrypoint-initdb.d/10-nexustix-cluster.sh
COPY despliegue-nodos/postgres/manage.sh /usr/local/bin/nexustix-pg-manage
COPY despliegue-nodos/postgres/configure-access.sh /usr/local/bin/nexustix-pg-access
RUN chmod 0755 /usr/local/bin/archive-wal.sh /usr/local/bin/nexustix-healthcheck.sh \
    /usr/local/bin/nexustix-nodes-entrypoint.sh /usr/local/bin/nexustix-pg-manage \
    /usr/local/bin/nexustix-pg-access /docker-entrypoint-initdb.d/10-nexustix-cluster.sh \
    && mkdir -p /var/lib/nexustix-pg /var/lib/postgresql/wal_archive \
    && chown postgres:postgres /var/lib/nexustix-pg /var/lib/postgresql/wal_archive
ENTRYPOINT ["nexustix-nodes-entrypoint.sh"]
CMD ["postgres"]
