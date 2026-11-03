"""R22-R23 · Verificación final de consistencia y de no alteración del entorno de demostración."""
import json

import nx_pruebas as nx


def test_r22_verificacion_final_de_stock_compras_reservas_y_registros(admin):
    """Objetivo: tras toda la suite, las dos bases cuadran.
    Esperado: auditoría todo_consistente=True, diferencia global 0, sin reservas pendientes,
    sin compras sin reserva confirmada ni operaciones confirmadas sin compra; ninguna clave
    'processing' de las pruebas QA; stock nunca negativo; el journal registró las compras."""
    report = nx.audit(admin)
    assert report["reservas_pendientes"] == [], report["reservas_pendientes"]
    assert report["compras_sin_reserva_confirmada"] == [] and report["operaciones_confirmadas_sin_compra"] == []
    assert report["balance_global_diferencia"] == 0
    inconsistent = [e for e in report["reporte_por_evento"] if not e["es_consistente"]]
    assert inconsistent == [], inconsistent
    assert report["todo_consistente"] is True
    assert nx.sql("eventos_db", "SELECT count(*) FROM eventos WHERE stock < 0") == "0"
    stuck = nx.sql("compras_db", "SELECT count(*) FROM purchase_idempotency WHERE state = 'processing' "
                                 "AND (idempotency_key LIKE 'r0%' OR idempotency_key LIKE 'r1%' OR idempotency_key LIKE 'r2%')")
    assert stuck == "0", "toda operación de la suite quedó resuelta"


def test_r23_el_entorno_de_demostracion_no_fue_alterado(demo_before):
    """Objetivo: demostrar que la suite no tocó el entorno principal.
    Esperado: la huella de solo lectura de demo (filas por tabla, revisión, contenedores y fecha
    de creación de volúmenes) es idéntica antes y después de la suite."""
    after = nx.demo_fingerprint()
    assert json.dumps(after, sort_keys=True) == json.dumps(demo_before, sort_keys=True)
