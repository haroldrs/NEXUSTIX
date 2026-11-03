"""Saga de pedidos. Inventario remoto; pagos simulados y emisión locales.

Cada paso toma FOR UPDATE sobre la orden; cualquier réplica puede continuar.
Los efectos remotos usan IDs deterministas persistidos antes de enviar HTTP.
No hay una transacción ACID entre Compras y Eventos.
"""
import hashlib
import io
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

import requests
from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from shared.auth_tokens import service_auth_headers


def now():
    return datetime.now(timezone.utc)


def expired(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc) <= now()


class OrderItem(BaseModel):
    model_config = {"extra": "forbid"}
    evento_id: int = Field(gt=0)
    cantidad: int = Field(ge=1, le=100)
    asientos: list[str] = Field(default_factory=list, max_length=100)


class OrderRequest(BaseModel):
    model_config = {"extra": "forbid"}
    items: list[OrderItem] = Field(min_length=1, max_length=20)


class PaymentRequest(BaseModel):
    model_config = {"extra": "forbid"}
    resultado: Literal["aprobado", "rechazado"] = "aprobado"


class ValidateRequest(BaseModel):
    model_config = {"extra": "forbid"}
    qr: str = Field(min_length=20, max_length=80)
    evento_id: int = Field(gt=0)


class RemoteUnavailable(Exception):
    pass


class RemoteRejected(Exception):
    pass


class EventsClient:
    """Reintenta solo operaciones idempotentes. Breaker local por proceso."""
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.failures = 0
        self.open_until = 0
        self.lock = threading.Lock()

    def call(self, method, path, params=None, headers=None):
        with self.lock:
            if time.monotonic() < self.open_until:
                raise RemoteUnavailable()
        for attempt in range(3):
            try:
                response = requests.request(method, self.url + path, params=params,
                    headers=headers or service_auth_headers(), timeout=(2, 5))
                if response.status_code < 500:
                    with self.lock:
                        self.failures = 0
                    if response.status_code >= 400:
                        if response.status_code in (400, 404, 409, 422):
                            raise RemoteRejected()
                        raise RemoteUnavailable()  # Auth/configuración: conservar la Saga.
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("Contrato remoto no verificable")
                    return payload
            except (requests.RequestException, ValueError):
                pass
            if attempt < 2:
                time.sleep(0.1 * (2 ** attempt))
        with self.lock:
            self.failures += 1
            if self.failures >= 3:
                self.open_until = time.monotonic() + 10
        raise RemoteUnavailable()


class Coordinator:
    def __init__(self, models, sessions, events_url, instance):
        self.m, self.sessions, self.instance = models, sessions, instance
        self.events = EventsClient(events_url)
        self.stop = threading.Event()
        self.worker = None

    def lock(self, db, order_id):
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
        row = db.query(self.m.Orden).filter_by(id=order_id).populate_existing().with_for_update().first()
        if not row:
            raise HTTPException(404, "Pedido no encontrado")
        return row

    def event(self, db, order, kind):
        key = f"{order.id}:{kind}"
        if not db.query(self.m.Outbox).filter_by(dedupe=key).first():
            db.add(self.m.Outbox(orden_id=order.id, tipo=kind, dedupe=key,
                datos={"estado": order.estado, "total_centimos": order.total_centimos}))

    def state(self, db, order, state):
        order.estado = state
        self.event(db, order, state)
        db.commit()

    def snapshot(self, db, order):
        payment = db.get(self.m.Pago, order.id)
        return {"id": order.id, "estado": order.estado, "items": order.items,
            "total_centimos": order.total_centimos, "moneda": "PEN", "expira_en": order.expira_en.isoformat(),
            "pago": payment.estado if payment else "pendiente", "motivo": order.motivo}

    def authorize(self, order, principal):
        if principal.subject != order.auth_subject and principal.role != "administrador":
            raise HTTPException(403, "Pedido de otra cuenta")

    def create(self, data, key, principal, db):
        if not key or len(key) > 128 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise HTTPException(400, "Idempotency-Key inválida")
        if sum(i.cantidad for i in data.items) > 200:
            raise HTTPException(422, "Máximo 200 entradas por pedido")
        for item in data.items:
            if item.asientos and (len(item.asientos) != item.cantidad or len(set(item.asientos)) != item.cantidad
                                  or any(not s or len(s) > 40 or ',' in s for s in item.asientos)):
                raise HTTPException(422, "Asientos inválidos")
        digest = hashlib.sha256(json.dumps(data.model_dump(), sort_keys=True).encode()).hexdigest()
        row = db.query(self.m.Orden).filter_by(clave=key).first()
        if row is None:
            order_id = uuid4().hex
            row = self.m.Orden(id=order_id, clave=key, request_hash=digest,
                auth_subject=principal.subject, usuario_id=principal.user_id, estado="reservando",
                items=[{**item.model_dump(), "operacion_id": f"ord-{order_id}-{idx}"} for idx, item in enumerate(data.items)],
                total_centimos=0, expira_en=now()+timedelta(seconds=max(30, int(os.getenv("ORDER_TTL_SECONDS", "600")))))
            db.add(row)
            try:
                db.flush()
                self.event(db, row, "reservando")
                db.commit()
            except IntegrityError:
                db.rollback()
                row = db.query(self.m.Orden).filter_by(clave=key).one()
        if row.auth_subject != principal.subject or row.request_hash != digest:
            raise HTTPException(409, "La clave pertenece a otro usuario o contenido")
        order_id = row.id
        db.rollback()
        return self.advance(db, order_id)

    def advance(self, db, order_id):
        for _ in range(6):
            order = self.lock(db, order_id)
            if order.estado in {"reservando", "reservada"} and expired(order.expira_en):
                order.motivo = "reserva_expirada"
                self.state(db, order, "cancelando")
                continue
            try:
                if order.estado == "reservando":
                    items = []
                    for item in order.items:
                        payload = self.events.call("POST", f"/eventos/{item['evento_id']}/reservar",
                            {"operacion_id": item["operacion_id"], "cantidad": item["cantidad"],
                             "asientos": ",".join(item["asientos"])})
                        self.validate_reservation(payload, item)
                        items.append({**item, "total_centimos": payload["total_centimos"]})
                    order.items = items
                    order.total_centimos = sum(i["total_centimos"] for i in items)
                    self.state(db, order, "reservada")
                    continue
                if order.estado == "pagada":
                    for item in order.items:
                        payload = self.events.call("POST", f"/eventos/{item['evento_id']}/confirmar",
                            {"operacion_id": item["operacion_id"]})
                        if payload.get("operacion_id") != item["operacion_id"] or payload.get("estado_reserva") != "confirmada":
                            raise RemoteUnavailable()
                        purchase = self.m.Compra(usuario_id=order.usuario_id, auth_subject=order.auth_subject,
                            evento_id=item["evento_id"], cantidad=item["cantidad"], estado="confirmada",
                            operacion_id=item["operacion_id"], instancia=self.instance)
                        db.add(purchase)
                        db.flush()
                        db.add(self.m.RegistroJournal(reloj_t=0, instancia=self.instance, tipo="COMMIT",
                            mensaje="Pedido confirmado", detalles={"compra_id": purchase.id, "orden_id": order.id}))
                    self.state(db, order, "emitiendo")
                    continue
                if order.estado == "emitiendo":
                    self.emit(db, order)
                    self.state(db, order, "completada")
                    continue
                if order.estado in {"cancelando", "reembolsando"}:
                    for item in order.items:
                        payload = self.events.call("POST", f"/eventos/{item['evento_id']}/devolver",
                            {"operacion_id": item["operacion_id"]})
                        if payload.get("estado") != "liberada" or payload.get("operacion_id") != item["operacion_id"]:
                            raise RemoteUnavailable()
                    refund = order.estado == "reembolsando"
                    if refund:
                        db.get(self.m.Pago, order.id).estado = "reembolsado"
                        for item in order.items:
                            db.query(self.m.Compra).filter_by(operacion_id=item["operacion_id"]).update({"estado": "reembolsada"})
                    self.state(db, order, "reembolsada" if refund else "cancelada")
                    continue
            except RemoteRejected:
                db.rollback()
                order = self.lock(db, order_id)
                if order.estado == "reservando":
                    order.motivo = "inventario_no_disponible"
                    self.state(db, order, "cancelando")
                    continue
                raise HTTPException(503, "Operación pendiente de conciliación; no se compensó un pago confirmado")
            except RemoteUnavailable:
                db.rollback()
                raise HTTPException(503, {"orden_id": order_id, "recuperacion_requerida": True}, headers={"Retry-After": "2"})
            result = self.snapshot(db, order)
            db.rollback()
            return result
        raise HTTPException(503, "Pedido pendiente de recuperación")

    @staticmethod
    def validate_reservation(payload, item):
        if (payload.get("operacion_id") != item["operacion_id"] or payload.get("evento_id") != item["evento_id"]
            or payload.get("cantidad") != item["cantidad"] or payload.get("estado_reserva") not in {"reservada", "confirmada"}
            or type(payload.get("total_centimos")) is not int or payload["total_centimos"] < 0
            or sorted(payload.get("asientos", [])) != sorted(item["asientos"])):
            raise RemoteUnavailable()

    def emit(self, db, order):
        for idx, item in enumerate(order.items):
            purchase = db.query(self.m.Compra).filter_by(operacion_id=item["operacion_id"]).one()
            for number in range(item["cantidad"]):
                db.add(self.m.Entrada(id=secrets.token_urlsafe(32), orden_id=order.id, compra_id=purchase.id,
                    evento_id=item["evento_id"], item=idx, numero=number,
                    asiento=item["asientos"][number] if item["asientos"] else None, estado="emitida"))

    def pay(self, db, order_id, principal, outcome):
        order = self.lock(db, order_id)
        self.authorize(order, principal)
        payment = db.get(self.m.Pago, order_id)
        if payment:
            original = "aprobado" if payment.estado in {"aprobado", "reembolsado"} else "rechazado"
            if original != outcome:
                raise HTTPException(409, "El resultado de pago ya está fijado")
            db.rollback()
            return self.advance(db, order_id)
        if order.estado != "reservada":
            raise HTTPException(409, "El pedido no está reservado")
        if expired(order.expira_en):
            order.motivo = "reserva_expirada"
            self.state(db, order, "cancelando")
        else:
            db.add(self.m.Pago(orden_id=order.id, estado=outcome, total_centimos=order.total_centimos))
            if outcome == "rechazado":
                order.motivo = "pago_rechazado"
            self.state(db, order, "pagada" if outcome == "aprobado" else "cancelando")
        return self.advance(db, order_id)

    def cancel(self, db, order_id, principal):
        order = self.lock(db, order_id)
        self.authorize(order, principal)
        if order.estado in {"completada", "emitiendo", "pagada"}:
            if order.estado != "completada":
                raise HTTPException(409, "Espere a que termine la emisión para reembolsar")
            if db.query(self.m.Entrada).filter_by(orden_id=order.id, estado="usada").first():
                raise HTTPException(409, "No se reembolsa un pedido con entradas utilizadas")
            db.query(self.m.Entrada).filter_by(orden_id=order.id).update({"estado": "anulada"})
            self.state(db, order, "reembolsando")
        elif order.estado in {"reservando", "reservada"}:
            order.motivo = "cancelada_por_usuario"
            self.state(db, order, "cancelando")
        else:
            db.rollback()
        return self.advance(db, order_id)

    def publish(self, db):
        rows = db.query(self.m.Outbox).filter_by(publicado_en=None).order_by(self.m.Outbox.id).with_for_update(skip_locked=True).limit(100).all()
        for row in rows:
            order = db.get(self.m.Orden, row.orden_id)
            if db.get(self.m.Notificacion, row.id) is None:
                db.add(self.m.Notificacion(evento_id=row.id, auth_subject=order.auth_subject,
                    mensaje=f"Pedido {order.id}: {row.tipo}"))
            row.publicado_en = now()
        db.commit()
        return len(rows)

    def recover(self):
        counts = {"revisadas": 0, "pendientes": 0, "publicadas": 0}
        with self.sessions() as db:
            ids = [r[0] for r in db.query(self.m.Orden.id).filter(
                self.m.Orden.estado.notin_(["completada", "cancelada", "reembolsada"])).order_by(self.m.Orden.creado_en).limit(1000)]
            db.rollback()
            for order_id in ids:
                try:
                    self.advance(db, order_id)
                    counts["revisadas"] += 1
                except (HTTPException, SQLAlchemyError):
                    db.rollback()
                    counts["pendientes"] += 1
            counts["publicadas"] = self.publish(db)
        return counts

    def start(self):
        def loop():
            while not self.stop.wait(5):
                try:
                    self.recover()
                except SQLAlchemyError:
                    # La siguiente iteración vuelve a abrir una sesión/pool_pre_ping.
                    pass
        self.stop.clear()
        self.worker = threading.Thread(target=loop, name="orders-recovery", daemon=True)
        self.worker.start()


def install(app, models, sessions, get_db, get_principal, require_admin, events_url, instance):
    coordinator = Coordinator(models, sessions, events_url, instance)

    @app.post("/ordenes", status_code=201)
    def create(data: OrderRequest, idempotency_key: str = Header(alias="Idempotency-Key"),
               db=Depends(get_db), principal=Depends(get_principal)):
        return coordinator.create(data, idempotency_key, principal, db)

    @app.get("/ordenes/mias")
    def mine(db=Depends(get_db), principal=Depends(get_principal)):
        return [coordinator.snapshot(db, o) for o in db.query(models.Orden).filter_by(auth_subject=principal.subject).order_by(models.Orden.creado_en.desc()).limit(100)]

    @app.get("/ordenes/estado")
    def status(db=Depends(get_db), _admin=Depends(require_admin)):
        counts = dict(db.query(models.Orden.estado, func.count()).group_by(models.Orden.estado).all())
        return {"estados": counts, "outbox_pendiente": db.query(models.Outbox).filter_by(publicado_en=None).count(),
                "instancia": instance, "recuperacion_activa": bool(coordinator.worker and coordinator.worker.is_alive())}

    @app.post("/ordenes/recuperar")
    def recover(_admin=Depends(require_admin)):
        return coordinator.recover()

    @app.get("/ordenes/{order_id}")
    def read(order_id: str, db=Depends(get_db), principal=Depends(get_principal)):
        order = db.get(models.Orden, order_id)
        if not order:
            raise HTTPException(404, "Pedido no encontrado")
        coordinator.authorize(order, principal)
        return coordinator.snapshot(db, order)

    @app.post("/ordenes/{order_id}/pagar")
    def pay(order_id: str, data: PaymentRequest, db=Depends(get_db), principal=Depends(get_principal)):
        return coordinator.pay(db, order_id, principal, data.resultado)

    @app.post("/ordenes/{order_id}/cancelar")
    def cancel(order_id: str, db=Depends(get_db), principal=Depends(get_principal)):
        return coordinator.cancel(db, order_id, principal)

    @app.get("/entradas/mias")
    def tickets(db=Depends(get_db), principal=Depends(get_principal)):
        import qrcode
        import qrcode.image.svg
        result = []
        for ticket in db.query(models.Entrada).join(models.Orden).filter(models.Orden.auth_subject == principal.subject).limit(500):
            output = io.BytesIO()
            qrcode.make(ticket.id, image_factory=qrcode.image.svg.SvgPathImage).save(output)
            result.append({"qr": ticket.id, "qr_svg": output.getvalue().decode(), "orden_id": ticket.orden_id,
                "evento_id": ticket.evento_id, "asiento": ticket.asiento, "estado": ticket.estado})
        return result

    @app.post("/entradas/validar")
    def validate(data: ValidateRequest, db=Depends(get_db), principal=Depends(get_principal),
                 authorization: str | None = Header(default=None)):
        if principal.role not in {"administrador", "organizador"}:
            raise HTTPException(403, "Se requiere control de acceso")
        if principal.role == "organizador":
            try:
                answer = coordinator.events.call("GET", f"/eventos/{data.evento_id}/autorizar-acceso", headers={"Authorization": authorization})
                if answer.get("autorizado") is not True:
                    raise HTTPException(403, "No administras este evento")
            except (RemoteRejected, RemoteUnavailable):
                raise HTTPException(403, "No se pudo autorizar el acceso al evento")
        ticket = db.get(models.Entrada, data.qr)
        if not ticket or ticket.evento_id != data.evento_id:
            raise HTTPException(404, "Entrada no encontrada para este evento")
        order = coordinator.lock(db, ticket.orden_id)
        db.refresh(ticket)
        if order.estado != "completada" or ticket.estado != "emitida":
            raise HTTPException(409, "Entrada utilizada o anulada")
        ticket.estado, ticket.usado_en, ticket.usado_por = "usada", now(), principal.subject
        db.add(models.Outbox(orden_id=order.id, tipo="acceso_validado", dedupe=f"acceso:{ticket.id}", datos={"evento_id": ticket.evento_id}))
        db.commit()
        return {"acceso": "permitido", "evento_id": ticket.evento_id, "asiento": ticket.asiento}

    @app.get("/notificaciones")
    def notifications(db=Depends(get_db), principal=Depends(get_principal)):
        return [{"id": n.evento_id, "mensaje": n.mensaje} for n in db.query(models.Notificacion).filter_by(auth_subject=principal.subject).order_by(models.Notificacion.evento_id.desc()).limit(100)]

    return coordinator
