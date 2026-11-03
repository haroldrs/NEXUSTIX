# 1. Imagen oficial de Python ligera
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 2. Directorio de trabajo dentro del contenedor
WORKDIR /app

# 3. Copiar e instalar dependencias primero (aprovecha la cache de Docker)
COPY usuarios/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copiar el codigo fuente del servicio
COPY usuarios/ .
COPY shared/ ./shared/

# 5. Puerto interno del servicio de usuarios (segun arquitectura)
EXPOSE 8001

# 6. Migraciones protegidas (shared/run_migrations.py) y luego la API.
#    "exec" deja a Uvicorn como PID 1 para que reciba las señales de Docker.
CMD ["sh", "-c", "python -m shared.run_migrations && exec uvicorn main:app --host 0.0.0.0 --port 8001"]
