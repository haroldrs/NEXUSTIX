"""Carga y restablece los datos base del servicio de Eventos.

Uso dentro del contenedor:
    python seed.py

Conserva los IDs existentes, actualiza nombre, stock y stock_inicial de los
eventos base y crea los que todavía no existen. No elimina compras ni recrea
la base.

Seguridad: restablecer el stock mientras existan reservas pendientes o
confirmadas rompería la ecuación stock_inicial == stock + vendidas y dejaría
compras sin inventario correlacionado. Por eso el script se niega a ejecutarse
en ese caso; para empezar de cero en el entorno de pruebas use
``python3 scripts/nx.py pruebas reset-datos``, que limpia de forma coordinada
las tres bases.
"""

import sys

from database import Base, SessionLocal, engine
from models import Evento, ReservaStock


EVENTOS_PRUEBA = [
    {"id": 1, "nombre": "Concierto Rock en Trujillo", "stock": 500},
    {"id": 2, "nombre": "Festival de Jazz", "stock": 20},
    {"id": 3, "nombre": "Concierto Pop Lima", "stock": 300},
    {"id": 4, "nombre": "Festival Electrónico", "stock": 250},
    {"id": 5, "nombre": "Concierto Internacional", "stock": 400},
]


def seed_eventos() -> int:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        activas = db.query(ReservaStock).filter(ReservaStock.estado.in_(["reservada", "confirmada"])).count()
        if activas:
            print(
                f"Se rechaza el seed: existen {activas} reservas pendientes o confirmadas. "
                "Restablecer el stock dejaría el inventario inconsistente con las compras.",
                file=sys.stderr,
            )
            return 1
        for datos in EVENTOS_PRUEBA:
            evento = db.query(Evento).filter(Evento.id == datos["id"]).first()

            if evento is None:
                evento = Evento(
                    id=datos["id"],
                    nombre=datos["nombre"],
                    stock=datos["stock"],
                    stock_inicial=datos["stock"],
                )
                db.add(evento)
            else:
                evento.nombre = datos["nombre"]
                evento.stock = datos["stock"]
                evento.stock_inicial = datos["stock"]

        db.commit()
        print("Eventos base reinicializados correctamente.")
        for datos in EVENTOS_PRUEBA:
            print(f'  {datos["id"]}. {datos["nombre"]} -> stock={datos["stock"]}')
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(seed_eventos())
