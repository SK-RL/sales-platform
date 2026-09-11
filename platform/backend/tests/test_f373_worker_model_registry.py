"""F373 — the worker must load the whole model registry.

Production, first Ashby dry run: the Celery task failed in ~1 s with

    NoReferencedTableError: Foreign key associated with column
    'applications.routine_run_id' could not find table 'routine_runs'

before opening a browser. ``submit_application_task`` imports the six
models it touches; SQLAlchemy's unit of work then sorts EVERY table in
the metadata by foreign key on the first flush, and ``routine_runs``
was never imported in the worker process (nine table modules were
missing from ``app/models/__init__``). The API never hit it because
routers import those modules themselves — so the feature looked whole
from the browser and had never once executed a submission.

Unit tests use fake sessions and cannot see this; the first test below
runs the worker's real import path in a fresh interpreter and asks the
metadata to sort itself.
"""

import pathlib
import subprocess
import sys

import pytest

MODELS_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "models"


class TestWorkerImportPath:
    def test_flush_dependency_sort_works_after_worker_imports(self):
        """Exactly what the worker does before its first commit."""
        code = (
            "import app.workers.celery_app\n"
            "import app.workers.tasks\n"
            "from app.models.application import Application\n"
            "from app.database import Base\n"
            "from sqlalchemy.orm import configure_mappers\n"
            "configure_mappers()\n"
            "print(len(list(Base.metadata.sorted_tables)))\n"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           cwd=str(MODELS_DIR.parents[1]), timeout=120)
        assert r.returncode == 0, (r.stdout + r.stderr)[-800:]
        assert int(r.stdout.strip().splitlines()[-1]) > 20

    def test_worker_package_loads_the_registry(self):
        src = (MODELS_DIR.parents[1] / "app" / "workers" / "tasks" / "__init__.py").read_text()
        assert "import app.models" in src


class TestRegistryIsComplete:
    def test_every_table_module_is_imported(self):
        """The guard: a new model file that isn't registered here will
        break the worker the same way, silently."""
        init = (MODELS_DIR / "__init__.py").read_text()
        missing = [
            f.stem for f in sorted(MODELS_DIR.glob("*.py"))
            if f.name != "__init__.py" and "__tablename__" in f.read_text()
            and f"app.models.{f.stem}" not in init and f"    {f.stem}," not in init
        ]
        assert missing == [], f"model modules not imported by app.models: {missing}"

    def test_the_production_offender_is_registered(self):
        import app.models  # noqa: F401
        assert "app.models.routine_run" in sys.modules


class TestDryRunLeavesTheRowUsable:
    def test_passed_dry_run_returns_to_prepared_with_a_result(self):
        import inspect
        import app.workers.tasks.apply_task as at
        src = inspect.getsource(at.submit_application_task)
        i = src.find("else:\n            app_row.status = STATUS_PREPARED")
        assert i > 0, "a passed dry run must go back to prepared, not stay in_flight"
        assert '"gate": "passed"' in src[i:i + 600]
