#!/usr/bin/env python3
"""Prepara una entrega por inventario. No invoca Docker, SSH ni bases de datos.

Cada entrega usa un directorio NUEVO fuera del repositorio. --previous conserva
la identidad y las credenciales, y compara cambios en las copias PostgreSQL.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import quote
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from generar_secretos import generate_rsa_pair, write_secret
from inventory import DOMAINS, InventoryError, inventory_fingerprint, load_inventory, stable_access, topology_document
from render import render_node

MASTER_FILES = {
    "jwt-private.pem", "jwt-public.pem", "eventos-service-token", "admin-password",
    *(f"{domain}-{suffix}" for domain in DOMAINS
      for suffix in ("db-password", "admin-password", "replication-password")),
}


def private_file(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise InventoryError(f"Archivo privado regular con permisos 0600 requerido: {path.name}")
    if path.stat().st_size > 32768:
        raise InventoryError("Archivo de credencial demasiado grande")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise InventoryError("Archivo de credencial vacío")
    return value


def private_directory(path):
    if path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o077:
        raise InventoryError("Directorio privado 0700 requerido; en WSL use el filesystem de Ubuntu")


def database_url(inventory, node, domain, password):
    database = inventory["databases"][domain]
    nodes = {entry["id"]: entry for entry in inventory["nodes"]}
    candidates = []
    for member in [database["primary"], *database["standbys"]]:
        # El acceso local por DNS Docker evita depender del hairpin de Windows.
        local_port = inventory["ports"][domain + "_db"] if inventory.get("coordination", {}).get("mode") == "patroni" else 5432
        address = (f"{domain}_db:{local_port}" if member == node["id"] else
                   f"{nodes[member]['address']}:{inventory['ports'][domain + '_db']}")
        candidates.append("host=" + address)
    return (f"postgresql://nexustix_app:{quote(password, safe='')}@/{domain}_db?"
            + "&".join(candidates) + "&target_session_attrs=read-write&connect_timeout=3")


def compare_inventories(previous, current, acknowledged):
    if previous["deployment"] != current["deployment"]:
        raise InventoryError("--previous pertenece a otro deployment; no compartir credenciales entre instalaciones")
    unknown = set(acknowledged) - set(DOMAINS)
    if unknown:
        raise InventoryError("Reconocimiento de transición con base lógica desconocida")
    changes = []
    old_nodes = {node["id"]: node for node in previous["nodes"]}
    new_nodes = {node["id"]: node for node in current["nodes"]}
    for domain in DOMAINS:
        old, new = previous["databases"][domain], current["databases"][domain]
        old_members = {old["primary"], *old["standbys"]}
        new_members = {new["primary"], *new["standbys"]}
        dangerous = []
        if old["primary"] != new["primary"]:
            if new["primary"] not in old["standbys"]:
                raise InventoryError(f"{domain}: el nuevo primario debe ser un standby conocido; no se puede inicializar otro primario vacío")
            dangerous.append("cambio de primario tras promoción manual")
            if old["primary"] in new_members and new["generations"][old["primary"]] <= old["generations"][old["primary"]]:
                raise InventoryError(f"{domain}: el primario anterior requiere una generación NUEVA para reintegrarlo como standby")
        if new["primary"] in old_members and new["generations"][new["primary"]] != old["generations"][new["primary"]]:
            raise InventoryError(f"{domain}: no se permite cambiar el volumen del primario elegido; conservar su generación y sus datos")
        for member in old_members & new_members:
            before, after = old["generations"][member], new["generations"][member]
            if after < before:
                raise InventoryError(f"{domain}/{member}: no reutilizar generaciones antiguas")
            if after > before:
                dangerous.append(f"reclonación de {member} en generación {after}; conservar volumen {before}")
            if old_nodes[member]["address"] != new_nodes[member]["address"]:
                dangerous.append(f"cambio de dirección de {member}; confirmar identidad física")
        if old_members - new_members:
            dangerous.append("retirada de copias: " + ", ".join(sorted(old_members - new_members)))
        if old["replication"] != new["replication"] or old["sync_quorum"] != new["sync_quorum"]:
            dangerous.append("cambio de política de durabilidad / quórum WAL")
        if dangerous and domain not in acknowledged:
            raise InventoryError(f"{domain}: {'; '.join(dangerous)}. Completar RECUPERACION.md y reconocer con --acknowledge-db-transition {domain}; el generador no hace esa operación")
        changes.extend(f"{domain}: {change}" for change in dangerous)
        added = new_members - old_members
        if added:
            changes.append(f"{domain}: nuevas copias {', '.join(sorted(added))}; preparar slots y accesos antes de arrancarlas")
    for node_id in sorted(set(new_nodes) - set(old_nodes)):
        changes.append(f"Añadir nodo {node_id}")
    for node_id in sorted(set(old_nodes) - set(new_nodes)):
        changes.append(f"Retirar nodo {node_id} de rutas; su contenedor y volúmenes NO se eliminan")
    for node_id in sorted(set(old_nodes) & set(new_nodes)):
        old_roles, new_roles = set(old_nodes[node_id]["roles"]), set(new_nodes[node_id]["roles"])
        if old_roles != new_roles:
            changes.append(f"{node_id}: aplicaciones añadidas {', '.join(sorted(new_roles - old_roles)) or 'ninguna'}; retiradas {', '.join(sorted(old_roles - new_roles)) or 'ninguna'}. Compose no reubica ni detiene las retiradas automáticamente")
        if old_nodes[node_id].get("identity_fingerprint") != new_nodes[node_id].get("identity_fingerprint"):
            changes.append(f"{node_id}: cambió la identidad declarada; verificar la clave pública por un canal independiente antes de distribuir")
    if previous["access"] != current["access"]:
        changes.append("Cambió el acceso estable declarado; actualizar y comprobar DNS/proxy externo de forma coordinada")
    if previous["release"] != current["release"]:
        changes.append(f"Etiqueta de versión declarada: {current['release']}; comprobar y distribuir el código correspondiente en todas las PC")
    if previous["profile"] != current["profile"]:
        changes.append(f"Perfil declarado: {previous['profile']} → {current['profile']}; conservar la identidad y aplicar los cambios de topología indicados")
    return changes


def prepare(inventory_path, destination, source_root, previous_path=None, acknowledged=(), offline_images=False):
    inventory, inventory_source = load_inventory(inventory_path, with_source=True)
    source_root = Path(source_root).expanduser().resolve()
    for filename in ("usuarios/Dockerfile", "eventos/Dockerfile", "compras/Dockerfile", "frontend/Dockerfile", "despliegue-nodos/postgres/Dockerfile"):
        if not (source_root / filename).is_file():
            raise InventoryError("--source-root debe apuntar al repositorio actualizado en cada PC")
    destination = Path(destination).expanduser().resolve()
    if destination.exists() or destination.is_relative_to(ROOT) or destination.is_relative_to(source_root):
        raise InventoryError("Use un destino NUEVO, fuera del repositorio y del contexto de construcción")
    previous, previous_master, identity, changes = None, None, uuid4().hex, []
    generation_history = {domain: {} for domain in DOMAINS}
    if previous_path:
        previous = Path(previous_path).expanduser().resolve()
        if destination.is_relative_to(previous):
            raise InventoryError("Use un destino NUEVO separado de --previous; no mezclar entregas dentro de una instalación anterior")
        private_directory(previous)
        if (previous / "INCOMPLETE").exists():
            raise InventoryError("La entrega anterior está incompleta; no se usa como origen")
        before = load_inventory(previous / "inventory.yml")
        metadata = json.loads(private_file(previous / "installation.json"))
        stored_identity = metadata.get("installation_id")
        if (metadata.get("deployment") != inventory["deployment"] or not isinstance(stored_identity, str)
                or len(stored_identity) != 32 or any(char not in "0123456789abcdef" for char in stored_identity)):
            raise InventoryError("Identidad de instalación anterior no válida")
        if metadata.get("inventory_sha256") != inventory_fingerprint(before):
            raise InventoryError("El inventario de --previous no coincide con su installation.json; use una entrega íntegra y pase los cambios como inventario nuevo")
        if metadata.get("profile", before["profile"]) != before["profile"]:
            raise InventoryError("El perfil de --previous no coincide con su installation.json; no mezclar archivos de entregas distintas")
        identity = metadata["installation_id"]
        changes = compare_inventories(before, inventory, acknowledged)
        generation_history = metadata.get("generation_history", {
            domain: dict(before["databases"][domain]["generations"]) for domain in DOMAINS})
        for domain in DOMAINS:
            old_members = before["databases"][domain]["generations"]
            for member, generation in inventory["databases"][domain]["generations"].items():
                highest = generation_history[domain].get(member, 0)
                if member not in old_members and generation <= highest:
                    raise InventoryError(f"{domain}/{member}: nodo retirado anteriormente; use una generación mayor que {highest}, sin reutilizar sus volúmenes")
        previous_master = previous / "master"
        private_directory(previous_master)
        for name in MASTER_FILES:
            private_file(previous_master / name)
    elif acknowledged:
        raise InventoryError("--acknowledge-db-transition requiere --previous")
    for domain in DOMAINS:
        generation_history[domain].update(inventory["databases"][domain]["generations"])
    # Renderizar todo antes de crear archivos; no consulta la infraestructura.
    outputs = [(node, render_node(inventory, node, source_root, installation_id=identity)) for node in inventory["nodes"]]
    previous_umask = os.umask(0o077)
    try:
        destination.mkdir(parents=True, mode=0o700)
        write_secret(destination / "INCOMPLETE", "Preparación incompleta: no desplegar esta entrega.\n")
        master = destination / "master"
        master.mkdir(mode=0o700)
        if previous_master:
            for name in sorted(MASTER_FILES):
                write_secret(master / name, private_file(previous_master / name) + "\n")
        else:
            generate_rsa_pair(master / "jwt-private.pem", master / "jwt-public.pem")
            for name in sorted(MASTER_FILES - {"jwt-private.pem", "jwt-public.pem"}):
                write_secret(master / name, secrets.token_urlsafe(40) + "\n")
        # Conservar roles manuales originales evita que quitar mirror_nodes de
        # una entrega futura deje como manuales los roles ya expandidos.
        write_secret(destination / "inventory-source.yml", inventory_source)
        write_secret(destination / "inventory.yml", yaml.safe_dump(inventory, sort_keys=False))
        metadata = {"version": 1, "deployment": inventory["deployment"], "installation_id": identity,
                    "inventory_sha256": inventory_fingerprint(inventory), "release": inventory["release"],
                    "profile": inventory["profile"],
                    "db_transitions_acknowledged": sorted(set(acknowledged)), "changes": changes,
                    "generation_history": generation_history,
                    "note": "Preparación de archivos; no acredita fencing, replicación ni despliegue"}
        write_secret(destination / "installation.json", json.dumps(metadata, indent=2) + "\n")
        for node, rendered in outputs:
            folder = destination / node["id"]
            folder.mkdir(mode=0o700)
            (folder / "secrets").mkdir(mode=0o700)
            compose = rendered["compose"]
            # Guía declarativa; no es un script y no inicia nada.
            launch_order = {"coordination": [], "primary": [], "standby": [], "applications": []}
            for name, specification in compose["services"].items():
                role = specification.get("environment", {}).get("NEXUSTIX_PG_ROLE")
                if role:
                    specification["environment"]["NEXUSTIX_PG_ALLOW_INITDB"] = "0" if previous else "1"
                    specification["environment"]["NEXUSTIX_INSTALLATION_ID"] = identity
                launch_order["coordination" if name == "etcd" else role if role in {"primary", "standby"} else "applications"].append(name)
            write_secret(folder / "launch-order.json", json.dumps(launch_order, indent=2) + "\n", mode=0o644)
            for service in compose["services"].values():
                service.setdefault("labels", {})["org.nexustix.installation"] = identity
                if offline_images and "build" in service:
                    # Imágenes cargadas desde scripts/paquete_offline.py: sin construir ni descargar.
                    service["image"] = f"nexustix/{service['image'].rsplit('/', 1)[-1].split(':')[0]}:offline"
                    del service["build"]
            for resource in ("volumes", "networks"):
                for spec in compose.get(resource, {}).values():
                    spec.setdefault("labels", {})["org.nexustix.installation"] = identity
            for relative, content in rendered["files"].items():
                target = folder / relative
                if Path(relative).is_absolute() or not target.resolve().is_relative_to(folder):
                    raise InventoryError("Ruta de artefacto fuera del nodo")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                write_secret(target, content, mode=0o644)
            for specification in compose.get("secrets", {}).values():
                target = folder / specification["file"]
                if not target.resolve().is_relative_to(folder / "secrets"):
                    raise InventoryError("El manifiesto referencia un secreto fuera del nodo")
                name = target.name
                domain = name.removesuffix("-database-url")
                if name.endswith("-database-url") and domain in DOMAINS:
                    value = database_url(inventory, node, domain, private_file(master / f"{domain}-db-password"))
                elif name in MASTER_FILES:
                    value = private_file(master / name)
                else:
                    raise InventoryError("El manifiesto solicita un secreto desconocido")
                write_secret(target, value + "\n")
            write_secret(folder / "compose.yml", yaml.safe_dump(compose, sort_keys=False))
        write_secret(destination / "access-points.json", json.dumps(topology_document(inventory)["access_points"], indent=2) + "\n", mode=0o644)
        write_secret(destination / "stable-access.json", json.dumps(stable_access(inventory), indent=2) + "\n", mode=0o644)
        (destination / "INCOMPLETE").unlink()  # Solo este marcador creado en esta ejecución.
    finally:
        os.umask(previous_umask)
    print(f"Entrega preparada: {destination}")
    print("Copiar únicamente la carpeta del nodo asignado; conservar master en el equipo administrador.")
    print("No se han ejecutado Docker, migraciones, consultas de red ni cambios de PostgreSQL.")
    for change in changes:
        print("Cambio pendiente de aplicar: " + change)
    for access in topology_document(inventory)["access_points"]:
        print(f"Acceso {'preferido' if access['preferred'] else 'alternativo'}: {access['url']}")
    stable = stable_access(inventory)
    if stable.get("url"):
        print(f"Acceso estable PREPARADO: {stable['url']}; requiere infraestructura externa redundante aún no comprobada")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("preparar", help="Solo genera archivos y secretos fuera del repositorio")
    prepare_parser.add_argument("inventory", type=Path)
    prepare_parser.add_argument("destination", type=Path)
    prepare_parser.add_argument("--source-root", type=Path, default=ROOT, help="Repositorio fuente disponible al preparar; cada PC define después NEXUSTIX_SOURCE_ROOT con su propia ruta")
    prepare_parser.add_argument("--previous", type=Path, help="Entrega anterior: conservar credenciales e identidad")
    prepare_parser.add_argument("--acknowledge-db-transition", action="append", choices=DOMAINS, default=[],
                                help="Reconoce procedimiento manual ya resuelto; NO promueve, aísla ni verifica el primario")
    prepare_parser.add_argument("--imagenes-offline", action="store_true",
                                help="Usar imágenes nexustix/*:offline cargadas con scripts/paquete_offline.py")
    arguments = parser.parse_args()
    try:
        prepare(arguments.inventory, arguments.destination, arguments.source_root,
                arguments.previous, arguments.acknowledge_db_transition, arguments.imagenes_offline)
    except (InventoryError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        # No imprimir trazas, contenidos de archivos privados ni comandos con claves.
        if isinstance(exc, InventoryError):
            parser.exit(2, f"ERROR: {exc}\n")
        parser.exit(2, "ERROR: preparación incompleta; revisar permisos, dependencias y archivos de entrada. No desplegar una entrega con marcador INCOMPLETE.\n")


if __name__ == "__main__":
    main()
