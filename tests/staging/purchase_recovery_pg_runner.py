"""Controlled fault-injection helper, executed inside the staging compras1 container.

The helper receives a staging-only scenario description and an admin access
token on stdin. It never prints the token or complete HTTP response bodies.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any
from urllib.parse import urlparse

import requests
from fastapi.responses import JSONResponse


STAGE = "init"


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, default=str), flush=True)


def snapshot(main: Any, scenario: dict[str, Any]) -> dict[str, Any]:
    from models import Compra, IdempotencyRequest

    db = main.SessionLocal()
    try:
        claim = db.query(IdempotencyRequest).filter_by(
            idempotency_key=scenario["key"]
        ).one_or_none()
        purchases = db.query(Compra).filter_by(
            operacion_id=scenario["operacion_id"]
        ).all()
        event_response = requests.get(
            f"{main.EVENTOS_URL}/eventos/{scenario['event_id']}", timeout=8
        )
        event_body = event_response.json() if event_response.status_code == 200 else {}
        reservation_response = requests.get(
            f"{main.EVENTOS_URL}/eventos/reservas/{scenario['operacion_id']}",
            headers=main.eventos_headers(), timeout=8,
        )
        reservation_body = (
            reservation_response.json() if reservation_response.status_code == 200 else {}
        )
        return {
            "claim_state": claim.state if claim else None,
            "replay_status": claim.response_status if claim else None,
            "replay_confirmation": (
                (claim.response_body or {}).get("confirmacion_reserva")
                if claim and isinstance(claim.response_body, dict)
                else None
            ),
            "purchases": [
                {"id": row.id, "event_id": row.evento_id, "quantity": row.cantidad,
                 "state": row.estado}
                for row in purchases
            ],
            "event_http": event_response.status_code,
            "stock": event_body.get("stock"),
            "reservation_http": reservation_response.status_code,
            "reservation_state": reservation_body.get("estado"),
            "reservation_event_id": reservation_body.get("evento_id"),
            "reservation_quantity": reservation_body.get("cantidad"),
        }
    finally:
        db.close()


def call_recovery(main: Any, token: str, operation_ids: set[str]) -> dict[str, Any]:
    from models import RegistroJournal

    db = main.SessionLocal()
    try:
        before = db.query(RegistroJournal.id).order_by(RegistroJournal.id.desc()).first()
        before_id = before[0] if before else 0
    finally:
        db.close()

    response = requests.post(
        "http://127.0.0.1:8003/compras/recuperar-servidor",
        headers={"Authorization": f"Bearer {token}"}, timeout=20,
    )
    try:
        body = response.json()
    except ValueError:
        body = {}

    db = main.SessionLocal()
    try:
        new_entries = db.query(RegistroJournal).filter(
            RegistroJournal.id > before_id
        ).order_by(RegistroJournal.id.asc()).all()
        journal_ids = [row.id for row in new_entries if row.tipo == "RECOVERY"]
        journal_cleanable = (
            len(new_entries) == 1 and len(journal_ids) == 1
        )
    finally:
        db.close()

    related = {
        operation_id: [entry for entry in body.get("diagnosticos", [])
                       if entry.get("operacion_id") == operation_id]
        for operation_id in sorted(operation_ids)
    }
    return {
        "http": response.status_code,
        "status": body.get("status"),
        "counters": body.get("contadores", {}),
        "diagnostics_by_operation": related,
        "recovery_journal_ids": journal_ids,
        "journal_cleanup_unambiguous": journal_cleanable,
    }


def invoke_purchase(main: Any, scenario: dict[str, Any], fault: str) -> tuple[int, dict[str, Any], str]:
    from main import CompraRequest, Principal

    db = main.SessionLocal()
    principal = Principal(
        user_id=scenario["synthetic_user_id"], role="comprador",
        subject=f"nexustix-staging-recovery-test|{scenario['synthetic_user_id']}",
    )
    payload = CompraRequest(evento_id=scenario["event_id"], cantidad=1)
    original_post = main.requests.post
    original_get = main.requests.get

    class LostAck:
        status_code = 503

        @staticmethod
        def json() -> dict[str, str]:
            return {"detail": "injected acknowledgement loss"}

    def injected_post(url: str, *args: Any, **kwargs: Any) -> Any:
        path = urlparse(url).path
        if fault == "before_reserve" and path.endswith("/reservar"):
            raise requests.exceptions.ReadTimeout("controlled test fault")
        if fault == "after_reserve" and path.endswith("/reservar"):
            original_post(url, *args, **kwargs)
            raise requests.exceptions.ReadTimeout("controlled test fault")
        if fault == "after_confirm" and path.endswith("/confirmar"):
            original_post(url, *args, **kwargs)
            return LostAck()
        return original_post(url, *args, **kwargs)

    def injected_get(url: str, *args: Any, **kwargs: Any) -> Any:
        path = urlparse(url).path
        if fault in {"after_confirm", "after_reserve"} and path.startswith("/eventos/reservas/"):
            raise requests.exceptions.ReadTimeout("controlled durable-status lookup fault")
        return original_get(url, *args, **kwargs)

    main.requests.post = injected_post
    main.requests.get = injected_get
    try:
        result = main.crear_compra(payload, scenario["key"], db, principal)
        claim = db.query(__import__("models").IdempotencyRequest).filter_by(
            idempotency_key=scenario["key"]
        ).one_or_none()
        if claim is None:
            raise AssertionError("purchase idempotency claim was not persisted")
        operation_id = claim.operacion_id
    finally:
        main.requests.post = original_post
        main.requests.get = original_get
        db.close()

    if isinstance(result, JSONResponse):
        return result.status_code, json.loads(result.body.decode("utf-8")), operation_id
    return 200, result, operation_id


def main_run(spec: dict[str, Any]) -> dict[str, Any]:
    global STAGE
    import main as purchases_main

    from models import RegistroJournal

    token = spec.pop("admin_access_token")
    scenarios = {entry["name"]: entry for entry in spec["scenarios"]}
    for name, fault in (
        ("confirmed_purchase_pending_replay", "after_confirm"),
        ("old_claim_without_reservation", "before_reserve"),
        ("old_reservation_without_purchase", "after_reserve"),
    ):
        STAGE = f"create:{name}"
        status, body, operation_id = invoke_purchase(purchases_main, scenarios[name], fault)
        scenarios[name]["operacion_id"] = operation_id
        expected = 503 if fault in {"before_reserve", "after_reserve"} else 200
        if status != expected:
            raise AssertionError(f"synthetic purchase returned unexpected HTTP {status}")
        if fault == "after_confirm" and body.get("confirmacion_reserva") != "pendiente":
            raise AssertionError("lost confirmation acknowledgement plus unavailable status did not persist pending replay")

    STAGE = "capture_initial_state"
    before = {name: snapshot(purchases_main, row) for name, row in scenarios.items()}
    expected_before = {
        "confirmed_purchase_pending_replay": ("completed", 1, "confirmada", 1),
        "old_claim_without_reservation": ("processing", 0, None, 2),
        "old_reservation_without_purchase": ("processing", 0, "reservada", 1),
    }
    for name, values in expected_before.items():
        observed = before[name]
        if (observed["claim_state"], len(observed["purchases"]),
            observed["reservation_state"], observed["stock"]) != values:
            raise AssertionError(f"initial state mismatch for {name}")

    STAGE = "wait_for_stale_threshold"
    wait_seconds = purchases_main.RECOVERY_STALE_SECONDS + 2
    time.sleep(wait_seconds)

    operation_ids = {scenario["operacion_id"] for scenario in scenarios.values()}

    def assert_recovery_scope(expected: set[str], stage: str) -> None:
        global STAGE
        STAGE = stage
        pending_response = requests.get(
            f"{purchases_main.EVENTOS_URL}/eventos/reservas/pendientes",
            headers=purchases_main.eventos_headers(), timeout=8,
        )
        if pending_response.status_code != 200:
            raise AssertionError("cannot verify pending Events scope")
        event_candidates = {
            str(row.get("operacion_id")) for row in pending_response.json().get("reservas", [])
            if row.get("operacion_id")
        }
        db = purchases_main.SessionLocal()
        try:
            now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            processing_candidates = {
                row.operacion_id for row in db.query(__import__("models").IdempotencyRequest)
                .filter_by(state="processing").all()
                if purchases_main._claim_is_stale(row, now)
            }
            pending_replay_candidates = {
                row.operacion_id for row in db.query(__import__("models").IdempotencyRequest)
                .filter_by(state="completed").all()
                if isinstance(row.response_body, dict)
                and row.response_body.get("confirmacion_reserva") == "pendiente"
            }
        finally:
            db.close()
        candidates = event_candidates | processing_candidates | pending_replay_candidates
        if candidates != expected:
            raise AssertionError("recovery would include unrelated or missing candidates; no recovery request sent")

    assert_recovery_scope(operation_ids, "recovery_scope_first")
    first = call_recovery(purchases_main, token, operation_ids)
    if first["http"] != 200 or not first["journal_cleanup_unambiguous"]:
        raise AssertionError("first recovery could not be safely verified")
    assert first["counters"].get("replays_actualizados") == 1
    assert first["counters"].get("reservas_liberadas") == 1
    assert first["counters"].get("claims_sin_reserva") == 1
    a_op = scenarios["confirmed_purchase_pending_replay"]["operacion_id"]
    c_op = scenarios["old_reservation_without_purchase"]["operacion_id"]
    if first["diagnostics_by_operation"].get(a_op) or first["diagnostics_by_operation"].get(c_op):
        raise AssertionError("first recovery reported an unexplained diagnostic for a repairable operation")
    if not any(entry.get("tipo") == "claim_sin_reserva_reintentable"
               for entry in first["diagnostics_by_operation"].get(
                   scenarios["old_claim_without_reservation"]["operacion_id"], [])):
        raise AssertionError("first recovery did not diagnose the claim without a reservation")
    after_first = {name: snapshot(purchases_main, row) for name, row in scenarios.items()}

    no_reservation_op = scenarios["old_claim_without_reservation"]["operacion_id"]
    assert_recovery_scope({no_reservation_op}, "recovery_scope_second")
    second = call_recovery(purchases_main, token, operation_ids)
    if second["http"] != 200 or not second["journal_cleanup_unambiguous"]:
        raise AssertionError("second recovery could not be safely verified")
    if second["counters"].get("replays_actualizados") != 0 or second["counters"].get("reservas_liberadas") != 0:
        raise AssertionError("second recovery repeated a completed effect")
    if second["counters"].get("claims_sin_reserva") != 1:
        raise AssertionError("second recovery did not leave the no-reservation claim retryable")
    if not any(entry.get("tipo") == "claim_sin_reserva_reintentable"
               for entry in second["diagnostics_by_operation"].get(no_reservation_op, [])):
        raise AssertionError("second recovery did not report the retryable claim")

    after_second = {name: snapshot(purchases_main, row) for name, row in scenarios.items()}
    for name in scenarios:
        if after_second[name] != after_first[name]:
            raise AssertionError(f"second recovery changed state for {name}")
    a = after_second["confirmed_purchase_pending_replay"]
    if (a["claim_state"], len(a["purchases"]), a["replay_confirmation"],
        a["reservation_state"], a["stock"]) != ("completed", 1, "confirmada", "confirmada", 1):
        raise AssertionError("confirmed purchase did not reconcile to a final replay")
    if first["counters"].get("replays_actualizados") != 1 or second["counters"].get("replays_actualizados") != 0:
        raise AssertionError("replay repair was not performed exactly once")

    b = after_second["old_claim_without_reservation"]
    if (b["claim_state"], len(b["purchases"]), b["reservation_state"], b["stock"]) != (
        "processing", 0, None, 2
    ):
        raise AssertionError("claim without reservation was changed or fabricated a purchase")
    if not any(item.get("tipo") == "claim_sin_reserva_reintentable"
               for item in first["diagnostics_by_operation"].get(
                   scenarios["old_claim_without_reservation"]["operacion_id"], [])):
        raise AssertionError("claim without reservation was not diagnosed as retryable")

    STAGE = "retry_claim_without_reservation"
    retry_status, retry_body, retry_operation_id = invoke_purchase(
        purchases_main, scenarios["old_claim_without_reservation"], "none"
    )
    b_after_retry = snapshot(purchases_main, scenarios["old_claim_without_reservation"])
    if retry_status != 200 or len(b_after_retry["purchases"]) != 1:
        raise AssertionError("same-key retry did not complete the operation exactly once")
    if retry_operation_id != scenarios["old_claim_without_reservation"]["operacion_id"]:
        raise AssertionError("same-key retry did not reuse the original operation id")
    if (b_after_retry["reservation_state"], b_after_retry["stock"]) != ("confirmada", 1):
        raise AssertionError("same-key retry did not leave one confirmed reservation and one stock decrement")

    c = after_second["old_reservation_without_purchase"]
    if (c["claim_state"], len(c["purchases"]), c["reservation_state"], c["stock"]) != (
        "completed", 0, "liberada", 2
    ):
        raise AssertionError("old reservation without purchase was not safely released exactly once")
    if first["counters"].get("reservas_liberadas") != 1:
        raise AssertionError("recovery did not report exactly one release")
    if second["counters"].get("reservas_liberadas") != 0:
        raise AssertionError("second recovery repeated a release")

    db = purchases_main.SessionLocal()
    try:
        recovery_ids = first["recovery_journal_ids"] + second["recovery_journal_ids"]
        if len(recovery_ids) != 2 or len(set(recovery_ids)) != 2:
            raise AssertionError("recovery journal rows cannot be matched one-to-one for cleanup")
        purchases = {}
        for name, scenario in scenarios.items():
            purchases[name] = [row.id for row in db.query(__import__("models").Compra).filter_by(
                operacion_id=scenario["operacion_id"]
            ).all()]
    finally:
        db.close()

    return {
        "status": "verified",
        "recovery_stale_seconds": purchases_main.RECOVERY_STALE_SECONDS,
        "waited_seconds": wait_seconds,
        "before": before,
        "recoveries": {"first_sweep": first, "second_sweep": second},
        "after_first_recovery": after_first,
        "after_second_recovery": after_second,
        "retry_claim": {"http": retry_status, "purchase": b_after_retry["purchases"][0]},
        "after_same_key_retry": b_after_retry,
        "purchase_ids": purchases,
        "recovery_journal_ids": recovery_ids,
        "event_ids": {name: row["event_id"] for name, row in scenarios.items()},
        "operation_ids": {name: row["operacion_id"] for name, row in scenarios.items()},
        "cleanup_safe": True,
    }


if __name__ == "__main__":
    input_spec = {}
    try:
        input_spec = json.load(sys.stdin)
        result = main_run(input_spec)
        emit(result)
    except BaseException as exc:
        scenarios = input_spec.get("scenarios", []) if isinstance(input_spec, dict) else []
        emit({"status": "failed", "stage": STAGE,
              "error_type": type(exc).__name__, "cleanup_safe": False,
              "error_hint": str(exc)[:160] if isinstance(exc, AssertionError) else None,
              "scenario_manifest": [
                  {key: row.get(key) for key in ("name", "event_id", "event_name", "operacion_id")}
                  for row in scenarios
              ]})
        sys.exit(1)
