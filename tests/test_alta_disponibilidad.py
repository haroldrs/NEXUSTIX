"""Modo de alta disponibilidad automática: inventario, render y observador.

La prueba con contenedores reales (caída de la PC principal, partición) está en
tests/ha/laboratorio_patroni.py porque requiere Docker.
"""
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "despliegue-nodos"))

from inventory import InventoryError, load_inventory, topology_document, validate_inventory  # noqa: E402
from nodos import database_url  # noqa: E402
from render import coordination_peers, etcd_cluster, patroni_config, render_node  # noqa: E402

EXAMPLE = ROOT / "despliegue-nodos/examples/6-dos-pc-arbitro.yml"


def inventory():
    return load_inventory(EXAMPLE)


def by_id(inv, node_id):
    return next(node for node in inv["nodes"] if node["id"] == node_id)


def test_example_two_pcs_and_arbiter_is_valid_with_tailscale_addresses():
    inv = inventory()
    assert by_id(inv, "pc1")["address"].startswith("100.")
    assert inv["ports"]["etcd_client"] == 2379 and inv["ports"]["compras_patroni"] == 8010
    document = topology_document(inv)
    assert document["automatic_failover"] is True
    assert document["coordination"] == "patroni_etcd_quorum"
    arbiter = next(node for node in document["nodes"] if node["id"] == "arbitro")
    assert arbiter["voter"] and arbiter["arbiter"]


def test_existing_inventories_keep_their_fingerprint_without_ha_ports():
    inv = load_inventory(ROOT / "despliegue-nodos/examples/3-espejo.yml")
    assert "etcd_client" not in inv["ports"]


def test_node_without_roles_must_be_a_voter():
    data = yaml.safe_load(EXAMPLE.read_text())
    data["coordination"]["voters"] = ["pc1", "pc2", "arbitro"]
    data["nodes"].append({"id": "suelto", "address": "100.101.1.14", "platform": "linux", "failure_domain": "otra",
                          "resources": {"cpus": 1, "memory_mb": 512}, "roles": []})
    with pytest.raises(InventoryError):
        validate_inventory(data)


def test_voters_need_independent_failure_domains_and_odd_count():
    data = yaml.safe_load(EXAMPLE.read_text())
    data["nodes"][2]["failure_domain"] = data["nodes"][0]["failure_domain"]
    with pytest.raises(InventoryError, match="dominios físicos"):
        validate_inventory(data)
    data = yaml.safe_load(EXAMPLE.read_text())
    data["coordination"]["voters"] = ["pc1", "pc2"]
    data["nodes"] = data["nodes"][:2]
    with pytest.raises(InventoryError, match="3 o 5"):
        validate_inventory(data)


def test_public_addresses_are_rejected():
    data = yaml.safe_load(EXAMPLE.read_text())
    data["nodes"][0]["address"] = "8.8.8.8"
    with pytest.raises(InventoryError):
        validate_inventory(data)


def test_arbiter_renders_only_etcd_and_data_nodes_render_patroni():
    inv = inventory()
    arbiter = render_node(inv, by_id(inv, "arbitro"), "/src")
    assert list(arbiter["compose"]["services"]) == ["etcd"]
    pc2 = render_node(inv, by_id(inv, "pc2"), "/src")
    services = pc2["compose"]["services"]
    assert {"etcd", "usuarios_db", "eventos_db", "compras_db"} <= set(services)
    db = services["eventos_db"]
    assert db["build"]["dockerfile"] == "despliegue-nodos/patroni/Dockerfile"
    assert db["environment"]["NEXUSTIX_PATRONI_PREFERRED"] == "1"          # eventos tiene su principal en pc2
    assert services["compras_db"]["environment"]["NEXUSTIX_PATRONI_PREFERRED"] == "0"
    assert "0.0.0.0:5434:5434" in services["compras_db"]["ports"]          # wsl2 escucha en todas
    assert "config/patroni-eventos.yml" in pc2["files"]


def test_etcd_cluster_lists_the_three_voters():
    cluster = etcd_cluster(inventory())
    assert cluster.count("=http://") == 3 and "arbitro=http://100.101.1.13:2380" in cluster


def test_patroni_config_failover_parameters_and_local_etcd_by_dns():
    inv = inventory()
    config = patroni_config(inv, by_id(inv, "pc1"), "compras")
    assert config["etcd3"]["hosts"][0] == "etcd:2379"                       # votante local por DNS Docker
    assert "100.101.1.12:2379" in config["etcd3"]["hosts"]
    dcs = config["bootstrap"]["dcs"]
    assert dcs["ttl"] == 30 and dcs["synchronous_mode"] is True and dcs["synchronous_mode_strict"] is False
    assert dcs["postgresql"]["use_pg_rewind"] is True
    assert config["postgresql"]["connect_address"] == "100.101.1.11:5434"
    assert config["restapi"]["connect_address"] == "100.101.1.11:8010"
    assert "data-checksums" in config["bootstrap"]["initdb"]
    assert any(rule.startswith("host replication replicator 100.101.1.12/32") for rule in config["postgresql"]["pg_hba"])


def test_api_receives_peers_coordination_endpoints_and_gateways():
    inv = inventory()
    rendered = render_node(inv, by_id(inv, "pc1"), "/src")
    env = rendered["compose"]["services"]["compras"]["environment"]
    peers = json.loads(env["NEXUSTIX_PEERS"])
    assert {p["id"] for p in peers} == {f"{d}_{n}" for d in ("usuarios", "eventos", "compras") for n in ("pc1", "pc2")}
    local = next(p for p in peers if p["id"] == "compras_pc1")
    assert local["url"] == "http://compras:8003"
    assert max(peers, key=lambda p: p["prioridad"])["nodo"] == "pc1"         # primer nodo = coordinador preferido
    endpoints = json.loads(env["NEXUSTIX_COORDINATION_ENDPOINTS"])
    assert endpoints["compras"][0] == "http://compras_db:8010"
    assert 'GATEWAYS: ["100.101.1.11", "100.101.1.12"]' in rendered["files"]["config/frontend-config.js"]


def test_database_url_uses_published_port_locally_in_patroni_mode():
    inv = inventory()
    url = database_url(inv, by_id(inv, "pc1"), "compras", "secreto")
    assert "host=compras_db:5434" in url and "host=100.101.1.12:5434" in url
    assert "target_session_attrs=read-write" in url


def test_coordination_peers_ignore_nodes_without_apis():
    inv = inventory()
    assert all(p["nodo"] != "arbitro" for p in coordination_peers(inv, by_id(inv, "pc1")))


def test_observer_reports_automatic_failover_with_leaders(monkeypatch):
    from shared import cluster_observer
    document = topology_document(inventory())
    observations = [{"base": base, "estado": "observado", "lider": f"nx_{base}_pc1", "miembros": []}
                    for base in ("usuarios", "eventos", "compras")]
    monkeypatch.setattr(cluster_observer, "coordination_observations", lambda: observations)
    result = cluster_observer.coordinator(document)
    assert result["modo"] == "patroni" and result["promocion_automatica"] is True
    assert result["ha_operativa"] is True and result["mayoria_requerida"] == 2
    observations[1]["lider"] = None
    assert cluster_observer.coordinator(document)["ha_operativa"] is False
