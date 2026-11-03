# Configuración y secretos

Ningún secreto vive en el repositorio. `scripts/generar_secretos.py` (o
`python3 scripts/nx.py <entorno> secretos`) crea una carpeta nueva, con modo
`0700` y archivos `0600`, **fuera** del proyecto:

| Entorno | Carpeta por defecto | Variable |
|---|---|---|
| demo | `~/.config/nexustix/demo-secrets` | `NEXUSTIX_SECRETS_DIR` |
| pruebas | `~/.config/nexustix-auth-staging-secrets` | `NEXUSTIX_STAGING_SECRETS_DIR` |

| Archivo | Uso | Lo monta |
|---|---|---|
| `jwt-private.pem` | Firma RS256 de tokens de acceso (3072 bits) | solo Usuarios |
| `jwt-public.pem` | Verificación de tokens | Usuarios, Eventos, Compras |
| `eventos-service-token` | Credencial interna Compras→Eventos | Eventos, Compras |
| `<base>-db-password` | Contraseña del rol `postgres` de cada base | su PostgreSQL |
| `<base>-database-url` | URL de un nodo | su API |
| `<base>-database-url-ha` | URL con primario y standby + `target_session_attrs=read-write` | su API (overlay de replicación) |
| `replication-password` | Rol `replicator` de la replicación física | PostgreSQL (overlay) |
| `admin-password` | Contraseña inicial del administrador (`nx.py <entorno> admin`) | Usuarios |
| `frontend-config.js` | URLs públicas de las APIs (no es secreto; modo 0644) | frontend |

Reglas:

- El script nunca sobrescribe una carpeta existente ni reutiliza la del otro
  entorno. Para rotar secretos, genere una carpeta nueva y apunte la variable.
- Carpeta de una instalación anterior (por ejemplo el staging previo, que no
  tenía `*-database-url-ha`, `replication-password` ni `admin-password`):
  `python3 scripts/nx.py <entorno> secretos --completar` añade solo lo que
  falta, reutiliza las contraseñas de base existentes y no modifica nada.
- `POSTGRES_PASSWORD_FILE` solo se aplica al **inicializar** un volumen vacío.
  Si se regeneran secretos para volúmenes existentes, la contraseña de la base
  no cambia: use `--legacy-db-password` (volúmenes antiguos con `postgres`) o
  cambie la contraseña con `ALTER ROLE` antes de usar las nuevas URL.
- En Fedora con SELinux *Enforcing*, el script etiqueta la carpeta como
  `container_file_t`; sin esa etiqueta Docker no puede leer los secretos.
- No ejecute `docker compose config` sin `--quiet`: imprime la configuración
  resuelta.

`demo.env.example` y `pruebas.env.example` documentan las variables no
secretas que admite cada entorno.

## Compatibilidad sin credenciales incrustadas

`--legacy-db-password` ya no contiene una contraseña fija: exige
`NEXUSTIX_LEGACY_DB_PASSWORD_FILE`, con la ruta a un archivo privado existente
fuera de Git. No cambia la contraseña de ninguna base. No usar esa opción
para la LAN nueva, que genera claves aleatorias. Las API exigen
`DATABASE_URL_FILE` o `DATABASE_URL`, sin fallback.
