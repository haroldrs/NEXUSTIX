# Balanceador Nginx de Compras. Requiere Nginx >= 1.27.3 por el parámetro
# "resolve" en el bloque upstream (re-resolución DNS de las réplicas).
FROM nginx:1.28-alpine

COPY compras_lb/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
