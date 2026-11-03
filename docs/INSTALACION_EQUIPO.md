# Instalación para el equipo

Guía para una computadora nueva. Ningún comando de instalación o despliegue de
esta guía se ejecutó durante la organización del repositorio. Sobre el Fedora
actual solo se permiten las comprobaciones de lectura y pruebas locales indicadas.

## Antes de clonar

El propietario debe revisar [AUDITORIA_REPOSITORIO.md](AUDITORIA_REPOSITORIO.md),
resolver los hallazgos y crear/publicar por su cuenta el repositorio privado.
Todavía no se conoce su URL. Conceder acceso a los compañeros mediante sus
cuentas; no compartir claves SSH privadas ni incluir tokens en URLs.

En cada PC: Git, Python con `venv`, Node para la prueba aislada del frontend y,
solo para ejecutar servicios, Docker Engine con Compose v2 (el proyecto exige
al menos 2.20) y OpenSSL. Las imágenes API usan Python 3.11 y PostgreSQL 15;
`requirements-test.txt` mantiene dependencias de pruebas distintas de producción.
Consultar [instalación Fedora](INSTALACION_FEDORA.md) y la
[documentación oficial de Docker](https://docs.docker.com/engine/install/fedora/)
antes de instalar paquetes. Los permisos de Docker equivalen a control del host.
No se requieren VirtualBox ni VMs para revisar este código.

## Clonar y verificar, sin desplegar

Estos comandos piden valores reales; no hay URL ni ruta de destino asumida:

```bash
read -r -p 'URL SSH del repositorio privado ya revisado: ' REPO_URL
read -r -p 'Directorio nuevo para el clon: ' CLONE_DIR
git clone -- "$REPO_URL" "$CLONE_DIR"
cd -- "$CLONE_DIR"
git status --short
git rev-parse HEAD
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-test.txt
python -m pytest
node --test tests/staging/frontend_auth_state.test.js
python scripts/revisar_publicacion.py
```

Comparar el commit entre las tres PC. Node aquí usa un DOM y HTTP simulados;
no llama al staging. El escáner detecta también ejemplos y credenciales débiles
heredadas: revisar por ruta/línea, sin publicar valores. No copiar `.venv`,
`resultados`, secretos, discos VM ni volúmenes desde la PC de origen.

## Ejecución local opcional, solo en PC nueva

Esta secuencia usa el entorno demo convencional, no implementa la distribución
física ni habilita por sí sola la interfaz nueva de pedidos del laboratorio.
Antes, confirmar con `docker ps -a` y `docker volume ls` que no existen proyectos
ni volúmenes previos con los nombres del Compose demo. Si existen, detener el
procedimiento: no recrearlos, no completar secretos y no inicializar bases.

```bash
python3 scripts/nx.py demo preflight
python3 scripts/nx.py demo secretos
python3 scripts/nx.py demo up
read -r -p 'Correo real del administrador local: ' ADMIN_EMAIL
python3 scripts/nx.py demo admin --email "$ADMIN_EMAIL"
python3 scripts/nx.py demo status
```

`secretos` crea una carpeta externa nueva; en Fedora puede etiquetarla para
SELinux. `up` construye imágenes, crea recursos y aplica migraciones; `admin`
escribe en la base. Son pasos operativos futuros, no verificaciones inocuas.
La contraseña se consulta en privado en el archivo generado `admin-password`;
no se incluye aquí. Abrir `http://localhost:8080`. El frontend demo conserva su
flujo por defecto; el flujo de pedidos/pagos/QR del laboratorio está documentado
en [EVOLUCION_DISTRIBUIDA.md](EVOLUCION_DISTRIBUIDA.md).

Para la evolución completa en otro host, revisar primero las instrucciones del
laboratorio y sus rutas de secretos; no ejecutar el generador ni `up` contra el
laboratorio existente de Fedora. Los primarios y standbys locales comparten host:
no acreditan tolerancia a caída de una PC.

## Preparación de las otras dos PC

Tras clonar y aprobar las pruebas locales, recoger RAM/CPU/disco libre,
SO/arquitectura, IP LAN reservada, puertos ocupados, permisos y disponibilidad
horaria. Completar la tabla de [PLAN_DESPLIEGUE_3_PC.md](PLAN_DESPLIEGUE_3_PC.md).
No ejecutar los Compose monolíticos en cada PC esperando que se conecten:
crearían tres conjuntos independientes de datos. Los manifiestos por rol ya están en `despliegue-pc/`. Continuar con su
[guía exacta](../despliegue-pc/README.md), validar sin arrancar y autorizar
por separado el despliegue de datos sintéticos en hosts nuevos.
