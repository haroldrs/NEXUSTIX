# Plantilla INACTIVA de coordinación (Patroni + etcd)

Nada de esta carpeta se genera, se despliega ni se referencia desde `render.py`,
`nodos.py` o los Compose existentes. Los archivos `*.example.yml` son un punto de
partida para un entorno aislado y futuro. Las promociones siguen siendo manuales
([RECUPERACION.md](../RECUPERACION.md)) y los requisitos están en
[COORDINACION.md](../COORDINACION.md).

No utilizar mientras falte **cualquiera** de estas condiciones: votantes en tres o
cinco dominios físicos independientes, mTLS, fencing verificable para cada candidato
(el watchdog dentro de WSL o Docker Desktop no controla toda la PC), acceso
redundante y un ensayo de recuperación registrado.

Para declarar el plan en un inventario, añade este fragmento. Solo alimenta el
Centro de Control; no instala nada ni autoriza promociones. `prepared_inactive` es
el único modo admitido.

```yaml
coordination:
  mode: prepared_inactive
  voters: [pc1, pc2, pc3]  # 3 o 5 nodos, cada uno en un failure_domain distinto.
```

Con un clúster real, y solo entonces, `NEXUSTIX_COORDINATION_ENDPOINTS` puede
indicar en el servidor la API REST de lectura de Patroni por base, por ejemplo
`{"compras":"https://patroni-compras.interna:8008"}`. El panel solo consulta
`/cluster`; nunca autoriza promociones.
