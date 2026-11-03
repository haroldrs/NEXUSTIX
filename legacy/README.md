# Material histórico (no usar contra la versión actual)

Scripts y archivos anteriores a la autenticación JWT e `Idempotency-Key`. Se
conservan como referencia académica, pero **no funcionan** contra las APIs
actuales (envían compras sin token ni clave idempotente) y algunos son
peligrosos:

| Archivo | Motivo por el que se retiró | Reemplazo |
|---|---|---|
| `demo_sistemas_distribuidos.py` | Compras sin autenticación (`401`). | `docs/GUIA_DEMOSTRACION.md`, `simular_trafico.py` |
| `prueba_punto1_consistencia.py`, `prueba_punto3_tolerancia.py`, `prueba_punto5_observabilidad.py` | Pre-autenticación y puertos del entorno principal. | `tests/resiliencia/`, `scripts/verificar_flujo.py` |
| `reset_entorno_pruebas.sh` | Operaba sobre el entorno **principal** sin seleccionar proyecto y dejaba claves idempotentes y journal huérfanos. | `python3 scripts/nx.py pruebas reset-datos --confirmar nexustix-auth-staging` |
| `multi-laptop/` | Compose pre-autenticación con contraseñas fijas. | `docs/ARQUITECTURA_DOS_PC.md` |
| `diagnostico_compras2_*.log` | Evidencia de la computadora original. | — |

No los ejecute. Están excluidos de las comprobaciones automáticas.
