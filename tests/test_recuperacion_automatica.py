"""Recuperación automática de Compras y ganchos de prueba (SQLite aislado, dobles HTTP).

Reutiliza los fixtures de test_authentication: nunca toca bases del proyecto.
"""
import importlib

import pytest
from fastapi import Response

from test_authentication import (  # noqa: F401 - fixtures reutilizados
    StubEventsResponse,
    _seed_recovery_claim,
    prepare_recovery,
    purchases_app,
)


def _events_get(op_id, state, event_id=10, quantity=1):
    def get(url, **_kwargs):
        if url.endswith("/reservas/pendientes"):
            pending = [{"operacion_id": op_id, "evento_id": event_id, "cantidad": quantity,
                        "estado": "reservada"}] if state["reservation"] == "reservada" else []
            return StubEventsResponse(200, {"total": len(pending), "reservas": pending})
        return StubEventsResponse(200, {"operacion_id": op_id, "evento_id": event_id, "cantidad": quantity,
                                        "estado": state["reservation"], "stock_actual": 3})
    return get


def test_automatic_recovery_never_releases_stock_by_default(purchases_app, monkeypatch):
    _, _, sessions, _ = purchases_app
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    op_id = "auto-stale-reserved"
    _seed_recovery_claim(sessions, models, key="auto-stale-key", op_id=op_id)
    prepare_recovery(monkeypatch, main)
    state = {"reservation": "reservada"}
    posts = []
    monkeypatch.setattr(main.requests, "get", _events_get(op_id, state))
    monkeypatch.setattr(main.requests, "post", lambda url, **_k: posts.append(url))

    with sessions() as db:
        result = main.ejecutar_recuperacion(db, permitir_compensacion=False, incluir_auditoria=False,
                                            origen="automatica_test")

    assert posts == [], "el modo automático no debe llamar a /liberar"
    assert result["contadores"]["compensaciones_requieren_admin"] == 1
    assert result["contadores"]["reservas_liberadas"] == 0
    assert result["auditoria_consistencia"] is None
    with sessions() as db:
        record = db.query(models.IdempotencyRequest).filter_by(operacion_id=op_id).one()
        assert record.state == "processing", "la clave sigue reanudable para el cliente o el administrador"


def test_admin_recovery_still_compensates_the_same_stale_claim(purchases_app, monkeypatch):
    _, _, sessions, _ = purchases_app
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    op_id = "auto-then-admin"
    _seed_recovery_claim(sessions, models, key="auto-then-admin-key", op_id=op_id)
    principal = prepare_recovery(monkeypatch, main)
    state = {"reservation": "reservada"}

    def post(url, **_kwargs):
        assert url.endswith("/liberar")
        state["reservation"] = "liberada"
        return StubEventsResponse(200, {"evento_id": 10, "cantidad": 1, "operacion_id": op_id,
                                        "estado_reserva": "liberada", "stock_restante": 4,
                                        "idempotente": False, "reloj_logico_T": 1})

    monkeypatch.setattr(main.requests, "get", _events_get(op_id, state))
    monkeypatch.setattr(main.requests, "post", post)
    with sessions() as db:
        automatic = main.ejecutar_recuperacion(db, permitir_compensacion=False, incluir_auditoria=False, origen="auto")
    with sessions() as db:
        admin = main.recuperar_servidor(db, principal)
    assert automatic["contadores"]["reservas_liberadas"] == 0
    assert admin["contadores"]["reservas_liberadas"] == 1
    with sessions() as db:
        record = db.query(models.IdempotencyRequest).filter_by(operacion_id=op_id).one()
        assert record.state == "completed" and record.response_status == 409
        assert db.query(models.Compra).filter_by(operacion_id=op_id).count() == 0


def test_automatic_recovery_confirms_persisted_purchase_once(purchases_app, monkeypatch):
    _, _, sessions, _ = purchases_app
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    op_id = "auto-confirm-pending"
    _seed_recovery_claim(sessions, models, key="auto-confirm-key", op_id=op_id, state="completed",
                         body={"id": 77, "operacion_id": op_id, "confirmacion_reserva": "pendiente"},
                         purchase={"id": 77, "evento_id": 10, "cantidad": 1})
    prepare_recovery(monkeypatch, main)
    state = {"reservation": "reservada", "confirm_calls": 0}

    def post(url, **_kwargs):
        assert url.endswith("/confirmar")
        state["confirm_calls"] += 1
        state["reservation"] = "confirmada"
        return StubEventsResponse(200, {"status": "ok", "evento_id": 10, "cantidad": 1, "operacion_id": op_id,
                                        "estado_reserva": "confirmada", "idempotente": False, "reloj_logico_T": 2})

    monkeypatch.setattr(main.requests, "get", _events_get(op_id, state))
    monkeypatch.setattr(main.requests, "post", post)
    for _ in range(2):  # repetir la recuperación no repite efectos
        with sessions() as db:
            main.ejecutar_recuperacion(db, permitir_compensacion=False, incluir_auditoria=False, origen="auto")
    assert state["confirm_calls"] == 1
    with sessions() as db:
        assert db.query(models.Compra).filter_by(operacion_id=op_id).count() == 1
        record = db.query(models.IdempotencyRequest).filter_by(operacion_id=op_id).one()
        assert record.response_body["confirmacion_reserva"] == "confirmada"


def test_automatic_sweep_is_skipped_on_simulated_shutdown(purchases_app, monkeypatch):
    main = importlib.import_module("main")
    monkeypatch.setattr(main, "SIMULACION_APAGADO", True)
    assert main.ejecutar_barrido_automatico()["motivo"] == "nodo_apagado_simulado"


def test_health_reports_automatic_recovery_state(purchases_app):
    _, _, sessions, _ = purchases_app
    main = importlib.import_module("main")
    with sessions() as db:
        body = main.health(Response(), db)
    assert body["status"] == "ok"
    assert body["recuperacion_automatica"]["habilitada"] is False
    assert body["recuperacion_automatica"]["compensacion_automatica"] is False


def test_crash_and_hold_hooks_are_inert_unless_enabled(purchases_app, monkeypatch):
    main = importlib.import_module("main")
    exits, sleeps = [], []
    monkeypatch.setattr(main.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(main.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(main, "TEST_CRASH_HOOKS_ENABLED", False)
    main._test_crash_at("after_reservation", "after_reservation", "op", "after_reservation")
    assert exits == [] and sleeps == []

    monkeypatch.setattr(main, "TEST_CRASH_HOOKS_ENABLED", True)
    main._test_crash_at(None, "after_reservation", "op", "after_reservation")
    assert sleeps == [main.TEST_HOLD_SECONDS] and exits == []
    main._test_crash_at("after_claim", "after_reservation", "op", None)
    assert exits == [], "un checkpoint distinto no detiene el proceso"
    main._test_crash_at("after_reservation", "after_reservation", "op", None)
    assert exits == [86]


def test_lamport_clock_is_monotonic_under_threads(purchases_app):
    from concurrent.futures import ThreadPoolExecutor

    main = importlib.import_module("main")
    start = main.RELOJ_LAMPORT
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: main.tic_lamport(), range(2000)))
    assert len(set(values)) == 2000, "cada tic debe producir un valor único"
    assert main.RELOJ_LAMPORT == start + 2000


@pytest.mark.parametrize("mode", ["processing", "completed"])
def test_recovery_result_always_exposes_counters(purchases_app, monkeypatch, mode):
    _, _, sessions, _ = purchases_app
    main = importlib.import_module("main")
    prepare_recovery(monkeypatch, main)

    def unreachable(*_args, **_kwargs):
        raise main.requests.exceptions.ConnectionError("eventos caído")

    monkeypatch.setattr(main.requests, "get", unreachable)
    with sessions() as db:
        result = main.ejecutar_recuperacion(db, permitir_compensacion=mode == "completed",
                                            incluir_auditoria=False, origen="auto")
    assert result["status"] == "recuperacion_incompleta"
    assert result["reservas_consultadas"] is False
    assert "contadores" in result and result["contadores"]["reservas_liberadas"] == 0
