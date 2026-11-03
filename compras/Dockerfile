# 1. Imagen oficial de Python ligera
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 2. Directorio de trabajo dentro del contenedor
WORKDIR /app

# 3. Copiar e instalar dependencias primero (aprovecha la cache de Docker)
COPY compras/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copiar el codigo fuente del servicio
COPY compras/ .
COPY shared/ ./shared/

# 5. Puerto interno del servicio de compras (mismo puerto para ambas replicas;
#    docker-compose las expone en el host como 8003 y 8004)
EXPOSE 8003

# 6. Migraciones protegidas (advisory lock: compras1 y compras2 pueden
#    arrancar a la vez contra la misma compras_db) y luego la API.
#    "exec" deja a Uvicorn como PID 1 para que reciba las señales de Docker.
CMD ["sh", "-c", "python -m shared.run_migrations && exec uvicorn main:app --host 0.0.0.0 --port 8003"]
