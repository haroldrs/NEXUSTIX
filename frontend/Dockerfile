# Frontend estático servido por Nginx. Los archivos se copian en la imagen
# (no se montan desde el repositorio) para que la instalación sea reproducible
# y no dependa de etiquetas SELinux de la carpeta del proyecto.
FROM nginx:1.28-alpine

COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY frontend/index.html frontend/style.css frontend/admin.css frontend/app.js frontend/checkout.js frontend/catalog.js frontend/admin.js frontend/config.js frontend/acceso.js frontend/observatorio.js frontend/persistencia.js frontend/resiliencia.js frontend/laboratorio.js frontend/coordinacion.js frontend/seguridad.js frontend/academico.js /usr/share/nginx/html/
COPY frontend/vendor/jsQR.js /usr/share/nginx/html/vendor/jsQR.js

EXPOSE 80
