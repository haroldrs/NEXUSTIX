"""Inventario de Eventos: stock inicial persistido, compensación acotada y seed protegido.

SQLite temporal aislado; las funciones de los endpoints se invocan directamente.
"""
import importlib
import sys

import pytest
from fastapi import HTTPException

from test_authentication import import_service


@pytest.fixture
def eventos(tmp_path):
    app, database, sessions, engine = import_service("eventos", tmp_path)
    main = importlib.import_module("main")
    models = importlib.import_module("models")
    with sessions() as db:
        db.add_all([
            models.Evento(id=1, nombre="Base", stock=500, stock_inicial=None),
            models.Evento(id=900001, nombre="Sintético", stock=3, stock_inicial=3),
        ])
        db.commit()
    yield main, models, sessions, engine
    app.dependency_overrides.clear()
    engine.dispose()


def test_audit_uses_persisted_initial_stock_and_falls_back_to_catalog(eventos):
    main, _, sessions, _ = eventos
    with sessions() as db:
        rows = {row["id"]: row for row in main.auditoria_eventos(db, None)}
    assert rows[900001]["stock_inicial"] == 3, "evento sintético: columna stock_inicial"
    assert rows[1]["stock_inicial"] == 500, "evento antiguo sin columna: catálogo base"


def test_reserve_confirm_and_release_keep_inventory_equation(eventos):
    main, models, sessions, _ = eventos
    with sessions() as db:
        main.reservar_entradas(900001, 2, 0, "op-a", db, None)
        main.confirmar_reserva(900001, "op-a", 0, db, None)
        main.reservar_entradas(900001, 1, 0, "op-b", db, None)
        with pytest.raises(HTTPException) as sold_out:
            main.reservar_entradas(900001, 1, 0, "op-c", db, None)
        assert sold_out.value.status_code == 409
        main.liberar_entradas(900001, 1, 0, "op-b", db, None)
        # liberar dos veces la misma operación no infla el stock
        again = main.liberar_entradas(900001, 1, 0, "op-b", db, None)
        assert again["idempotente"] is True
        evento = db.get(models.Evento, 900001)
        confirmed = sum(r.cantidad for r in db.query(models.ReservaStock).filter_by(estado="confirmada"))
        assert evento.stock == 1 and evento.stock + confirmed == evento.stock_inicial


def test_release_cannot_exceed_persisted_initial_stock(eventos):
    main, models, sessions, _ = eventos
    with sessions() as db:
        main.reservar_entradas(900001, 1, 0, "op-cap", db, None)
        evento = db.get(models.Evento, 900001)
        evento.stock = 3  # inventario manipulado fuera del protocolo
        db.commit()
        with pytest.raises(HTTPException) as capped:
            main.liberar_entradas(900001, 1, 0, "op-cap", db, None)
        assert capped.value.status_code == 409


def test_seed_refuses_to_reset_stock_with_active_reservations(eventos, monkeypatch, capsys):
    main, models, sessions, engine = eventos
    sys.modules.pop("seed", None)
    seed = importlib.import_module("seed")
    monkeypatch.setattr(seed, "SessionLocal", sessions)
    monkeypatch.setattr(seed, "engine", engine)
    with sessions() as db:
        main.reservar_entradas(900001, 1, 0, "op-seed", db, None)
    assert seed.seed_eventos() == 1
    assert "Se rechaza el seed" in capsys.readouterr().err
    with sessions() as db:
        assert db.get(models.Evento, 900001).stock == 2, "el stock no se tocó"
        main.liberar_entradas(900001, 1, 0, "op-seed", db, None)
    assert seed.seed_eventos() == 0
    with sessions() as db:
        base = db.get(models.Evento, 1)
        assert base.stock == 500 and base.stock_inicial == 500
