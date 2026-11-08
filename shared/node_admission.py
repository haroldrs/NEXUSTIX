"""Anuncios Ed25519 para revisión administrativa; nunca descubren ni activan hosts.

Una firma demuestra posesión de clave. La autorización se obtiene únicamente
del pin de identidad en el inventario montado por el operador, no del anuncio.
El registro es acotado, local al observador y no contiene claves privadas.
"""
import base64
from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import re
from threading import RLock
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

DOMAIN = b"NEXUSTIX-NODE-ANNOUNCEMENT-V1\n"
MAX_BYTES = 16384
MAX_CANDIDATES = 64
MAX_REPLAYS = 512
PRIVATE = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


class AnnouncementError(ValueError):
    pass


def canonical(payload):
    return DOMAIN + json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise AnnouncementError("El anuncio no cumple el formato esperado")


def _slug(value, maximum):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0," + str(maximum - 1) + "}", value):
        raise AnnouncementError("Identificador de anuncio inválido")


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise AnnouncementError("Capacidad o fecha de anuncio inválida")


def verify(envelope, deployment, epoch=None):
    _keys(envelope, {"version", "public_key", "payload", "signature"})
    if type(envelope["version"]) is not int or envelope["version"] != 1:
        raise AnnouncementError("Versión de anuncio incompatible")
    payload = envelope["payload"]
    _keys(payload, {"deployment", "node_id", "address", "platform", "failure_domain", "resources", "release", "issued_at", "expires_at", "nonce"})
    _slug(payload["deployment"], 24)
    _slug(payload["node_id"], 20)
    _slug(payload["failure_domain"], 40)
    if payload["deployment"] != deployment:
        raise AnnouncementError("El anuncio corresponde a otra instalación")
    if not isinstance(payload["address"], str):
        raise AnnouncementError("La dirección LAN debe ser una cadena IPv4")
    try:
        address = ipaddress.ip_address(payload["address"])
    except (TypeError, ValueError):
        raise AnnouncementError("Dirección LAN inválida") from None
    if address.version != 4 or not any(address in subnet for subnet in PRIVATE):
        raise AnnouncementError("El anuncio requiere una dirección IPv4 privada de LAN")
    if payload["platform"] not in ("fedora", "wsl2", "linux"):
        raise AnnouncementError("Plataforma de anuncio no admitida")
    _keys(payload["resources"], {"cpu_millicores", "memory_mb"})
    _integer(payload["resources"]["cpu_millicores"], 250, 256000)
    _integer(payload["resources"]["memory_mb"], 128, 1048576)
    if not isinstance(payload["release"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", payload["release"]):
        raise AnnouncementError("Etiqueta de versión inválida")
    if not isinstance(payload["nonce"], str) or not re.fullmatch(r"[a-f0-9]{32}", payload["nonce"]):
        raise AnnouncementError("Identificador de anuncio inválido")
    for key in ("issued_at", "expires_at"):
        _integer(payload[key], 0, 4102444800)
    epoch = int(time.time()) if epoch is None else epoch
    if (payload["issued_at"] > epoch + 60 or payload["expires_at"] <= epoch
            or not 1 <= payload["expires_at"] - payload["issued_at"] <= 900):
        raise AnnouncementError("Anuncio caducado, futuro o con vigencia superior a 15 minutos")
    try:
        if not isinstance(envelope["public_key"], str) or not isinstance(envelope["signature"], str):
            raise ValueError()
        public = base64.b64decode(envelope["public_key"], validate=True)
        signature = base64.b64decode(envelope["signature"], validate=True)
        if len(public) != 32 or len(signature) != 64:
            raise ValueError()
        Ed25519PublicKey.from_public_bytes(public).verify(signature, canonical(payload))
    except (ValueError, TypeError, InvalidSignature):
        raise AnnouncementError("Firma Ed25519 no válida") from None
    return hashlib.sha256(public).hexdigest(), payload


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class AnnouncementRegistry:
    def __init__(self):
        self.lock = RLock()
        self.records = OrderedDict()
        self.seen = OrderedDict()

    def ingest(self, envelope, document):
        fingerprint, payload = verify(envelope, document["deployment"])
        key = (document["deployment"], fingerprint)
        replay = (*key, payload["nonce"])
        with self.lock:
            previous = self.records.get(key)
            if replay in self.seen:
                if previous and previous["payload"] == payload:
                    return self.view(previous, document), False
                raise AnnouncementError("Anuncio ya procesado; genere uno nuevo")
            if previous and previous["payload"]["issued_at"] >= payload["issued_at"]:
                raise AnnouncementError("El observador ya recibió un anuncio más reciente de esta identidad")
            record = {"id": fingerprint, "payload": payload, "recibido_en": iso(time.time())}
            self.records[key] = record
            self.records.move_to_end(key)
            self.seen[replay] = True
            while len(self.records) > MAX_CANDIDATES:
                self.records.popitem(last=False)
            while len(self.seen) > MAX_REPLAYS:
                self.seen.popitem(last=False)
            return self.view(record, document), previous is None

    @staticmethod
    def view(record, document):
        payload = record["payload"]
        node = next((node for node in document["nodes"] if node["id"] == payload["node_id"]), None)
        pinned = bool(node and node.get("identity_fingerprint") == record["id"])
        expired = payload["expires_at"] <= time.time()
        resources = {"cpus": payload["resources"]["cpu_millicores"] / 1000, "memory_mb": payload["resources"]["memory_mb"]}
        reasons = []
        if node:
            reasons.extend(f"{key} difiere del inventario" for key in ("address", "platform", "failure_domain") if node[key] != payload[key])
            if any(resources[key] < node["resources"][key] for key in ("cpus", "memory_mb")):
                reasons.append("Recursos anunciados inferiores al presupuesto configurado")
            if document.get("release", "sin-etiqueta") != payload["release"]:
                reasons.append("La etiqueta de versión difiere del inventario")
        else:
            reasons.append("Nodo pendiente de asignación y validación en el inventario")
        if any(n["id"] != payload["node_id"] and n.get("identity_fingerprint") == record["id"] for n in document["nodes"]):
            reasons.append("Huella asignada a otro nodo")
        return {"id": record["id"], "node_id": payload["node_id"], "address": payload["address"],
            "platform": payload["platform"], "failure_domain": payload["failure_domain"], "resources": resources,
            "release": payload["release"], "emitido_en": iso(payload["issued_at"]), "expira_en": iso(payload["expires_at"]),
            "recibido_en": record["recibido_en"], "firma_valida": True, "identidad_validada": pinned and not expired,
            "autorizado": pinned, "configurado": node is not None, "incorporado": pinned and node is not None,
            "compatibilidad": {"estado": "discordante" if reasons and node else "pendiente" if not node else "compatible_declarado", "motivos": reasons},
            "estado": "expirado" if expired else "autorizado_por_inventario" if pinned else "pendiente_autorizacion",
            "alcance": "Declaración firmada: no acredita conectividad, capacidad medida ni activación del nodo."}

    def list(self, document):
        if not document:
            return []
        with self.lock:
            return [self.view(record, document) for (deployment, _), record in self.records.items() if deployment == document["deployment"]]


registry = AnnouncementRegistry()
