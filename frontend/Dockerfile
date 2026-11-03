# Frontend estático servido por Nginx. Los archivos se copian en la imagen
# (no se montan desde el repositorio) para que la instalación sea reproducible
# y no dependa de etiquetas SELinux de la carpeta del proyecto.
FROM nginx:1.28-alpine

COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY frontend/index.html frontend/style.css frontend/app.js frontend/checkout.js frontend/config.js /usr/share/nginx/html/

EXPOSE 80
