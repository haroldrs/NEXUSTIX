"""Flujo nuevo sobre dos bases PostgreSQL efímeras; transporte HTTP en proceso."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pg_helpers import load_service, migrate
from shared.auth_tokens import issue_access_token
from shared.checkout import Coordinator, RemoteUnavailable

pytestmark = pytest.mark.skipif(not os.getenv("NEXUSTIX_TEST_PG_ADMIN_URL"), reason="PostgreSQL local aislado requerido")


@pytest.fixture
def flow(pg_url_factory, keys, monkeypatch):
    ev, em, ed = load_service("eventos", pg_url_factory(), monkeypatch)
    ev.esperar_bd()
    url = pg_url_factory()
    assert migrate("compras", url, monkeypatch) == 0
    co, cm, cd = load_service("compras", url, monkeypatch)
    events, purchases = TestClient(ev.app), TestClient(co.app)
    private = (keys / "jwt_private.pem").read_text()
    auth = lambda uid, role="comprador": {"Authorization": "Bearer " + issue_access_token(uid, role, private)}
    admin, buyer = auth(1, "administrador"), auth(2)
    original = co.order_coordinator.events.call
    # Mantiene serialización, validación y autenticación HTTP de ambas APIs.
    def transport(method, url, params=None, headers=None, timeout=None):
        from urllib.parse import urlparse
        return events.request(method, urlparse(url).path, params=params, headers=headers)
    monkeypatch.setattr("shared.checkout.requests.request", transport)
    def event(capacity=10, seats=None):
        result = events.post("/eventos", headers=admin, json={"nombre": "Evento de prueba", "fecha": "2027-01-01T20:00:00-05:00",
            "capacidad": capacity, "precio_centimos": 1200, "asientos": seats or []})
        assert result.status_code == 201, result.text
        return result.json()["id"]
    def order(items, key=None, headers=None):
        return purchases.post("/ordenes", headers={**(headers or buyer), "Idempotency-Key": key or uuid4().hex}, json={"items": items})
    def pay(oid, outcome="aprobado"):
        return purchases.post(f"/ordenes/{oid}/pagar", headers=buyer, json={"resultado": outcome})
    yield dict(ev=ev, em=em, ed=ed, co=co, cm=cm, cd=cd, events=events, purchases=purchases,
               auth=auth, admin=admin, buyer=buyer, event=event, order=order, pay=pay, original=original)
    events.close(); purchases.close()
    ed.engine.dispose(); cd.engine.dispose()


def invariant(f):
    response = f["events"].get("/inventario/estado", headers=f["admin"])
    assert response.status_code == 200
    assert response.json()["consistente"], response.text


def test_individual_payment_qr_and_duplicate_validation(flow):
    f = flow; eid = f["event"]()
    order = f["order"]([{"evento_id": eid, "cantidad": 2}]).json()
    assert order["estado"] == "reservada" and order["total_centimos"] == 2400
    invariant(f)
    result = f["pay"](order["id"])
    assert result.status_code == 200 and result.json()["estado"] == "completada", result.text
    tickets = f["purchases"].get("/entradas/mias", headers=f["buyer"]).json()
    assert len(tickets) == 2 and "<svg" in tickets[0]["qr_svg"]
    data = {"qr": tickets[0]["qr"], "evento_id": eid}
    assert f["purchases"].post("/entradas/validar", headers=f["buyer"], json=data).status_code == 403
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: f["purchases"].post("/entradas/validar", headers=f["admin"], json=data).status_code, range(2)))
    assert sorted(results) == [200, 409]
    assert f["purchases"].post(f"/ordenes/{order['id']}/cancelar", headers=f["buyer"]).status_code == 409
    invariant(f)


def test_group_idempotency_payment_replay_and_privacy(flow):
    f = flow; a, b = f["event"](), f["event"]()
    items = [{"evento_id": a, "cantidad": 2}, {"evento_id": b, "cantidad": 3}]
    key = uuid4().hex
    with ThreadPoolExecutor(2) as pool:
        orders = list(pool.map(lambda _: f["order"](items, key).json(), range(2)))
    assert orders[0]["id"] == orders[1]["id"]
    oid = orders[0]["id"]
    assert f["order"](items[:1], key).status_code == 409
    assert f["order"](items, key, f["auth"](3)).status_code == 409
    assert f["purchases"].get(f"/ordenes/{oid}", headers=f["auth"](3)).status_code == 403
    assert f["pay"](oid).json()["estado"] == "completada"
    assert f["pay"](oid).json()["estado"] == "completada"
    assert f["pay"](oid, "rechazado").status_code == 409
    with f["cd"].SessionLocal() as db:
        assert db.query(f["cm"].Pago).count() == 1
        assert db.query(f["cm"].Compra).count() == 2
        assert db.query(f["cm"].Entrada).count() == 5
    invariant(f)


def test_two_buyers_last_seat_no_overselling(flow):
    f = flow
    eid = f["event"](1, [{"codigo": "A1", "zona": "VIP", "precio_centimos": 5000}])
    item = [{"evento_id": eid, "cantidad": 1, "asientos": ["A1"]}]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda uid: f["order"](item, headers=f["auth"](uid)).json(), [2, 3]))
    assert sorted(o["estado"] for o in results) == ["cancelada", "reservada"]
    assert next(o for o in results if o["estado"] == "reservada")["total_centimos"] == 5000
    invariant(f)


def test_rejected_payment_compensates_once(flow):
    f = flow; eid = f["event"](2)
    oid = f["order"]([{"evento_id": eid, "cantidad": 2}]).json()["id"]
    for _ in range(2):
        result = f["pay"](oid, "rechazado")
        assert result.json()["estado"] == "cancelada", result.text
    with f["cd"].SessionLocal() as db:
        assert db.query(f["cm"].Compra).count() == db.query(f["cm"].Entrada).count() == 0
    assert f["events"].get(f"/eventos/{eid}").json()["stock"] == 2
    invariant(f)


def test_expiration_cannot_charge_and_recovery_releases(flow):
    f = flow; eid = f["event"](1)
    oid = f["order"]([{"evento_id": eid, "cantidad": 1}]).json()["id"]
    with f["cd"].SessionLocal() as db:
        db.get(f["cm"].Orden, oid).expira_en = datetime.now(timezone.utc)-timedelta(seconds=1)
        db.commit()
    assert f["pay"](oid).json()["estado"] == "cancelada"
    invariant(f)


def test_emission_failure_resumes_after_coordinator_restart(flow, monkeypatch):
    f = flow; eid = f["event"](1)
    oid = f["order"]([{"evento_id": eid, "cantidad": 1}]).json()["id"]
    def crash(*_):
        raise RemoteUnavailable()
    monkeypatch.setattr(f["co"].order_coordinator, "emit", crash)
    assert f["pay"](oid).status_code == 503
    restarted = Coordinator(f["cm"], f["cd"].SessionLocal, "http://eventos:8002", "2")
    assert restarted.recover()["pendientes"] == 0
    assert restarted.recover()["pendientes"] == 0
    with f["cd"].SessionLocal() as db:
        assert db.get(f["cm"].Orden, oid).estado == "completada"
        assert db.query(f["cm"].Entrada).count() == db.query(f["cm"].Compra).count() == 1
    invariant(f)


def test_lost_reservation_reply_retries_same_operation(flow, monkeypatch):
    f = flow; eid = f["event"](1); key = uuid4().hex
    def lost(method, path, params=None, headers=None):
        f["original"](method, path, params, headers)
        raise RemoteUnavailable()
    monkeypatch.setattr(f["co"].order_coordinator.events, "call", lost)
    assert f["order"]([{"evento_id": eid, "cantidad": 1}], key).status_code == 503
    monkeypatch.setattr(f["co"].order_coordinator.events, "call", f["original"])
    result = f["order"]([{"evento_id": eid, "cantidad": 1}], key)
    assert result.status_code == 201 and result.json()["estado"] == "reservada", result.text
    invariant(f)


def test_refund_revokes_qr_and_restores_seat_once(flow):
    f = flow; eid = f["event"](1, [{"codigo": "B1", "zona": "Plate a", "precio_centimos": 100}])
    oid = f["order"]([{"evento_id": eid, "cantidad": 1, "asientos": ["B1"]}]).json()["id"]
    assert f["pay"](oid).status_code == 200
    ticket = f["purchases"].get("/entradas/mias", headers=f["buyer"]).json()[0]
    for _ in range(2):
        result = f["purchases"].post(f"/ordenes/{oid}/cancelar", headers=f["buyer"])
        assert result.json()["estado"] == "reembolsada", result.text
    assert f["events"].get(f"/eventos/{eid}/asientos").json()[0]["disponible"]
    assert f["purchases"].post("/entradas/validar", headers=f["admin"], json={"qr": ticket["qr"], "evento_id": eid}).status_code == 409
    invariant(f)


def test_repeated_outbox_delivery_has_one_notification_per_event(flow):
    f = flow; eid = f["event"]()
    oid = f["order"]([{"evento_id": eid, "cantidad": 1}]).json()["id"]
    f["pay"](oid)
    coordinator = f["co"].order_coordinator
    coordinator.recover()
    with f["cd"].SessionLocal() as db:
        count = db.query(f["cm"].Notificacion).count()
        db.query(f["cm"].Outbox).update({"publicado_en": None})
        db.commit()
    coordinator.recover()
    with f["cd"].SessionLocal() as db:
        assert db.query(f["cm"].Notificacion).count() == count > 0


def test_legacy_recovery_cannot_release_order_reservations(flow, monkeypatch):
    f = flow; eid = f["event"](1)
    oid = f["order"]([{"evento_id": eid, "cantidad": 1}]).json()["id"]
    from test_pg_integracion import EventosEnProceso
    adapter = EventosEnProceso(f["ev"], f["ed"].SessionLocal)
    monkeypatch.setattr(f["co"].requests, "get", adapter.get)
    monkeypatch.setattr(f["co"].requests, "post", adapter.post)
    with f["cd"].SessionLocal() as db:
        f["co"].ejecutar_recuperacion(db, permitir_compensacion=True, incluir_auditoria=False, origen="test")
    assert f["purchases"].get(f"/ordenes/{oid}", headers=f["buyer"]).json()["estado"] == "reservada"
    assert f["events"].get(f"/eventos/{eid}").json()["stock"] == 0
    invariant(f)


def test_missing_event_cancels_group_and_blocks_delayed_reservation(flow):
    f = flow; eid = f["event"](1)
    result = f["order"]([{"evento_id": eid, "cantidad": 1}, {"evento_id": eid + 1, "cantidad": 1}])
    assert result.status_code == 201 and result.json()["estado"] == "cancelada", result.text
    op = result.json()["items"][1]["operacion_id"]
    new_id = f["event"](1)
    assert new_id == eid + 1
    from shared.auth_tokens import service_auth_headers
    delayed = f["events"].post(f"/eventos/{new_id}/reservar", headers=service_auth_headers(),
        params={"cantidad": 1, "operacion_id": op})
    assert delayed.status_code == 409
    assert f["events"].get(f"/eventos/{new_id}").json()["stock"] == 1
    invariant(f)


def test_organizer_can_only_edit_owned_events_and_actions_are_audited(flow):
    f = flow
    data = {"nombre": "Organizado", "fecha": "2027-01-01T20:00:00-05:00", "capacidad": 3, "precio_centimos": 100}
    organizer = f["auth"](20, "organizador")
    assert f["events"].post("/eventos", headers=f["buyer"], json=data).status_code == 403
    r = f["events"].post("/eventos", headers=organizer, json=data)
    assert r.status_code == 201
    eid = r.json()["id"]
    edit = {"nombre": "Editado", "fecha": data["fecha"]}
    assert f["events"].put(f"/eventos/{eid}", headers=f["auth"](20, "comprador"), json=edit).status_code == 403
    assert f["events"].put(f"/eventos/{eid}", headers=f["auth"](21, "organizador"), json=edit).status_code == 403
    assert f["events"].put(f"/eventos/{eid}", headers=organizer, json=edit).status_code == 200
    with f["ed"].SessionLocal() as db:
        assert db.query(f["em"].AuditoriaCatalogo).filter_by(evento_id=eid).count() == 2
