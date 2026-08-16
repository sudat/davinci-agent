from services.contracts.serialization import canonical_json_bytes
from services.gates.models import GatePolicy, GateResult


def canonical_gate_bytes(gate: GatePolicy | GateResult) -> bytes:
    return canonical_json_bytes(gate)
