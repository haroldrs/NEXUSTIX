# PostgreSQL 15 de NEXUSTIX: imagen oficial + envoltorio de arranque que
#  - se niega a arrancar un nodo aislado (fencing) tras una promoción, para
#    impedir dos primarios a la vez;
#  - clona un nodo en espera (standby) con pg_basebackup cuando su volumen
#    está vacío y NEXUSTIX_PG_UPSTREAM_HOST indica el primario.
# Sin esas variables se comporta exactamente como postgres:15-alpine.
FROM postgres:15-alpine

COPY postgres/nexustix-entrypoint.sh /usr/local/bin/nexustix-entrypoint.sh
COPY postgres/nexustix-healthcheck.sh /usr/local/bin/nexustix-healthcheck.sh
COPY postgres/archive-wal.sh /usr/local/bin/archive-wal.sh
COPY postgres/init-replication.sh /docker-entrypoint-initdb.d/10-replication.sh
RUN chmod 0755 /usr/local/bin/archive-wal.sh /docker-entrypoint-initdb.d/10-replication.sh
RUN chmod 0755 /usr/local/bin/nexustix-entrypoint.sh /usr/local/bin/nexustix-healthcheck.sh

ENTRYPOINT ["nexustix-entrypoint.sh"]
CMD ["postgres"]

RUN mkdir -p /var/lib/postgresql/archive && chown postgres:postgres /var/lib/postgresql/archive
