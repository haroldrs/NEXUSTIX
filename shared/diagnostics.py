"""Estado real del nodo PostgreSQL conectado; acceso administrativo."""
from datetime import datetime, timezone
import os
import re

from fastapi import Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from fastapi.responses import JSONResponse


def install(app, get_db, require_admin):
    from shared.cluster_observer import install as install_cluster, require_observer_admin
    install_cluster(app)

    @app.get("/diagnostico/instancia")
    def instance(_admin=Depends(require_observer_admin)):
        # Metadatos de proceso, sin conexión SQL ni lectura de secretos/host.
        def label(name, maximum=96):
            value = os.getenv(name, "")
            return value if len(value) <= maximum and re.fullmatch(r"[A-Za-z0-9_.:+-]+", value) else None
        return {"instancia": label("INSTANCE_ID"), "servicio": label("NEXUSTIX_SERVICE"),
            "node_id": label("NEXUSTIX_NODE_ID"), "deployment_id": label("NEXUSTIX_DEPLOYMENT_ID"),
            "release": label("NEXUSTIX_RELEASE"), "inventory_sha256": label("NEXUSTIX_INVENTORY_SHA256"),
            "comprobado_en": datetime.now(timezone.utc).isoformat(),
            "alcance": "Metadatos declarados al iniciar esta réplica; no certifican imagen ni identidad física."}
    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        return JSONResponse(status_code=503, content={"detail": "Persistencia temporalmente no disponible; consulte o reintente con la misma clave."}, headers={"Retry-After": "2"})

    @app.get("/diagnostico/metricas")
    def metrics(_admin=Depends(require_admin)):
        snapshot = getattr(app.state, "http_metrics_snapshot", None)
        return snapshot() if snapshot else {"disponible": False, "motivo": "Sin instrumentación en este proceso"}

    @app.get("/diagnostico/db")
    def database(db=Depends(get_db), _admin=Depends(require_admin)):
        if db.get_bind().dialect.name != "postgresql":
            return {"disponible": False, "motivo": "Requiere PostgreSQL"}
        try:
            standby = db.execute(text("SELECT pg_is_in_recovery()")).scalar_one()
            version = int(db.execute(text("SELECT current_setting('server_version_num')")).scalar_one())
            missing = []

            def read(name, query, *, many=False):
                # Cada consulta opcional conserva las anteriores si el rol carece
                # de permiso o esta versión de PostgreSQL no ofrece una vista.
                try:
                    with db.begin_nested():
                        rows = db.execute(text(query)).mappings()
                        return [dict(row) for row in rows] if many else dict(rows.one())
                except SQLAlchemyError:
                    missing.append(name)
                    return None

            replication = None if standby else read("replicacion", "SELECT application_name, state, sync_state, "
                "sent_lsn::text, write_lsn::text, flush_lsn::text, replay_lsn::text, "
                "pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint AS lag_bytes, "
                "EXTRACT(EPOCH FROM replay_lag) AS replay_lag_seconds FROM pg_stat_replication", many=True)
            receiver = read("receptor_wal", "SELECT status, written_lsn::text, flushed_lsn::text, "
                "latest_end_lsn::text, last_msg_receipt_time::text FROM pg_stat_wal_receiver", many=True) if standby else None
            wal = read("wal", "SELECT pg_last_wal_receive_lsn()::text AS recibido_lsn, "
                "pg_last_wal_replay_lsn()::text AS aplicado_lsn, pg_last_xact_replay_timestamp()::text AS ultimo_replay, "
                "pg_is_wal_replay_paused() AS replay_pausado" if standby else
                "SELECT pg_current_wal_lsn()::text AS actual_lsn, pg_current_wal_flush_lsn()::text AS persistido_lsn")
            archive = read("archivado", "SELECT archived_count, failed_count, last_archived_time::text, "
                "last_failed_time::text FROM pg_stat_archiver")
            checkpoints = read("checkpoints", "SELECT num_timed, num_requested, write_time, sync_time, "
                "buffers_written, stats_reset::text FROM pg_stat_checkpointer" if version >= 170000 else
                "SELECT checkpoints_timed AS num_timed, checkpoints_req AS num_requested, "
                "checkpoint_write_time AS write_time, checkpoint_sync_time AS sync_time, "
                "buffers_checkpoint AS buffers_written, stats_reset::text FROM pg_stat_bgwriter")
            control = read("ultimo_checkpoint", "SELECT checkpoint_lsn::text, redo_lsn::text, "
                "timeline_id, checkpoint_time::text FROM pg_control_checkpoint()")
            connections = read("conexiones", "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE state = 'active') AS activas, "
                "current_setting('max_connections')::int AS maximas FROM pg_stat_activity")
            durability = read("durabilidad", "SELECT current_setting('fsync') AS fsync, "
                "current_setting('full_page_writes') AS full_page_writes, "
                "current_setting('synchronous_commit') AS synchronous_commit, "
                "current_setting('wal_level') AS wal_level, "
                "current_setting('synchronous_standby_names') AS synchronous_standby_names, "
                "current_setting('transaction_read_only') AS transaction_read_only")
            identity = read("identidad", "SELECT current_setting('cluster_name') AS identidad_nodo, "
                "inet_server_addr()::text AS direccion, inet_server_port() AS puerto")
            system = read("sistema", "SELECT system_identifier::text AS identificador_sistema FROM pg_control_system()")
            return {"disponible": True, "rol": "standby" if standby else "primario",
                "comprobado_en": datetime.now(timezone.utc).isoformat(), "version": version,
                "en_recuperacion": standby, "replicas": replication, "receptor_wal": receiver,
                "archivado": archive, "wal": wal, "checkpoints": checkpoints,
                "ultimo_checkpoint": control, "conexiones": connections, "durabilidad": durability,
                "identidad": identity, "sistema": system,
                "ultimo_failover": None, "consultas_no_disponibles": missing,
                "estado": "parcial" if missing else "disponible",
                "alcance": "nodo conectado; una lista vacía no acredita redundancia ni failover",
                "limitaciones": ["No se consultan credenciales ni cadenas de conexión.",
                    "La ausencia de nuevos WAL no mide retraso cuando no hay actividad.",
                    "Un cambio de rol o timeline por sí solo no demuestra un failover seguro.",
                    "Checkpoints y WAL no sustituyen backups ni pruebas de recuperación."]}
        except SQLAlchemyError:
            db.rollback()
            raise HTTPException(503, "Diagnóstico PostgreSQL no disponible")
