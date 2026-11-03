# Documentación histórica

Documentos de iteraciones anteriores (hasta 2026-10-25), conservados como
evidencia de las pruebas y decisiones previas. **No describen la versión
actual**: varios comandos ya no aplican (por ejemplo, migraciones manuales con
`alembic upgrade head`, nombres de contenedor fijos, `app.staging.js`,
`reset_entorno_pruebas.sh` o la política `on-failure:5`).

| Documento histórico | Sustituido por |
|---|---|
| `AUDITORIA_TOLERANCIA_FALLOS_NEXUSTIX.md`, `MATRIZ_TOLERANCIA_FALLOS.md`, `PLAN_PRUEBAS_FALLOS.md`, `PUNTO_1_*`, `PUNTO_3_*` | [`../PRUEBAS_RESILIENCIA.md`](../PRUEBAS_RESILIENCIA.md) |
| `RESPALDO_RESTAURACION.md`, `PLAN_BACKUP_RESTAURACION.md` | [`../BASES_DE_DATOS.md`](../BASES_DE_DATOS.md) |
| `AUTENTICACION_DESPLIEGUE.md`, `STAGING_AUTENTICACION_HTTP.md`, `PLAN_FINALIZACION_MANUAL.md` | [`../INSTALACION_FEDORA.md`](../INSTALACION_FEDORA.md), [`../ARQUITECTURA.md`](../ARQUITECTURA.md) |
| `GUIA_DEMOSTRACION_2026-10-25.md`, `DEMO_DISTRIBUIDA_NEXUSTIX.md` | [`../GUIA_DEMOSTRACION.md`](../GUIA_DEMOSTRACION.md) |
| `ARQUITECTURA_DOS_PC_2026-10-24.md` | [`../ARQUITECTURA_DOS_PC.md`](../ARQUITECTURA_DOS_PC.md) |

Los resultados registrados en estos documentos se obtuvieron en la
computadora original con el código de esa fecha; no se volvieron a ejecutar
en esta entrega y no deben citarse como resultados de la versión actual.
