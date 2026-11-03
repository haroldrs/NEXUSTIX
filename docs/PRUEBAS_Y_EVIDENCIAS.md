# Ampliación LAN

`tests/lan/` añade pruebas unitarias de configuración obligatoria, secretos,
roles, guardas y simulación de continuidad, sin Docker real ni red. Se incluye
en el runner unitario y en `pytest.ini`. El ensayo real `lan.py failover`
es independiente y exige `--apply` sobre el despliegue nuevo. No se ha ejecutado
en PC físicas. Resultados: [PUBLICACION_Y_LAN.md](PUBLICACION_Y_LAN.md).

---

# Pruebas seguras y evidencias

`pytest.ini` selecciona pruebas locales explícitas. El ejecutor ahora usa solo
`unit` por defecto e incluye `tests/vm`: son comprobaciones de preparación con
dobles, no pruebas sobre VMs. Se conservaron todos los archivos y aserciones de
pruebas existentes. `conftest.py` aborta antes de ejecutar fixtures si se recogen
suites de infraestructura sin `--allow-infrastructure-tests`.

| Capa/ruta | Efectos | Forma de habilitar |
|---|---|---|
| Los nueve `tests/test_*.py` enumerados en `pytest.ini` | SQLite temporal/en memoria, archivos temporales, dobles HTTP/Docker, lectura de fuentes | Por defecto |
| `tests/vm/` | Secretos sintéticos y paquetes en temporales, OpenSSL y shell con comandos simulados | Por defecto; no Docker/red/VM |
| `tests/staging/frontend_auth_state.test.js` | DOM/HTTP simulados en Node; lee frontend | `node --test` o capa unit del ejecutor |
| `tests/postgres/` | Crea/elimina bases desechables; según binarios, procesos PG, promoción/PITR | Opt-in infraestructura + `NEXUSTIX_TEST_PG_ADMIN_URL`; revisión previa obligatoria |
| `tests/resiliencia/`, suites Python de `tests/staging/` | HTTP con escrituras, detenciones/reinicios y recuperación en staging | Opt-in infraestructura + `NEXUSTIX_STAGING_HTTP=1`; **prohibido sobre el staging protegido actual** |
| `tests/test_backup_restore_isolated.py` | Crea/detiene/elimina recursos Docker temporales | Opt-in infraestructura + `NEXUSTIX_BACKUP_DOCKER_TEST=1`; no ejecutada aquí |
| `locustfile.py`, `simular_trafico.py`, verificadores HTTP | Carga, usuarios/compras y posible inyección de fallos | Revisión y autorización del destino; no ejecutar aquí |

## Comandos seguros

Desde la raíz, en el entorno con `requirements-test.txt` instalado:

```bash
python -m pytest
python scripts/ejecutar_pruebas.py --capas unit
node --test tests/staging/frontend_auth_state.test.js
python scripts/revisar_publicacion.py
```

El ejecutor crea `resultados/<UTC>/`, sin sobrescribir corridas. Para guardar
fuera del árbol, `--salida` acepta un directorio elegido por el operador.
No iniciar las APIs para comprobar imports: su arranque puede migrar bases.
Las pruebas importan servicios con URLs SQLite y dependencias simuladas.

## Suites con efectos, pendientes de autorización operativa

El ejecutor exige `--permitir-infraestructura` además de seleccionar una capa
no unitaria. `scripts/probar_local.py` también requiere esa opción y propaga
`--allow-infrastructure-tests` a pytest. Los comandos anteriores documentados
en informes históricos son evidencia histórica, no instrucciones seguras actuales.
Para invocar pytest directamente sobre infraestructura hace falta la opción
`--allow-infrastructure-tests` y las variables opt-in originales del destino.

No habilitar todo globalmente en el perfil de shell. Un flag no hace seguro un
staging compartido: sus suites históricas aún apuntan al proyecto protegido.
La colección explícita sin autorización falla, en lugar de fingir una aprobación.
No se ejecutaron estas suites durante esta tarea.

## Estados y evidencia

La línea base [RESULTADOS_DISTRIBUIDO.md](RESULTADOS_DISTRIBUIDO.md) conserva los
105 casos Python + 1 Node, 24 PostgreSQL y escenarios HTTP reales del 03/11/2026.
[Preparación VM](../despliegue-vm/RESULTADOS.md) conserva sus 25 casos (incluye 3
compartidos con configuración). No sumar repeticiones como casos nuevos.
Resultados de esta organización: [auditoría](AUDITORIA_REPOSITORIO.md).

- **APROBADA**: comando ejecutado y criterio comprobado; indicar entorno y alcance.
- **FALLIDA**: ejecución que no cumplió; conservar salida sin ocultarla.
- **BLOQUEADA**: no pudo ejecutarse por dependencia/permiso; no equivale a aprobada.
- **PENDIENTE**: no ejecutada; en especial tres PC, fallos LAN y fencing físico.

Guardar comando, UTC, versión/commit, destino, salida, XML y hashes. Revisar y
redactar datos/credenciales antes de compartir. `resultados/` y el contenido de
`docs/evidencias/` quedan fuera de Git; se conservan localmente. Los enlaces de
informes anteriores a esos artefactos solo funcionan en el equipo que los posee.
Los resúmenes Markdown históricos sí permanecen; hashes no sustituyen artefactos.
Entregar evidencias por canal privado tras revisión si un compañero necesita
reproducir el análisis. No versionar capturas de sesiones, dumps ni logs en bruto.

Referencia de configuración: [pytest.ini/testpaths](https://docs.pytest.org/en/stable/reference/customize.html).
