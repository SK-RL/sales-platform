"""F384 — the applications list carries the gate verdict."""
import inspect
from app.api.v1 import applications

def test_list_serialises_gate_fields():
    src = inspect.getsource(applications.list_applications)
    assert '"gate_reason"' in src and '"gate"' in src and '"gate_error"' in src
