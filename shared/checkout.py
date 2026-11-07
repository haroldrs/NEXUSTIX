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
from fastapi import Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, text, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from shared.auth_tokens import service_auth_headers
from shared.telemetry import correlation_id


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
                request_headers = dict(headers or service_auth_headers())
                if correlation_id.get():
                    request_headers["X-Correlation-ID"] = correlation_id.get()
                response = requests.request(method, self.url + path, params=params,
                    headers=request_headers, timeout=(2, 5), allow_redirects=False)
                if 200 <= response.status_code < 500:
                    with self.lock:
                        self.failures = 0
                    if response.status_code >= 300:
                        if response.status_code in (400, 404, 409, 422):
                            raise RemoteRejected()
                        raise RemoteUnavailable()  # Auth/configuración: conservar la Saga.
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("Contrato remoto no verificable")
                    return payload
            except (requests.RequestException, ValueError, RuntimeError):
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
        self.cancellation_cursor = 0

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
                datos={"estado": order.estado, "total_centimos": order.total_centimos,
                       "correlation_id": correlation_id.get(), "instancia": self.instance}))

    def state(self, db, order, state):
        order.estado = state
        self.event(db, order, state)
        db.commit()

    def snapshot(self, db, order):
        payment = db.get(self.m.Pago, order.id)
        return {"id": order.id, "estado": order.estado, "items": order.items,
            "total_centimos": order.total_centimos, "moneda": "PEN", "expira_en": order.expira_en.isoformat(),
            "pago": payment.estado if payment else "pendiente", "motivo": order.motivo,
            "reembolsado_centimos": payment.reembolsado_centimos if payment else 0,
            "creado_en": order.creado_en.isoformat() if order.creado_en else None}

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
            if db.query(self.m.CancelacionEvento).filter(self.m.CancelacionEvento.evento_id.in_([i.evento_id for i in data.items])).first():
                raise HTTPException(409, "Uno de los eventos está en cancelación")
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
                        if item.get("cancelado"):
                            continue
                        payload = self.events.call("POST", f"/eventos/{item['evento_id']}/confirmar",
                            {"operacion_id": item["operacion_id"]})
                        if payload.get("operacion_id") != item["operacion_id"] or payload.get("estado_reserva") != "confirmada":
                            raise RemoteUnavailable()
                        purchase = db.query(self.m.Compra).filter_by(operacion_id=item["operacion_id"]).first()
                        if purchase is None:
                            purchase = self.m.Compra(usuario_id=order.usuario_id, auth_subject=order.auth_subject,
                                evento_id=item["evento_id"], cantidad=item["cantidad"], estado="confirmada",
                                operacion_id=item["operacion_id"], instancia=self.instance)
                            db.add(purchase)
                            db.flush()
                            db.add(self.m.RegistroJournal(reloj_t=0, instancia=self.instance, tipo="COMMIT",
                                mensaje="Pedido confirmado", detalles={"compra_id": purchase.id, "orden_id": order.id,
                                    "correlation_id": correlation_id.get()}))
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
                        for item in order.items:
                            db.query(self.m.Compra).filter_by(operacion_id=item["operacion_id"]).update({"estado": "reembolsada"})
                    for record in db.query(self.m.ReembolsoEvento).filter_by(orden_id=order.id).all():
                        self.finish_refund(db, order, record)
                    if refund:
                        payment = db.get(self.m.Pago, order.id)
                        payment.estado, payment.reembolsado_centimos = "reembolsado", payment.total_centimos
                    order.items = [{**item, "cancelado": True} for item in order.items]
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
            if item.get("cancelado"):
                continue
            purchase = db.query(self.m.Compra).filter_by(operacion_id=item["operacion_id"]).one()
            for number in range(item["cantidad"]):
                if db.query(self.m.Entrada).filter_by(orden_id=order.id, item=idx, numero=number).first():
                    continue
                db.add(self.m.Entrada(id=secrets.token_urlsafe(32), orden_id=order.id, compra_id=purchase.id,
                    evento_id=item["evento_id"], item=idx, numero=number,
                    asiento=item["asientos"][number] if item["asientos"] else None, estado="emitida"))

    def pay(self, db, order_id, principal, outcome):
        order = self.lock(db, order_id)
        self.authorize(order, principal)
        payment = db.get(self.m.Pago, order_id)
        if payment:
            original = "aprobado" if payment.estado in {"aprobado", "reembolsado", "reembolsado_parcial"} else "rechazado"
            if original != outcome:
                raise HTTPException(409, "El resultado de pago ya está fijado")
            db.rollback()
            return self.advance(db, order_id)
        if order.estado != "reservada":
            raise HTTPException(409, "El pedido no está reservado")
        if db.query(self.m.CancelacionEvento).filter(self.m.CancelacionEvento.evento_id.in_([i["evento_id"] for i in order.items])).first():
            order.motivo = "evento_cancelado"
            self.state(db, order, "cancelando")
            return self.advance(db, order_id)
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
            for event_id in {item["evento_id"] for item in order.items}:
                self.refund_record(db, order, event_id, "Cancelación solicitada por el comprador", principal.subject)
            db.query(self.m.Entrada).filter_by(orden_id=order.id).update({"estado": "anulada"})
            self.state(db, order, "reembolsando")
        elif order.estado in {"reservando", "reservada"}:
            order.motivo = "cancelada_por_usuario"
            self.state(db, order, "cancelando")
        else:
            db.rollback()
        return self.advance(db, order_id)

    def cancel_event(self, db, event_id, principal, motivo):
        """Persistir la intención antes de contactar Eventos permite reanudar."""
        task = db.get(self.m.CancelacionEvento, event_id)
        if task is None:
            task = self.m.CancelacionEvento(evento_id=event_id, estado="pendiente", motivo=motivo,
                solicitada_por=principal.subject)
            db.add(task)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
        # Solo el cierre remoto se intenta en la petición HTTP; el lote durable
        # continúa en el worker y no depende de la conexión del navegador.
        self.close_event_sales(db, event_id)
        task = db.get(self.m.CancelacionEvento, event_id)
        return self.cancel_event_status(db, task)

    def close_event_sales(self, db, event_id):
        task = db.query(self.m.CancelacionEvento).filter_by(evento_id=event_id).with_for_update().one()
        if task.ventas_cerradas_en:
            db.rollback()
            return True
        try:
            result = self.events.call("POST", f"/interno/eventos/{event_id}/cancelar",
                {"motivo": task.motivo, "actor": task.solicitada_por})
            if result.get("evento_id") != event_id or result.get("estado") != "cancelado":
                raise RemoteUnavailable()
        except (RemoteRejected, RemoteUnavailable):
            task.estado, task.ultimo_error = "error_recuperable", "No se pudo confirmar el cierre de ventas en Eventos; se reintentará"
            db.commit()
            return False
        task.ventas_cerradas_en, task.estado, task.ultimo_error = now(), "procesando", None
        db.commit()
        return True

    def refund_record(self, db, order, event_id, reason, actor):
        # Llamante conserva FOR UPDATE sobre la orden; la unicidad en PostgreSQL
        # es la defensa durable adicional entre réplicas.
        record = db.query(self.m.ReembolsoEvento).filter_by(orden_id=order.id, evento_id=event_id).first()
        if record and record.tipo not in {"requiere_conciliacion", "verificacion_pendiente"}:
            return record
        payment = db.get(self.m.Pago, order.id)
        paid = payment is not None and payment.estado in {"aprobado", "reembolsado_parcial", "reembolsado"}
        items = [item for item in order.items if item["evento_id"] == event_id]
        if paid and any(type(i.get("total_centimos")) is not int or i["total_centimos"] < 0 for i in items):
            raise HTTPException(409, "Importe histórico no verificable; requiere conciliación manual")
        if record:
            # Un fallo previo a persistir el trabajo no convierte un importe
            # verificable en una conciliación manual permanente.
            record.tipo = "reembolso_simulado" if paid else "liberacion_reserva"
            record.total_centimos = sum(i["total_centimos"] for i in items) if paid else 0
            db.flush()
            return record
        record = self.m.ReembolsoEvento(id=uuid4().hex, orden_id=order.id, evento_id=event_id,
            tipo="reembolso_simulado" if paid else "liberacion_reserva", estado="pendiente",
            total_centimos=sum(i["total_centimos"] for i in items) if paid else 0,
            motivo=reason, solicitado_por=actor)
        db.add(record)
        db.flush()
        self.event(db, order, f"reembolso:{event_id}:pendiente")
        return record

    def finish_refund(self, db, order, record):
        if record.estado == "completado":
            return
        if record.tipo == "reembolso_simulado":
            payment = db.get(self.m.Pago, order.id)
            if not payment or payment.reembolsado_centimos + record.total_centimos > payment.total_centimos:
                raise HTTPException(409, "El reembolso excede el pago verificable; requiere conciliación")
            payment.reembolsado_centimos += record.total_centimos
            payment.estado = "reembolsado" if payment.reembolsado_centimos == payment.total_centimos else "reembolsado_parcial"
        record.estado, record.ultimo_error = "completado", None
        self.event(db, order, f"reembolso:{record.evento_id}:completado")
        db.add(self.m.RegistroJournal(reloj_t=0, instancia=self.instance, tipo="COMPENSACION",
            mensaje="Compensación por evento completada", detalles={"orden_id": order.id,
                "evento_id": record.evento_id, "reembolso_id": record.id,
                "total_centimos": record.total_centimos, "tipo": record.tipo,
                "correlation_id": correlation_id.get()}))

    def compensate_event_order(self, db, order_id, event_id, reason, actor):
        order = self.lock(db, order_id)
        if order.estado in {"cancelada", "reembolsada"}:
            db.rollback()
            return
        if order.estado in {"reservando", "reservada"}:
            # El pago de un pedido es indivisible. Antes de pagar se libera todo
            # el grupo, permitiendo al comprador crear un pedido actualizado.
            for affected_id in {item["evento_id"] for item in order.items}:
                self.refund_record(db, order, affected_id, reason, actor)
            order.motivo = "evento_cancelado"
            self.state(db, order, "cancelando")
            self.advance(db, order_id)
            return
        if order.estado in {"cancelando", "reembolsando"}:
            db.rollback()
            self.advance(db, order_id)
            return
        record = self.refund_record(db, order, event_id, reason, actor)
        if record.estado == "completado":
            db.rollback()
            return
        record.estado, record.ultimo_error = "procesando", None
        record.intentos += 1
        order.items = [{**item, "cancelado": True} if item["evento_id"] == event_id else item for item in order.items]
        # La invalidación se persiste ANTES del efecto remoto. Caer en cualquiera
        # de los pasos deja una compensación reanudable y un QR inutilizable.
        db.query(self.m.Entrada).filter_by(orden_id=order.id, evento_id=event_id).update({"estado": "anulada"})
        db.commit()
        order = self.lock(db, order_id)
        record = db.query(self.m.ReembolsoEvento).filter_by(orden_id=order_id, evento_id=event_id).one()
        if record.estado == "completado":
            db.rollback()
            return
        for item in order.items:
            if item["evento_id"] != event_id:
                continue
            payload = self.events.call("POST", f"/eventos/{event_id}/devolver", {"operacion_id": item["operacion_id"]})
            if payload.get("estado") != "liberada" or payload.get("operacion_id") != item["operacion_id"] or payload.get("evento_id") != event_id:
                raise RemoteUnavailable()
            db.query(self.m.Compra).filter_by(operacion_id=item["operacion_id"]).update({"estado": "reembolsada"})
        self.finish_refund(db, order, record)
        db.flush()
        if all(item.get("cancelado") for item in order.items):
            remaining = db.query(self.m.ReembolsoEvento).filter(self.m.ReembolsoEvento.orden_id == order_id,
                self.m.ReembolsoEvento.estado != "completado").count()
            if remaining == 0:
                order.estado = "reembolsada"
                self.event(db, order, "reembolsada")
        db.commit()

    def discover_cancellations(self, db):
        """Importación idempotente: recupera incluso cierres hechos en Eventos."""
        result = self.events.call("GET", "/interno/eventos/cancelados", {"after_id": self.cancellation_cursor})
        rows = result.get("items")
        if not isinstance(rows, list):
            raise RemoteUnavailable()
        for row in rows:
            event_id = row.get("evento_id")
            if type(event_id) is not int or event_id <= 0:
                raise RemoteUnavailable()
            if db.get(self.m.CancelacionEvento, event_id) is None:
                db.add(self.m.CancelacionEvento(evento_id=event_id, estado="pendiente",
                    motivo=(row.get("motivo") or "Cancelación administrativa")[:300],
                    solicitada_por=(row.get("solicitada_por") or "catalogo:administrador")[:128]))
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
            self.cancellation_cursor = event_id
        if len(rows) < 100:
            self.cancellation_cursor = 0
        db.rollback()

    def process_cancellation(self, db, event_id):
        if not self.close_event_sales(db, event_id):
            return
        task = db.get(self.m.CancelacionEvento, event_id)
        reason, actor = task.motivo, task.solicitada_por
        # JSON en el esquema existente: se leen solo IDs/items, sin información
        # del comprador. Los efectos y reintentos se serializan por cada pedido.
        rows = db.query(self.m.Orden.id, self.m.Orden.items, self.m.Orden.estado).all()
        affected = [r.id for r in rows if any(i.get("evento_id") == event_id for i in r.items)]
        completed = {r.orden_id for r in db.query(self.m.ReembolsoEvento.orden_id).filter_by(evento_id=event_id, estado="completado")}
        terminal = {r.id for r in rows if r.estado in {"cancelada", "reembolsada"}}
        attempted = {r.orden_id: r.actualizado_en.timestamp() for r in
                     db.query(self.m.ReembolsoEvento).filter_by(evento_id=event_id)}
        pending = sorted((order_id for order_id in affected if order_id not in completed | terminal),
                         key=lambda order_id: (attempted.get(order_id, 0), order_id))
        db.rollback()
        for order_id in pending[:20]:
            try:
                self.compensate_event_order(db, order_id, event_id, reason, actor)
            except (RemoteUnavailable, RemoteRejected, HTTPException, SQLAlchemyError):
                db.rollback()
                # Guardar errores sin detalles de excepciones, URLs ni secretos.
                order = self.lock(db, order_id)
                record = db.query(self.m.ReembolsoEvento).filter_by(orden_id=order_id, evento_id=event_id).first()
                if record is None:
                    record = self.m.ReembolsoEvento(id=uuid4().hex, orden_id=order_id, evento_id=event_id,
                        tipo="verificacion_pendiente", estado="error_recuperable", total_centimos=0,
                        motivo=reason, solicitado_por=actor, intentos=1)
                    db.add(record)
                if record.estado != "completado":
                    record.estado, record.ultimo_error = "error_recuperable", "Compensación pendiente; revisar disponibilidad o importe histórico"
                    record.actualizado_en = now()
                db.commit()
        self.summarize_cancellation(db, event_id)

    def summarize_cancellation(self, db, event_id):
        remote = self.events.call("GET", f"/interno/eventos/{event_id}/reservas-activas")
        if remote.get("evento_id") != event_id or remote.get("cancelado") is not True or not isinstance(remote.get("reservas"), list):
            raise RemoteUnavailable()
        task = db.query(self.m.CancelacionEvento).filter_by(evento_id=event_id).with_for_update().one()
        rows = db.query(self.m.Orden.id, self.m.Orden.items, self.m.Orden.estado).all()
        affected = {r.id: r for r in rows if any(i.get("evento_id") == event_id for i in r.items)}
        known_ops = {i["operacion_id"] for row in affected.values() for i in row.items if i["evento_id"] == event_id}
        legacy_ops = {r.operacion_id or f"compra:{r.id}" for r in db.query(self.m.Compra).filter_by(evento_id=event_id, estado="confirmada")
                      if r.operacion_id not in known_ops}
        # Toda reserva remota sin orden permanece visible: incluye legacy en
        # vuelo y compras cuyo pago no tiene un importe verificable.
        legacy_ops.update(r.get("operacion_id") for r in remote["reservas"] if r.get("operacion_id") not in known_ops)
        refunds = {r.orden_id: r for r in db.query(self.m.ReembolsoEvento).filter_by(evento_id=event_id)}
        finished = sum(1 for oid, order in affected.items() if order.estado in {"cancelada", "reembolsada"}
                       or (oid in refunds and refunds[oid].estado == "completado"))
        errors = sum(1 for oid, record in refunds.items() if oid in affected and record.estado == "error_recuperable") + len(legacy_ops)
        task.total_ordenes, task.completadas = len(affected) + len(legacy_ops), finished
        task.errores, task.legacy_pendientes = errors, len(legacy_ops)
        pending = task.total_ordenes - finished
        if not pending and not remote["reservas"]:
            task.estado, task.ultimo_error = "completado", None
        elif errors:
            task.estado = "error_recuperable"
            task.ultimo_error = "Existen ventas o reservas legacy sin pago verificable; requieren conciliación" if legacy_ops else "Hay compensaciones pendientes de reintento"
        else:
            task.estado, task.ultimo_error = "procesando", "Esperando compensaciones o reservas en tránsito" if remote["reservas"] else None
        task.actualizado_en = now()
        db.commit()

    @staticmethod
    def cancel_event_status(db, task):
        return {"evento_id": task.evento_id, "estado": task.estado, "motivo": task.motivo,
                "solicitada_por": task.solicitada_por,
                "creado_en": task.creado_en.isoformat() if task.creado_en else None,
                "total_ordenes": task.total_ordenes, "completadas": task.completadas,
                "errores": task.errores, "pendientes": max(0, task.total_ordenes - task.completadas),
                "legacy_pendientes": task.legacy_pendientes, "ultimo_error": task.ultimo_error,
                "ventas_cerradas": task.ventas_cerradas_en is not None,
                "actualizado_en": task.actualizado_en.isoformat() if task.actualizado_en else None}

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
        counts = {"revisadas": 0, "pendientes": 0, "publicadas": 0, "cancelaciones_revisadas": 0}
        with self.sessions() as db:
            try:
                self.discover_cancellations(db)
            except (RemoteRejected, RemoteUnavailable, SQLAlchemyError):
                db.rollback()
            # También se revisan cierres completados: una operación legacy en
            # tránsito jamás queda oculta por un contador calculado anteriormente.
            event_ids = [r[0] for r in db.query(self.m.CancelacionEvento.evento_id).order_by(self.m.CancelacionEvento.actualizado_en).limit(20)]
            db.rollback()
            for event_id in event_ids:
                try:
                    self.process_cancellation(db, event_id)
                    counts["cancelaciones_revisadas"] += 1
                except (RemoteRejected, RemoteUnavailable, HTTPException, SQLAlchemyError):
                    db.rollback()
                    task = db.get(self.m.CancelacionEvento, event_id)
                    if task:
                        task.estado, task.ultimo_error = "error_recuperable", "No se pudo verificar la compensación; el worker reintentará"
                        task.actualizado_en = now()
                        db.commit()
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

    @app.get("/checkout/capacidades")
    def capabilities():
        return {"pedidos": True, "pago_simulado": True, "qr": True,
                "max_por_item": 100, "max_por_pedido": 200}

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

    @app.post("/eventos/{event_id}/cancelar", status_code=202)
    def cancel_event(event_id: int, motivo: str = Query(default="Cancelación administrativa", min_length=3, max_length=300), db=Depends(get_db),
                     principal=Depends(require_admin)):
        if event_id <= 0 or len(motivo.strip()) < 3:
            raise HTTPException(422, "Evento y motivo de cancelación válidos requeridos")
        # Antes de guardar la intención se comprueba la identidad del evento.
        # Si ya existe una intención, se permite reintentar durante una caída.
        if db.get(models.CancelacionEvento, event_id) is None:
            try:
                event = coordinator.events.call("GET", f"/eventos/{event_id}")
                if event.get("id") != event_id:
                    raise RemoteUnavailable()
            except RemoteRejected:
                raise HTTPException(404, "Evento no encontrado")
            except RemoteUnavailable:
                raise HTTPException(503, "No se pudo verificar el evento; intente nuevamente")
        return coordinator.cancel_event(db, event_id, principal, motivo.strip())

    def page(query, limit, offset, serialize):
        return {"items": [serialize(row) for row in query.offset(offset).limit(limit).all()],
                "total": query.order_by(None).count(), "limit": limit, "offset": offset}

    @app.get("/admin/ventas")
    def sales(evento_id: int | None = Query(default=None, gt=0), estado: str | None = None,
              limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
              db=Depends(get_db), _admin=Depends(require_admin)):
        query = db.query(models.Compra).order_by(models.Compra.id.desc())
        if evento_id is not None:
            query = query.filter_by(evento_id=evento_id)
        if estado:
            query = query.filter_by(estado=estado)
        def row(purchase):
            ticket = db.query(models.Entrada.orden_id).filter_by(compra_id=purchase.id).first()
            return {"id": purchase.id, "evento_id": purchase.evento_id, "cantidad": purchase.cantidad,
                    "estado": purchase.estado, "instancia": purchase.instancia,
                    "orden_id": ticket[0] if ticket else None,
                    "creado_en": purchase.creado_en.isoformat() if purchase.creado_en else None}
        return page(query, limit, offset, row)

    @app.get("/admin/ordenes")
    def orders(evento_id: int | None = Query(default=None, gt=0), estado: str | None = None, limit: int = Query(default=50, ge=1, le=200),
               offset: int = Query(default=0, ge=0), db=Depends(get_db), _admin=Depends(require_admin)):
        query = db.query(models.Orden).order_by(models.Orden.creado_en.desc(), models.Orden.id)
        if evento_id is not None:
            # El esquema existente conserva los items como JSON, no JSONB.
            predicate = ("EXISTS (SELECT 1 FROM json_array_elements(CAST(ordenes.items AS json)) AS item "
                         "WHERE CAST(item->>'evento_id' AS integer) = :filter_event_id)")
            if db.get_bind().dialect.name == "sqlite":
                predicate = ("EXISTS (SELECT 1 FROM json_each(ordenes.items) AS item "
                             "WHERE json_extract(item.value, '$.evento_id') = :filter_event_id)")
            query = query.filter(text(predicate)).params(filter_event_id=evento_id)
        if estado:
            query = query.filter_by(estado=estado)
        return page(query, limit, offset, lambda order: coordinator.snapshot(db, order))

    @app.get("/admin/entradas")
    def admin_tickets(evento_id: int | None = Query(default=None, gt=0), estado: str | None = None,
                      limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                      db=Depends(get_db), _admin=Depends(require_admin)):
        query = db.query(models.Entrada).order_by(models.Entrada.compra_id.desc(), models.Entrada.item, models.Entrada.numero)
        if evento_id is not None:
            query = query.filter_by(evento_id=evento_id)
        if estado:
            query = query.filter_by(estado=estado)
        return page(query, limit, offset, lambda ticket: {
            "referencia": f"{ticket.compra_id}:{ticket.item}:{ticket.numero}", "orden_id": ticket.orden_id,
            "compra_id": ticket.compra_id, "evento_id": ticket.evento_id, "asiento": ticket.asiento,
            "estado": ticket.estado, "usado_en": ticket.usado_en.isoformat() if ticket.usado_en else None})

    @app.get("/admin/reembolsos")
    def refunds(evento_id: int | None = Query(default=None, gt=0), estado: str | None = None,
                limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                db=Depends(get_db), _admin=Depends(require_admin)):
        query = db.query(models.ReembolsoEvento).order_by(models.ReembolsoEvento.creado_en.desc(), models.ReembolsoEvento.id)
        if evento_id is not None:
            query = query.filter_by(evento_id=evento_id)
        if estado:
            query = query.filter_by(estado=estado)
        return page(query, limit, offset, lambda row: {
            "id": row.id, "orden_id": row.orden_id, "evento_id": row.evento_id, "tipo": row.tipo,
            "estado": row.estado, "total_centimos": row.total_centimos, "moneda": "PEN", "motivo": row.motivo,
            "intentos": row.intentos, "ultimo_error": row.ultimo_error,
            "creado_en": row.creado_en.isoformat() if row.creado_en else None,
            "actualizado_en": row.actualizado_en.isoformat() if row.actualizado_en else None})

    @app.get("/admin/cancelaciones")
    def cancellations(estado: str | None = None, limit: int = Query(default=50, ge=1, le=200),
                      offset: int = Query(default=0, ge=0), db=Depends(get_db), _admin=Depends(require_admin)):
        query = db.query(models.CancelacionEvento).order_by(models.CancelacionEvento.creado_en.desc())
        if estado:
            query = query.filter_by(estado=estado)
        return page(query, limit, offset, lambda task: coordinator.cancel_event_status(db, task))

    @app.get("/eventos/{event_id}/cancelacion")
    def cancellation_status(event_id: int, db=Depends(get_db), _admin=Depends(require_admin)):
        task = db.get(models.CancelacionEvento, event_id)
        if not task:
            raise HTTPException(404, "No hay una cancelación registrada")
        return coordinator.cancel_event_status(db, task)

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
                "evento_id": ticket.evento_id, "asiento": ticket.asiento, "estado": ticket.estado,
                "usado_en": ticket.usado_en.isoformat() if ticket.usado_en else None})
        return result

    @app.post("/entradas/validar")
    def validate(data: ValidateRequest, db=Depends(get_db), principal=Depends(get_principal),
                 authorization: str | None = Header(default=None)):
        if principal.role not in {"administrador", "organizador"}:
            raise HTTPException(403, "Se requiere control de acceso")
        if db.get(models.CancelacionEvento, data.evento_id):
            raise HTTPException(409, "Evento en cancelación; acceso bloqueado")
        try:
            event = coordinator.events.call("GET", f"/eventos/{data.evento_id}")
            if event.get("id") != data.evento_id or event.get("cancelado_en") or event.get("activo") is not True:
                raise HTTPException(409, "Evento cerrado o cancelado")
        except (RemoteRejected, RemoteUnavailable):
            raise HTTPException(503, "No se pudo verificar que el evento admite acceso")
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
