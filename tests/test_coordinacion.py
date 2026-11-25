"""Directorio con latido, Bully, relojes vectoriales, Cristian y Berkeley.

Se simulan cinco instancias en memoria: un enrutador entrega los mensajes y
permite "apagar" instancias o cortar enlaces sin red real.
"""
from shared.coordination import Coordinator, compare_vectors


class FakeTime:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Network:
    def __init__(self):
        self.nodes = {}
        self.down = set()
        self.cut = set()          # pares (origen, destino) sin enlace
        self.skew = {}            # desfase del reloj físico por instancia (s)
        self.sender = None

    def reachable(self, target):
        return target in self.nodes and target not in self.down and (self.sender, target) not in self.cut

    def post(self, peer, path, payload):
        target = peer["id"]
        if not self.reachable(target):
            return None
        node = self.nodes[target]
        previous, self.sender = self.sender, target
        try:
            if path == "/coordinacion/latido":
                return {"ok": node.receive_heartbeat(payload)}
            if path == "/coordinacion/eleccion":
                reply = node.receive_election(payload)
                if reply and reply.get("ok"):
                    node.start_election("respuesta a una elección")
                return reply
            if path == "/coordinacion/coordinador":
                return {"ok": node.receive_coordinator(payload)}
            if path == "/coordinacion/ajuste":
                return {"ok": node.receive_adjustment(payload)}
        finally:
            self.sender = previous
        return None

    def get(self, peer, path):
        target = peer["id"]
        if not self.reachable(target) or self.nodes[target].silenced:
            return None
        if path == "/coordinacion/tiempo":
            return self.nodes[target].time_reply()
        return None


class Transport:
    def __init__(self, network, owner):
        self.network, self.owner = network, owner

    def post(self, peer, path, payload):
        previous, self.network.sender = self.network.sender, self.owner
        try:
            return self.network.post(peer, path, payload)
        finally:
            self.network.sender = previous

    def get(self, peer, path):
        previous, self.network.sender = self.network.sender, self.owner
        try:
            return self.network.get(peer, path)
        finally:
            self.network.sender = previous


def cluster(count=5):
    net, mono = Network(), FakeTime()
    members = [{"id": f"api_n{i}", "servicio": "compras" if i % 2 else "eventos", "nodo": f"n{i}",
                "url": f"http://n{i}", "prioridad": i} for i in range(1, count + 1)]
    for member in members:
        ident = member["id"]
        clock = lambda ident=ident: mono.now + net.skew.get(ident, 0.0)
        net.nodes[ident] = Coordinator(member, members, Transport(net, ident), clock=clock, monotonic=mono)
    return net, mono


def run(net, mono, seconds=2, cycles=1):
    for _ in range(cycles):
        mono.now += seconds
        for node_id, node in net.nodes.items():
            if node_id not in net.down:
                node.tick()


def test_highest_priority_becomes_coordinator_and_everyone_agrees():
    net, mono = cluster()
    run(net, mono, cycles=2)
    assert {node.coordinator for node in net.nodes.values()} == {"api_n5"}


def test_directory_marks_silent_node_suspect_then_dead_and_resolver_skips_it():
    net, mono = cluster()
    run(net, mono, cycles=2)
    observer = net.nodes["api_n1"]
    assert observer.state_of("api_n3") == "viva"
    net.down.add("api_n3")
    run(net, mono, seconds=4, cycles=2)        # 8 s sin latido > T + Δ
    assert observer.state_of("api_n3") == "sospechosa"
    run(net, mono, seconds=4, cycles=2)        # 16 s > plazo de caída
    assert observer.state_of("api_n3") == "caida"
    urls = [row["url"] for row in observer.resolve("compras")["direcciones"]]
    assert "http://n3" not in urls and "http://n1" in urls and "http://n5" in urls


def test_coordinator_crash_triggers_bully_election_of_next_highest():
    net, mono = cluster()
    run(net, mono, cycles=2)
    net.down.add("api_n5")
    run(net, mono, seconds=4, cycles=3)
    alive = [node for ident, node in net.nodes.items() if ident != "api_n5"]
    assert {node.coordinator for node in alive} == {"api_n4"}
    assert any("convoca elección" in item["detalle"] for item in net.nodes["api_n1"].history) or \
        any("convoca elección" in item["detalle"] for item in net.nodes["api_n4"].history)


def test_recovered_higher_priority_node_takes_over_bully():
    net, mono = cluster()
    run(net, mono, cycles=2)
    net.nodes["api_n5"].silence()
    run(net, mono, seconds=4, cycles=3)
    assert net.nodes["api_n1"].coordinator == "api_n4"
    net.nodes["api_n5"].restore()
    run(net, mono, cycles=1)
    assert {node.coordinator for node in net.nodes.values()} == {"api_n5"}


def test_silenced_instance_rejects_coordination_messages():
    net, mono = cluster(3)
    node = net.nodes["api_n2"]
    node.silence()
    assert node.receive_heartbeat({"id": "api_n1"}) is False
    assert node.receive_election({"id": "api_n1", "prioridad": 1}) is None
    assert node.state_of("api_n2") == "silenciada"


def test_unknown_sender_cannot_register():
    net, mono = cluster(3)
    assert net.nodes["api_n1"].receive_heartbeat({"id": "intruso"}) is False
    assert "intruso" not in net.nodes["api_n1"].directory


def test_vector_clocks_detect_causality_and_concurrency():
    net, mono = cluster(3)
    a, b, c = (net.nodes[f"api_n{i}"] for i in (1, 2, 3))
    header = a.outgoing("compras pide reserva")["X-Nexustix-Reloj"]
    receive = b.incoming(header, "eventos recibe reserva")
    independent = c.record("evento local en otra instancia")
    sent = a.events[-1]
    assert compare_vectors(sent["vector"], receive["vector"]) == "antes"
    assert receive["lamport"] > sent["lamport"]
    assert compare_vectors(receive["vector"], independent["vector"]) == "concurrente"
    assert compare_vectors(receive["vector"], receive["vector"]) == "igual"
    assert compare_vectors(receive["vector"], sent["vector"]) == "despues"


def test_cristian_estimates_offset_against_coordinator():
    net, mono = cluster(3)
    run(net, mono, cycles=2)
    net.skew["api_n1"] = -0.250                 # el reloj de n1 va 250 ms atrasado
    result = net.nodes["api_n1"].cristian_sync()
    assert result["referencia"] == "api_n3"
    assert abs(result["desfase_ms"] - 250) < 1


def test_berkeley_averages_clocks_and_sends_adjustments():
    net, mono = cluster(3)
    run(net, mono, cycles=2)
    net.skew.update({"api_n1": -0.300, "api_n2": 0.0, "api_n3": 0.300})
    result = net.nodes["api_n3"].berkeley_round()
    adjustments = {row["instancia"]: row["ajuste_ms"] for row in result["participantes"]}
    # El promedio se expresa respecto al reloj del coordinador (n3 va +300 ms).
    assert round(result["promedio_ms"]) == -300
    assert round(adjustments["api_n1"]) == 300 and round(adjustments["api_n3"]) == -300
    assert round(net.nodes["api_n1"].berkeley["ajuste_ms"]) == 300


def test_partition_keeps_each_side_with_its_own_coordinator_and_heals():
    net, mono = cluster(4)
    run(net, mono, cycles=2)
    left, right = {"api_n1", "api_n2"}, {"api_n3", "api_n4"}
    net.cut = {(a, b) for a in left for b in right} | {(b, a) for a in left for b in right}
    run(net, mono, seconds=4, cycles=3)
    assert net.nodes["api_n1"].coordinator == "api_n2"
    assert net.nodes["api_n3"].coordinator == "api_n4"
    net.cut = set()
    run(net, mono, cycles=2)
    assert {node.coordinator for node in net.nodes.values()} == {"api_n4"}
