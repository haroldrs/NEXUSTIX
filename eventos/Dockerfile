# 1. Imagen oficial de Python ligera
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 2. Directorio de trabajo dentro del contenedor
WORKDIR /app

# 3. Copiar e instalar dependencias primero (aprovecha la cache de Docker)
COPY eventos/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copiar el codigo fuente del servicio
COPY eventos/ .
COPY shared/ ./shared/

# 5. Puerto interno del servicio de eventos (segun arquitectura)
EXPOSE 8002

# 6. Ejecutar el servicio (Eventos crea/actualiza su esquema de forma
#    idempotente al arrancar; no usa Alembic)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
