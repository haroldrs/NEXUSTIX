#!/usr/bin/env python3
"""
Script de Demostración Práctica — Sistemas Operativos II
Plataforma Distribuida de Entradas para Conciertos

Permite ejecutar y verificar en vivo:
1. Verificación de salud y latencia (Heartbeats & Detección de Fallos).
2. Atomicidad Distribuida y Transacción Compensatoria (Patrón Saga).
3. Sincronización y Exclusión Mutua (SELECT FOR UPDATE sin sobreventa).
4. Tolerancia a Fallos y Failover con réplicas y Balanceador Nginx.
5. Elección de Líder (Algoritmo del Matón / Bully & Pérdida de Recursos).
6. Diálogo de Consenso Secuencial (GLOBAL_COMMIT vs GLOBAL_ABORT).
7. Reloj Lógico de Lamport (T) y Ordenación Causal.
"""

import sys
import time
import json
import requests
from concurrent.futures import ThreadPoolExecutor

BASE_USUARIOS = "http://localhost:8001"
BASE_EVENTOS = "http://localhost:8002"
BASE_COMPRAS_R1 = "http://localhost:8003"
BASE_COMPRAS_R2 = "http://localhost:8004"
BASE_COMPRAS_LB = "http://localhost:8005"

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"

def print_header(title):
    print(f"\n{BOLD}{CYAN}{'='*70}{RESET}")
    print(f"{BOLD}{CYAN}{title.center(70)}{RESET}")
    print(f"{BOLD}{CYAN}{'='*70}{RESET}\n")

def check_health():
    print_header("1. DETECCIÓN DE ESTADO Y HEARTBEATS (MONITOREO ACTIVO)")
    servicios = [
        ("Usuarios (API + BD)", f"{BASE_USUARIOS}/health"),
        ("Eventos (API + BD Stock)", f"{BASE_EVENTOS}/health"),
        ("Compras Réplica 1 (R1 :8003)", f"{BASE_COMPRAS_R1}/health"),
        ("Compras Réplica 2 (R2 :8004)", f"{BASE_COMPRAS_R2}/health"),
        ("Balanceador Nginx (LB :8005)", f"{BASE_COMPRAS_LB}/health"),
    ]

    for nombre, url in servicios:
        t0 = time.time()
        try:
            r = requests.get(url, timeout=3)
            latencia = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                data = r.json()
                db_info = f" | DB: {data.get('db', 'N/A')}" if 'db' in data else ""
                inst = f" | Instancia: {data.get('instancia', 'LB')}"
                lamport = f" | Lamport T={data.get('reloj_logico_T', 'N/A')}"
                lider = " 👑[LÍDER]" if data.get('lider') else ""
                print(f"  {GREEN}● [ACTIVO]{RESET} {nombre:<28} ({latencia:>3}ms){db_info}{inst}{lamport}{lider}")
            else:
                print(f"  {YELLOW}▲ [DEGRADADO]{RESET} {nombre:<28} (HTTP {r.status_code})")
        except Exception as e:
            print(f"  {RED}■ [CAÍDO/DOWN]{RESET} {nombre:<28} (Error: No responde)")

def demo_atomicidad():
    print_header("2. DEMOSTRACIÓN DE ATOMICIDAD DISTRIBUIDA (PATRÓN SAGA)")
    print(f"{BOLD}Concepto:{RESET} Si una compra reserva stock en Eventos pero falla al persistir")
    print(f"en la base de datos de Compras, el sistema debe ejecutar un {YELLOW}Rollback Compensatorio{RESET}")
    print(f"liberando el stock para evitar que se pierdan entradas (Inconsistencia).\n")

    try:
        r_stock = requests.get(f"{BASE_EVENTOS}/eventos/2/stock", timeout=3)
        stock_inicial = r_stock.json()["stock"]
        print(f"[*] Stock inicial de Festival de Jazz: {BOLD}{stock_inicial}{RESET} entradas")
    except Exception as e:
        print(f"{RED}[!] Error al consultar stock: {e}{RESET}")
        return

    print(f"\n--- Prueba A: Compra Normal Atómica (Exitosa) ---")
    try:
        r_compra = requests.post(
            f"{BASE_COMPRAS_LB}/compras",
            json={"usuario_id": 1, "evento_id": 2, "cantidad": 1, "simular_fallo": False},
            timeout=5
        )
        if r_compra.status_code == 200:
            data = r_compra.json()
            print(f"  {GREEN}✔ Compra completada exitosamente!{RESET}")
            print(f"    Atendido por: {data['atendido_por']}")
            print(f"    Stock remanente: {data['stock_restante']}")
            print(f"    Reloj Lamport final: T={data.get('reloj_logico_T')}")
        else:
            print(f"  {RED}✖ Fallo en compra: {r_compra.text}{RESET}")
    except Exception as e:
        print(f"  {RED}[!] Error: {e}{RESET}")

    print(f"\n--- Prueba B: Compra con Fallo Simulado (Rollback Compensatorio Saga) ---")
    print(f"[*] Enviando compra con 'simular_fallo=True'...")
    try:
        r_fallo = requests.post(
            f"{BASE_COMPRAS_LB}/compras",
            json={"usuario_id": 1, "evento_id": 2, "cantidad": 1, "simular_fallo": True},
            timeout=5
        )
        if r_fallo.status_code == 500:
            data = r_fallo.json()["detail"]
            print(f"  {YELLOW}✔ COMPENSACIÓN EXITOSA:{RESET}")
            print(f"    Detalle devuelto por el servidor: {CYAN}{data.get('mensaje')}{RESET}")
            print(f"    Decisión de Consenso: {BOLD}{data.get('decision_consenso')}{RESET}")
            print(f"    Stock restaurado en Eventos: {BOLD}{data.get('stock_restaurado')}{RESET}")
        else:
            print(f"  {RED}✖ Comportamiento inesperado: HTTP {r_fallo.status_code}{RESET}")
    except Exception as e:
        print(f"  {RED}[!] Error: {e}{RESET}")

    try:
        r_stock_final = requests.get(f"{BASE_EVENTOS}/eventos/2/stock", timeout=3)
        stock_final = r_stock_final.json()["stock"]
        print(f"\n[*] Stock final tras fallo simulado: {BOLD}{stock_final}{RESET} entradas.")
        if stock_final == stock_inicial - 1:
            print(f"  {GREEN}✔ ÉXITO TOTAL: Solo se descontó la compra A.{RESET}")
            print(f"  {GREEN}  La compra B restauró el stock automáticamente gracias a la compensación.{RESET}")
        else:
            print(f"  {RED}[!] Inconsistencia detectada en el stock.{RESET}")
    except Exception as e:
        print(f"  {RED}[!] Error: {e}{RESET}")

def demo_sincronizacion():
    print_header("3. SINCRONIZACIÓN Y EXCLUSIÓN MUTUA (SELECT ... FOR UPDATE)")
    print(f"{BOLD}Concepto:{RESET} Múltiples solicitudes concurrentes compitiendo por un stock limitado")
    print(f"no deben causar sobreventa gracias a la exclusión mutua pesimista.\n")

    try:
        r_stock = requests.get(f"{BASE_EVENTOS}/eventos/2/stock", timeout=3)
        stock_actual = r_stock.json()["stock"]
        print(f"[*] Stock actual de Festival de Jazz: {BOLD}{stock_actual}{RESET} entradas")
    except Exception as e:
        print(f"{RED}[!] Error al consultar stock: {e}{RESET}")
        return

    if stock_actual <= 0:
        print(f"{YELLOW}[!] El evento no tiene entradas. Restablece el stock primero con seed.py{RESET}")
        return

    peticiones = stock_actual + 10
    print(f"[*] Lanzando {BOLD}{peticiones}{RESET} compras simultáneas concurrentes...")

    exitos = 0
    rechazos = 0

    def enviar_compra(_):
        try:
            r = requests.post(
                f"{BASE_COMPRAS_LB}/compras",
                json={"usuario_id": 1, "evento_id": 2, "cantidad": 1, "simular_fallo": False},
                timeout=10
            )
            return r.status_code
        except Exception:
            return 500

    with ThreadPoolExecutor(max_workers=10) as executor:
        codigos = list(executor.map(enviar_compra, range(peticiones)))

    for c in codigos:
        if c == 200:
            exitos += 1
        elif c == 409:
            rechazos += 1

    print(f"\n  {GREEN}✔ Compras exitosas (200 OK): {exitos}{RESET}")
    print(f"  {YELLOW}▲ Compras rechazadas por falta de stock (409 Conflict): {rechazos}{RESET}")

    r_stock_post = requests.get(f"{BASE_EVENTOS}/eventos/2/stock", timeout=3)
    stock_post = r_stock_post.json()["stock"]
    print(f"[*] Stock remanente en BD: {BOLD}{stock_post}{RESET}")

    if stock_post == 0 and exitos == stock_actual:
        print(f"\n{BOLD}{GREEN}✔ SINCRONIZACIÓN PERFECTA: Cero sobreventa.{RESET}")
    elif stock_post < 0:
        print(f"\n{BOLD}{RED}✖ FALLO DE CONCURRENCIA: Se vendió stock negativo ({stock_post}).{RESET}")

def demo_tolerancia_fallos():
    print_header("4. TOLERANCIA A FALLOS Y REACCIÓN DE SERVIDORES (FAILOVER)")
    print(f"{BOLD}Concepto:{RESET} Nginx balancea entre compras1 y compras2. Si una réplica cae,")
    print(f"Nginx detecta el fallo mediante {CYAN}proxy_next_upstream{RESET} y redirige la petición a la otra.")
    print(f"\n[*] Enviando 6 peticiones al Balanceador (:8005) para observar qué instancia atiende:")

    for i in range(1, 7):
        try:
            r = requests.post(
                f"{BASE_COMPRAS_LB}/compras",
                json={"usuario_id": 2, "evento_id": 1, "cantidad": 1, "simular_fallo": False},
                timeout=4
            )
            if r.status_code == 200:
                data = r.json()
                print(f"  Petición #{i}: {GREEN}200 OK{RESET} -> {BOLD}{data['atendido_por']}{RESET}")
            else:
                print(f"  Petición #{i}: {YELLOW}HTTP {r.status_code}{RESET}")
        except Exception as e:
            print(f"  Petición #{i}: {RED}Error {e}{RESET}")
        time.sleep(0.4)

    print(f"\n{BOLD}Para demostrar la caída en vivo:{RESET}")
    print(f"  1. Ejecuta en otra terminal: {YELLOW}docker compose stop compras1{RESET}")
    print(f"  2. Vuelve a ejecutar esta opción o usa la interfaz web en http://localhost:8080")
    print(f"  3. Observarás que el 100% de las peticiones serán atendidas por {BOLD}compras (instancia 2){RESET}")
    print(f"     sin que el usuario final perciba ninguna interrupción.")
    print(f"  4. Para restaurarla: {YELLOW}docker compose start compras1{RESET}")

def demo_eleccion_lider():
    print_header("5. ELECCIÓN DE LÍDER (BULLY ALGORITHM) Y GESTIÓN DE RECURSOS")
    print(f"{BOLD}Concepto:{RESET} Un líder dirige mientras tenga recursos óptimos y mayor jerarquía.")
    print(f"Si se deteriora por falta de recursos o conflicto, debe ceder el mando a un seguidor.\n")

    # 1. Consultar estado actual de compras1 y compras2
    try:
        r1 = requests.get(f"{BASE_COMPRAS_R1}/lider", timeout=3).json()
        r2 = requests.get(f"{BASE_COMPRAS_R2}/lider", timeout=3).json()
        print(f"[*] Estado inicial:")
        print(f"    compras1: Prioridad={r1['prioridad']} | Rol={'👑 LÍDER' if r1['es_lider'] else 'SEGUIDOR'} | Recursos={r1['estado_recursos']}")
        print(f"    compras2: Prioridad={r2['prioridad']} | Rol={'👑 LÍDER' if r2['es_lider'] else 'SEGUIDOR'} | Recursos={r2['estado_recursos']}")
    except Exception as e:
        print(f"{RED}[!] Error al consultar líderes: {e}{RESET}")
        return

    # 2. Simular deterioro y renuncia del líder compras1
    print(f"\n[*] Simulando agotamiento de recursos y degradación en compras1...")
    try:
        requests.post(f"{BASE_COMPRAS_R1}/lider/ceder", timeout=3)
        r1_post = requests.get(f"{BASE_COMPRAS_R1}/lider", timeout=3).json()
        print(f"    {YELLOW}compras1 ha cedido la dirección: {r1_post['motivo']}{RESET}")
    except Exception as e:
        print(f"{RED}[!] Error: {e}{RESET}")

    # 3. compras2 asume el liderazgo (Bully Failover)
    print(f"\n[*] compras2 detecta la vacante y reclama el liderazgo:")
    try:
        requests.post(f"{BASE_COMPRAS_R2}/lider/asumir", timeout=3)
        r2_post = requests.get(f"{BASE_COMPRAS_R2}/lider", timeout=3).json()
        print(f"    {GREEN}compras2 es ahora el NUEVO LÍDER activo: {r2_post['motivo']}{RESET}")
    except Exception as e:
        print(f"{RED}[!] Error: {e}{RESET}")

    # 4. Restauración
    print(f"\n[*] Restaurando configuración óptima para compras1 y compras2...")
    requests.post(f"{BASE_COMPRAS_R1}/lider/restaurar", timeout=3)
    requests.post(f"{BASE_COMPRAS_R2}/lider/restaurar", timeout=3)
    print(f"    {GREEN}✔ Sistema restablecido a condiciones normales.{RESET}")

def demo_dialogo_consenso():
    print_header("6. DIÁLOGO DE CONSENSO SECUENCIAL (GLOBAL_COMMIT VS GLOBAL_ABORT)")
    print(f"{BOLD}Concepto:{RESET} Si uno de los servidores no cumple con la secuencia atómica")
    print(f"(Usuarios -> Eventos -> Compras), la sincronización IMPIDE la transacción.\n")

    # Escenario 1: Todos cumplen -> GLOBAL_COMMIT
    print(f"--- Escenario 1: Todos los servidores cumplen la secuencia ---")
    try:
        r = requests.post(
            f"{BASE_COMPRAS_LB}/compras",
            json={"usuario_id": 1, "evento_id": 3, "cantidad": 1, "simular_fallo": False},
            timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            print(f"  {GREEN}✔ Decisión de Consenso: {data['decision_consenso']}{RESET}")
            print(f"  {GREEN}  Sincronización permitió transacción: {data['sincronizacion_permitio_transaccion']}{RESET}")
            for paso in data.get("dialogo_consenso", []):
                print(f"    Paso {paso['paso']} [{paso['servidor']}]: {paso['respuesta']} (Reloj T={paso['reloj_logico_T']})")
    except Exception as e:
        print(f"{RED}[!] Error: {e}{RESET}")

    # Escenario 2: Usuario inválido -> GLOBAL_ABORT
    print(f"\n--- Escenario 2: Servidor Usuarios no cumple (Usuario inexistente ID=999) ---")
    try:
        r = requests.post(
            f"{BASE_COMPRAS_LB}/compras",
            json={"usuario_id": 999, "evento_id": 3, "cantidad": 1, "simular_fallo": False},
            timeout=5
        )
        if r.status_code in [404, 500]:
            det = r.json()["detail"]
            print(f"  {RED}✖ Decisión de Consenso: {det.get('decision_consenso', 'GLOBAL_ABORT')}{RESET}")
            print(f"  {RED}  Sincronización impidió transacción: {det.get('sincronizacion_impidio_transaccion')}{RESET}")
            print(f"  {YELLOW}  Motivo: {det.get('motivo')}{RESET}")
            for paso in det.get("dialogo_consenso", []):
                print(f"    Paso {paso['paso']} [{paso['servidor']}]: {paso['respuesta']}")
    except Exception as e:
        print(f"{RED}[!] Error: {e}{RESET}")

def demo_reloj_lamport():
    print_header("7. RELOJ LÓGICO DE LAMPORT (T) Y EXCLUSIÓN MUTUA EN STOCK")
    print(f"{BOLD}Concepto:{RESET} Los relojes físicos sufren drift y desincronización de red.")
    print(f"El timestamp lógico T garantiza causalidad: max(L_local, L_msg) + 1.\n")

    try:
        r = requests.post(
            f"{BASE_COMPRAS_LB}/compras",
            json={"usuario_id": 2, "evento_id": 1, "cantidad": 1, "simular_fallo": False},
            timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            print(f"[*] Compra procesada con éxito:")
            print(f"    Timestamp Lógico Final: {BOLD}{MAGENTA}T={data['reloj_logico_T']}{RESET}")
            mutex = data.get("exclusion_mutua", {})
            if mutex:
                print(f"\n    {CYAN}🔒 Exclusión Mutua en Base de Datos:{RESET}")
                print(f"      Mecanismo: {mutex.get('mecanismo')}")
                print(f"      Recurso: {mutex.get('recurso_bloqueado')}")
                print(f"      Tiempo retención sección crítica: {BOLD}{mutex.get('tiempo_seccion_critica_ms')} ms{RESET}")
                print(f"      Garantía: {mutex.get('estado')}")
    except Exception as e:
        print(f"{RED}[!] Error: {e}{RESET}")

def demo_imprevisto_recuperacion():
    print_header("8. SIMULACIÓN DE IMPREVISTOS Y CRASH RECOVERY (APAGÓN & ROLLBACK)")
    print(f"{BOLD}Concepto:{RESET} Si la máquina se apaga abruptamente o un imprevisto interrumpe")
    print(f"la transacción en pleno vuelo, el sistema debe activar Crash Recovery,")
    print(f"revertir el stock y verificar que los datos previamente guardados permanezcan intactos.\n")

    print(f"[*] Lanzando compra con imprevisto: 'apagon_maquina'...")
    try:
        r = requests.post(
            f"{BASE_COMPRAS_LB}/compras",
            json={"usuario_id": 1, "evento_id": 2, "cantidad": 1, "tipo_imprevisto": "apagon_maquina"},
            timeout=5
        )
        if r.status_code == 500:
            det = r.json()["detail"]
            print(f"  {YELLOW}⚡ IMPREVISTO CAPTURADO:{RESET} {det.get('mensaje')}")
            print(f"  {CYAN}🔄 PROTOCOLO DE RECUPERACIÓN SAGA:{RESET}")
            print(f"     Stock compensado y restaurado: {BOLD}{det.get('stock_restaurado')}{RESET}")
            print(f"     Datos previos preservados: {GREEN}{det.get('datos_previos_preservados')}{RESET}")
            print(f"     Decisión global: {BOLD}{det.get('decision_consenso')}{RESET}")
    except Exception as e:
        print(f"{RED}[!] Error: {e}{RESET}")

    print(f"\n[*] Ejecutando diagnóstico de Crash Recovery en el clúster...")
    try:
        r_rec = requests.post(f"{BASE_COMPRAS_LB}/compras/recuperar-servidor", timeout=4)
        if r_rec.status_code == 200:
            rec_data = r_rec.json()
            print(f"  {GREEN}✔ DIAGNÓSTICO EXITOSO:{RESET} {rec_data.get('integridad_base_datos')}")
            print(f"    Compras previas intactas: {BOLD}{rec_data.get('compras_previas_intactas')}{RESET}")
            print(f"    Protocolo verificado: {rec_data.get('protocolo')}")
    except Exception as e:
        print(f"{RED}[!] Error al recuperar: {e}{RESET}")

def demo_transaccion_grupal():
    print_header("9. TRANSACCIÓN GRUPAL ATÓMICA (ALL-OR-NOTHING)")
    print(f"{BOLD}Concepto:{RESET} Un lote de compra con múltiples eventos en una sola transacción.")
    print(f"O se confirman TODOS los ítems, o NINGUNO (All-or-Nothing con compensación en cascada).\n")

    # Caso 1: Lote exitoso
    print(f"--- Caso 1: Transacción Grupal Válida (GLOBAL_COMMIT) ---")
    payload_ok = {
        "usuario_id": 1,
        "items": [
            {"evento_id": 1, "cantidad": 1},
            {"evento_id": 3, "cantidad": 2}
        ],
        "tipo_imprevisto": "ninguno"
    }
    try:
        r = requests.post(f"{BASE_COMPRAS_LB}/compras/grupal", json=payload_ok, timeout=6)
        if r.status_code == 200:
            data = r.json()
            print(f"  {GREEN}✔ GLOBAL_COMMIT:{RESET} {data['mensaje']}")
            print(f"    Total de ítems confirmados: {BOLD}{data['total_items']}{RESET}")
            print(f"    Atendido por: {data['atendido_por']}")
        else:
            print(f"  {RED}✖ Fallo: {r.text}{RESET}")
    except Exception as e:
        print(f"{RED}[!] Error: {e}{RESET}")

    # Caso 2: Lote con Imprevisto / Fallo simulado
    print(f"\n--- Caso 2: Transacción Grupal con Imprevisto Forzado (GLOBAL_ABORT) ---")
    payload_fallo = {
        "usuario_id": 1,
        "items": [
            {"evento_id": 1, "cantidad": 1},
            {"evento_id": 2, "cantidad": 1}
        ],
        "tipo_imprevisto": "apagon_maquina"
    }
    try:
        r_f = requests.post(f"{BASE_COMPRAS_LB}/compras/grupal", json=payload_fallo, timeout=6)
        if r_f.status_code == 409:
            det = r_f.json()["detail"]
            print(f"  {YELLOW}✔ GLOBAL_ABORT CONTROLADO:{RESET} {det['mensaje']}")
            print(f"    Garantía: {BOLD}{det.get('atomicidad_grupal')}{RESET}")
            print(f"    Compensaciones en cascada ejecutadas:")
            for comp in det.get("items_compensados", []):
                print(f"      ↺ Evento #{comp['evento_id']}: {comp['cantidad_liberada']} entradas liberadas ({comp['estado']})")
        else:
            print(f"  HTTP {r_f.status_code}: {r_f.text}")
    except Exception as e:
        print(f"{RED}[!] Error: {e}{RESET}")

def demo_auditoria_consistencia():
    print_header("10. AUDITORÍA DE CONSISTENCIA DE DATOS (INVARIANTE ACID)")
    print(f"{BOLD}Concepto:{RESET} Demuestra matemáticamente que Stock_Origen == Stock_Actual + Vendidas")
    print(f"garantizando que ninguna manipulación, compra o imprevisto corrompa los datos.\n")

    try:
        r = requests.get(f"{BASE_COMPRAS_LB}/auditoria/consistencia", timeout=5)
        if r.status_code == 200:
            data = r.json()
            print(f"[*] Veredicto Global: {BOLD}{GREEN if data['todo_consistente'] else RED}{data['veredicto']}{RESET}")
            print(f"    Ecuación Invariante: {CYAN}{data['ecuacion']}{RESET}")
            print(f"    Balance de Discrepancia Global: {BOLD}{data['balance_global_diferencia']}{RESET}")
            print(f"    Total Entradas Iniciales: {data['totales']['stock_inicial']}")
            print(f"    Total Entradas Vendidas: {data['totales']['entradas_vendidas']}")
            print(f"    Total Stock Disponible: {data['totales']['stock_actual_disponible']}")

            print(f"\n  {'ID':<4} {'Nombre del Evento':<30} {'Inicial':<9} {'Vendidas':<10} {'Actual':<8} {'Estado':<12}")
            print(f"  {'-'*75}")
            for ev in data.get("reporte_por_evento", []):
                color = GREEN if ev['es_consistente'] else RED
                print(f"  #{ev['evento_id']:<3} {ev['nombre']:<30} {ev['stock_inicial_origen']:<9} {ev['entradas_vendidas']:<10} {ev['stock_actual_disponible']:<8} {color}{ev['estado']}{RESET}")
        else:
            print(f"{RED}[!] Error al consultar auditoría: HTTP {r.status_code}{RESET}")
    except Exception as e:
        print(f"{RED}[!] Error: {e}{RESET}")

def menu():
    while True:
        print(f"\n{BOLD}{MAGENTA}=================================================================={RESET}")
        print(f"{BOLD}{MAGENTA} PLATAFORMA DISTRIBUIDA DE ENTRADAS — SUITE DE PRUEBAS Y DEMO {RESET}")
        print(f"{BOLD}{MAGENTA}=================================================================={RESET}")
        print("  1. Monitoreo Activo y Detección de Caídas (Heartbeats)")
        print("  2. Atomicidad Distribuida y Rollback (Patrón Saga)")
        print("  3. Sincronización Concurrente y Exclusión Mutua (SELECT FOR UPDATE)")
        print("  4. Tolerancia a Fallos y Failover (Nginx LB)")
        print("  5. Elección de Líder (Bully) y Deterioro de Recursos")
        print("  6. Diálogo de Consenso Secuencial (GLOBAL_COMMIT vs GLOBAL_ABORT)")
        print("  7. Reloj Lógico de Lamport (T) y Métricas de Sección Crítica")
        print("  8. Simulación de Imprevistos & Crash Recovery (Apagón & Rollback)")
        print("  9. Transacción Grupal Atómica (All-or-Nothing Batch)")
        print(" 10. Auditoría de Consistencia de Datos en Vivo (Invariante ACID)")
        print(" 11. Ejecutar TODAS las demostraciones secuencialmente")
        print("  0. Salir")
        print("------------------------------------------------------------------")
        opc = input(f"{BOLD}Selecciona una opción [0-11]: {RESET}").strip()

        if opc == "1":
            check_health()
        elif opc == "2":
            demo_atomicidad()
        elif opc == "3":
            demo_sincronizacion()
        elif opc == "4":
            demo_tolerancia_fallos()
        elif opc == "5":
            demo_eleccion_lider()
        elif opc == "6":
            demo_dialogo_consenso()
        elif opc == "7":
            demo_reloj_lamport()
        elif opc == "8":
            demo_imprevisto_recuperacion()
        elif opc == "9":
            demo_transaccion_grupal()
        elif opc == "10":
            demo_auditoria_consistencia()
        elif opc == "11":
            check_health()
            demo_atomicidad()
            demo_sincronizacion()
            demo_tolerancia_fallos()
            demo_eleccion_lider()
            demo_dialogo_consenso()
            demo_reloj_lamport()
            demo_imprevisto_recuperacion()
            demo_transaccion_grupal()
            demo_auditoria_consistencia()
        elif opc == "0":
            print("\nFinalizando demostración. ¡Éxitos en la sustentación!\n")
            break
        else:
            print(f"{RED}Opción no válida.{RESET}")

if __name__ == "__main__":
    menu()
