# Imagen exclusiva de VMs; no cambia imágenes del laboratorio ni staging.
FROM postgres:15-alpine
COPY postgres/nexustix-healthcheck.sh /usr/local/bin/nexustix-healthcheck.sh
COPY postgres/archive-wal.sh /usr/local/bin/archive-wal.sh
COPY postgres/init-replication.sh /docker-entrypoint-initdb.d/10-replication.sh
COPY despliegue-vm/postgres/init-access.sh /docker-entrypoint-initdb.d/20-vm-access.sh
COPY despliegue-vm/postgres/entrypoint.sh /usr/local/bin/nexustix-vm-entrypoint.sh
RUN chmod 0755 /usr/local/bin/*.sh /docker-entrypoint-initdb.d/*.sh \
    && mkdir -p /var/lib/postgresql/archive \
    && chown postgres:postgres /var/lib/postgresql/archive
ENTRYPOINT ["nexustix-vm-entrypoint.sh"]
CMD ["postgres"]
