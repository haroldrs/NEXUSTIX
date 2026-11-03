"""Estado real del nodo PostgreSQL conectado; acceso administrativo."""
from fastapi import Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from fastapi.responses import JSONResponse


def install(app, get_db, require_admin):
    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        return JSONResponse(status_code=503, content={"detail": "Persistencia temporalmente no disponible; consulte o reintente con la misma clave."}, headers={"Retry-After": "2"})

    @app.get("/diagnostico/db")
    def database(db=Depends(get_db), _admin=Depends(require_admin)):
        if db.get_bind().dialect.name != "postgresql":
            return {"disponible": False, "motivo": "Requiere PostgreSQL"}
        try:
            standby = db.execute(text("SELECT pg_is_in_recovery()")).scalar_one()
            replication = [] if standby else [dict(row) for row in db.execute(text(
                "SELECT application_name, state, sync_state, "
                "pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint AS lag_bytes "
                "FROM pg_stat_replication")).mappings()]
            archive = dict(db.execute(text("SELECT archived_count, failed_count, last_archived_time::text, "
                "last_failed_time::text FROM pg_stat_archiver")).mappings().one())
            return {"disponible": True, "rol": "standby" if standby else "primario",
                "replicas": replication, "archivado": archive,
                "alcance": "nodo conectado; una lista vacía no acredita redundancia"}
        except SQLAlchemyError:
            db.rollback()
            raise HTTPException(503, "Diagnóstico PostgreSQL no disponible")
