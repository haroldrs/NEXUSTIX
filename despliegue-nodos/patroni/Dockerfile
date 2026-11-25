# PostgreSQL 15 administrado por Patroni: elección de la base principal por
# mayoría de votantes etcd y reincorporación automática con pg_rewind.
# Patroni se instala con pip en una versión fija dentro de un Python aislado
# (/opt/patroni) que luego se copia sobre la imagen oficial de PostgreSQL.
FROM python:3.11-slim-bookworm AS patroni
# Secreto opcional "pipca": certificado de un proxy corporativo, solo para la descarga.
RUN --mount=type=secret,id=pipca,required=false \
    if [ -f /run/secrets/pipca ]; then export PIP_CERT=/run/secrets/pipca; fi; \
    python -m venv --copies /opt/patroni \
    && /opt/patroni/bin/pip install --no-cache-dir "patroni[etcd3]==4.0.4" "psycopg2-binary==2.9.10"

FROM postgres:15-bookworm
COPY --from=patroni /usr/local /usr/local
COPY --from=patroni /opt/patroni /opt/patroni
RUN ldconfig && ln -s /opt/patroni/bin/patroni /usr/local/bin/patroni \
    && ln -s /opt/patroni/bin/patronictl /usr/local/bin/patronictl \
    && patroni --version
COPY despliegue-nodos/patroni/entrypoint.sh /usr/local/bin/nexustix-patroni
COPY despliegue-nodos/patroni/post-bootstrap.sh /usr/local/bin/nexustix-post-bootstrap
COPY despliegue-nodos/patroni/healthcheck.py /usr/local/bin/nexustix-patroni-health
RUN chmod 0755 /usr/local/bin/nexustix-patroni /usr/local/bin/nexustix-post-bootstrap /usr/local/bin/nexustix-patroni-health \
    && sed -i '1s|.*|#!/usr/local/bin/python3|' /usr/local/bin/nexustix-patroni-health
ENTRYPOINT ["/usr/local/bin/nexustix-patroni"]
