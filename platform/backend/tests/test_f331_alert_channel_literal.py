"""F331 — Literal-validate ``AlertConfigCreate.channel``.

Closes F120 🟠. Pre-fix the schema declared ``channel: str =
"google_chat"`` so a typo (``slcak``, ``email_v2``, ``""``) was
silently persisted to the DB. The downstream dispatcher's
``if channel == "slack": ...`` cascade fell through to the default
branch — the alert was created, the admin saw a green check, but
no message ever fired. Worse: the test-alert endpoint also
short-circuits on the typo so the admin's "Test webhook" click
returned success.

F331 swaps in
``AlertChannel = Literal["google_chat", "slack", "discord", "email"]``
and uses it as the field type. Pydantic V2 422s typos at parse
time so the admin gets a clear error AT CREATE — no silent half-
configured alert.

The Literal includes ``"slack"``, ``"discord"``, ``"email"`` as
pre-allocated values matching the comment on the
``AlertConfig.channel`` model column (``google_chat | slack |
email``). Currently only ``google_chat`` is fully implemented in
the dispatcher; the other values 422-pass at parse time but the
dispatcher returns a "channel not implemented" error so an admin
can't accidentally configure an alert for a channel that won't
fire.
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
os.environ.setdefault("JWT_SECRET", "pytest-f331")


_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _read_handler() -> str:
    return (_BACKEND / "app" / "api" / "v1" / "alerts.py").read_text()


def test_handler_imports_literal():
    src = _read_handler()
    assert "from typing import Literal" in src, (
        "F331 regression: Literal import missing — channel field "
        "can't be Literal-validated."
    )


def test_alert_channel_literal_defined():
    """The reusable AlertChannel Literal must be at module level
    so the schema can declare ``channel: AlertChannel`` and any
    future PATCH / dispatcher code can reuse it.
    """
    src = _read_handler()
    assert "AlertChannel = Literal[" in src, (
        "F331 regression: AlertChannel Literal removed."
    )
    # Must include google_chat (the implemented one).
    assert '"google_chat"' in src
    # And the documented future channels per the model column comment.
    assert '"slack"' in src
    assert '"email"' in src


def test_create_schema_uses_literal_channel():
    """The actual field declaration must use the Literal, not
    bare ``str``.
    """
    src = _read_handler()
    # Anchor on AlertConfigCreate body.
    create_idx = src.find("class AlertConfigCreate(BaseModel)")
    assert create_idx > 0
    window = src[create_idx:create_idx + 1200]
    # The channel field must reference AlertChannel.
    assert "channel: AlertChannel" in window, (
        "F331 regression: AlertConfigCreate.channel reverted to "
        "``str``. Typos silently accepted again — admin will see "
        "Slack-typo alerts created but never firing."
    )
    # And NOT the old bare-str form. Match the actual field
    # declaration (newline + indent) so we don't trip on the
    # F331 docstring/comment that recounts the old shape.
    assert "    channel: str =" not in window, (
        "F331 regression: stale ``channel: str`` declaration "
        "still present in AlertConfigCreate."
    )


def test_default_value_matches_implemented_channel():
    """The default must remain ``google_chat`` (the only fully-
    implemented channel) so admins who omit the field land on
    a working configuration.
    """
    src = _read_handler()
    create_idx = src.find("class AlertConfigCreate(BaseModel)")
    window = src[create_idx:create_idx + 1200]
    assert '= "google_chat"' in window, (
        "F331 regression: AlertConfigCreate.channel default is no "
        "longer ``\"google_chat\"`` — admins omitting the field "
        "may land on an unimplemented channel and silent-fail."
    )


def test_update_schema_does_not_reintroduce_bare_channel():
    """AlertConfigUpdate intentionally OMITS channel (channel is
    pinned at create-time alongside webhook_url; switching channels
    is a destroy+recreate). Make sure a future re-add uses the
    Literal, not bare str.
    """
    src = _read_handler()
    update_idx = src.find("class AlertConfigUpdate(BaseModel)")
    assert update_idx > 0
    # Body of AlertConfigUpdate is short; window 800 is plenty.
    window = src[update_idx:update_idx + 800]
    # If channel was re-introduced as ``str``, F331 regression.
    assert "channel: str" not in window, (
        "F331 regression: AlertConfigUpdate.channel reintroduced as "
        "bare ``str``. PATCH would silently accept typos."
    )
