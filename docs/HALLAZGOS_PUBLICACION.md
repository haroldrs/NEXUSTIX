# Revisión de publicación — resolución de las 81 coincidencias

La revisión estática identificó **0 secretos de instalación reales**, **11
coincidencias de contraseñas predeterminadas**, **29 datos sintéticos** y **41
falsos positivos** dentro de los 81 hallazgos originales. No se consultaron ni
modificaron las carpetas privadas de secretos ni las credenciales de bases vivas.
La ausencia de secretos identificados no es una garantía universal de seguridad.

Las 11 coincidencias de riesgo se retiraron de código, manifiestos y ejemplos:
las API requieren configuración externa; los Compose legacy requieren variables
obligatorias; el modo de compatibilidad exige un archivo externo explícito.
Además, una regla nueva detectó tres `POSTGRES_PASSWORD` sin comillas en legacy:
también se sustituyeron por variables obligatorias. No se vuelven a imprimir valores.

## Disposición individual de la instantánea original

Las líneas de esta tabla son las del informe original, no necesariamente las
actuales. Para ubicaciones actuales, ejecutar el escáner. REDACTADO se mantiene
incluso para datos sintéticos. No se descartó ninguna coincidencia por pertenecer
a una carpeta: la revisión se vincula al texto exacto, ruta y tipo mediante hash.

| Ruta original | Línea original | Tipo | Clasificación | Valor | Resolución |
|---|---:|---|---|---|---|
| `compras/database.py` | 6 | URL con credencial | contraseña predeterminada | REDACTADO | RETIRADA: configuración externa obligatoria; ningún cambio en credenciales existentes. |
| `config/pruebas.env.example` | 7 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `config/pruebas.env.example` | 14 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `despliegue-vm/postgres/entrypoint.sh` | 15 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `despliegue-vm/postgres/entrypoint.sh` | 27 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `despliegue-vm/postgres/entrypoint.sh` | 30 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `despliegue-vm/scripts/preparar.py` | 49 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/EVOLUCION_DISTRIBUIDA.md` | 169 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/PRUEBAS_RESILIENCIA.md` | 51 | URL con credencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/historico/ARQUITECTURA_DOS_PC_2026-10-24.md` | 67 | URL con credencial | contraseña predeterminada | REDACTADO | RETIRADA: configuración externa obligatoria; ningún cambio en credenciales existentes. |
| `docs/historico/ARQUITECTURA_DOS_PC_2026-10-24.md` | 193 | URL con credencial | contraseña predeterminada | REDACTADO | RETIRADA: configuración externa obligatoria; ningún cambio en credenciales existentes. |
| `docs/historico/ARQUITECTURA_DOS_PC_2026-10-24.md` | 212 | URL con credencial | contraseña predeterminada | REDACTADO | RETIRADA: configuración externa obligatoria; ningún cambio en credenciales existentes. |
| `docs/historico/AUDITORIA_TOLERANCIA_FALLOS_NEXUSTIX.md` | 354 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/historico/AUDITORIA_TOLERANCIA_FALLOS_NEXUSTIX.md` | 435 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/historico/AUDITORIA_TOLERANCIA_FALLOS_NEXUSTIX.md` | 437 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/historico/AUDITORIA_TOLERANCIA_FALLOS_NEXUSTIX.md` | 579 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/historico/AUDITORIA_TOLERANCIA_FALLOS_NEXUSTIX.md` | 638 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/historico/AUDITORIA_TOLERANCIA_FALLOS_NEXUSTIX.md` | 706 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/historico/DEMO_DISTRIBUIDA_NEXUSTIX.md` | 13 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/historico/DEMO_DISTRIBUIDA_NEXUSTIX.md` | 53 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/historico/GUIA_DEMOSTRACION_2026-10-25.md` | 23 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/historico/GUIA_DEMOSTRACION_2026-10-25.md` | 327 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/historico/GUIA_DEMOSTRACION_2026-10-25.md` | 360 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/historico/PLAN_FINALIZACION_MANUAL.md` | 81 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/historico/PLAN_PRUEBAS_FALLOS.md` | 31 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/historico/PLAN_PRUEBAS_FALLOS.md` | 76 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `docs/historico/STAGING_AUTENTICACION_HTTP.md` | 61 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `docs/historico/STAGING_AUTENTICACION_HTTP.md` | 78 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `eventos/database.py` | 6 | URL con credencial | contraseña predeterminada | REDACTADO | RETIRADA: configuración externa obligatoria; ningún cambio en credenciales existentes. |
| `frontend/app.js` | 143 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `legacy/multi-laptop/docker-compose.compras.yml` | 32 | URL con credencial | contraseña predeterminada | REDACTADO | RETIRADA: configuración externa obligatoria; ningún cambio en credenciales existentes. |
| `legacy/multi-laptop/docker-compose.compras.yml` | 48 | URL con credencial | contraseña predeterminada | REDACTADO | RETIRADA: configuración externa obligatoria; ningún cambio en credenciales existentes. |
| `legacy/multi-laptop/docker-compose.eventos.yml` | 30 | URL con credencial | contraseña predeterminada | REDACTADO | RETIRADA: configuración externa obligatoria; ningún cambio en credenciales existentes. |
| `legacy/multi-laptop/docker-compose.usuarios.yml` | 32 | URL con credencial | contraseña predeterminada | REDACTADO | RETIRADA: configuración externa obligatoria; ningún cambio en credenciales existentes. |
| `postgres/init-replication.sh` | 6 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `postgres/nexustix-entrypoint.sh` | 21 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `postgres/nexustix-entrypoint.sh` | 71 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `scripts/diagnostico_compras2_crash.sh` | 16 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `scripts/ejecutar_pruebas.py` | 75 | URL con credencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `scripts/entornos.py` | 90 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `scripts/entornos.py` | 91 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `scripts/entornos.py` | 102 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `scripts/entornos.py` | 103 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `scripts/generar_secretos.py` | 63 | URL con credencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `scripts/generar_secretos.py` | 66 | URL con credencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `scripts/generar_secretos.py` | 109 | credencial literal potencial | contraseña predeterminada | REDACTADO | RETIRADA: configuración externa obligatoria; ningún cambio en credenciales existentes. |
| `scripts/generar_topologia.py` | 8 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `scripts/generar_topologia.py` | 57 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `scripts/probar_local.py` | 35 | URL con credencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `tests/postgres/conftest.py` | 3 | URL con credencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `tests/resiliencia/test_05_dependencias.py` | 32 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/resiliencia/test_07_postgres.py` | 48 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/staging/frontend_auth_state.test.js` | 78 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/staging/frontend_auth_state.test.js` | 84 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/staging/frontend_auth_state.test.js` | 120 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/staging/frontend_auth_state.test.js` | 129 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/staging/frontend_auth_state.test.js` | 147 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/staging/frontend_auth_state.test.js` | 163 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/staging/test_auth_http.py` | 599 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/staging/test_auth_http.py` | 760 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |
| `tests/test_authentication.py` | 71 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_authentication.py` | 81 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_authentication.py` | 88 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_authentication.py` | 89 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_authentication.py` | 95 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_authentication.py` | 105 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_authentication.py` | 238 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_authentication.py` | 310 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_authentication.py` | 411 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_authentication.py` | 449 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_authentication.py` | 469 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_authentication.py` | 521 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_authentication.py` | 783 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_authentication.py` | 827 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_authentication.py` | 879 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_configuracion.py` | 113 | URL con credencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_configuracion.py` | 227 | URL con credencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_configuracion.py` | 230 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `tests/test_locust_safety.py` | 11 | credencial literal potencial | dato sintético | REDACTADO | CONSERVADO: fixture/ejemplo de prueba, no credencial de instalación LAN. |
| `usuarios/database.py` | 6 | URL con credencial | contraseña predeterminada | REDACTADO | RETIRADA: configuración externa obligatoria; ningún cambio en credenciales existentes. |
| `usuarios/main.py` | 148 | credencial literal potencial | falso positivo | REDACTADO | REVISADO: ruta, variable, plantilla, parámetro SQL o metadato; no un valor secreto incrustado. |

## Escaneo posterior

El escáner mantiene las coincidencias visibles. Solo acepta como revisados los
falsos positivos y datos sintéticos cuyas huellas figuran en
`config/publicacion-revisiones.json`, con motivo explícito. Una modificación
de texto/ruta/tipo vuelve a exigir revisión. Claves privadas, JWT completos y
tokens de proveedor nunca se aprueban mediante ese registro. No existen
excepciones globales por directorio ni exclusión de pruebas del escaneo.

`python3 scripts/revisar_publicacion.py` revisa candidatos del árbol;
`python3 scripts/revisar_publicacion.py --index` revisa el contenido exacto que
Git tiene preparado, incluido el propio registro de revisión. Código 0: sin
pendientes detectados; 1: pendientes; 2: archivos ilegibles/binarios. No analiza
entropía ni valida credenciales contra proveedores externos.

El escaneo final, sus contadores y límites están en
[PUBLICACION_Y_LAN.md](PUBLICACION_Y_LAN.md). Los datos sintéticos conservados
son fixtures de pruebas locales u opt-in, no secretos ni contraseñas para LAN.
Los tres PostgreSQL nuevos usan claves aleatorias por dominio y rol.

No quedan riesgos de credenciales incrustadas identificados sin resolver en
esta revisión. Esto permite preparar el commit local; la publicación remota
sigue reservada al propietario. TLS, filtrado LAN y aceptación física son
requisitos pendientes de operación, detallados en la guía de despliegue.
