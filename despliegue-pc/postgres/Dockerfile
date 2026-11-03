FROM postgres:15-alpine
COPY despliegue-pc/postgres/entrypoint.sh /usr/local/bin/nexustix-lan-entrypoint
COPY despliegue-pc/postgres/init-app.sh /docker-entrypoint-initdb.d/10-app.sh
RUN chmod 0755 /usr/local/bin/nexustix-lan-entrypoint /docker-entrypoint-initdb.d/10-app.sh
ENTRYPOINT ["nexustix-lan-entrypoint"]
CMD ["postgres"]
