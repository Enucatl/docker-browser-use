"""Append-only audit writer and per-run hash-chain verification."""

from browser_use_agent.audit.browser_actions import BrowserActionWriter
from browser_use_agent.audit.hashchain import (
    GENESIS_PREV_HASH,
    canonicalize_event_fields,
    canonicalize_value,
    compute_event_hash,
    event_hash_fields,
    verify_events_chain,
    verify_run_chain,
)
from browser_use_agent.audit.model_calls import ModelCallWriter
from browser_use_agent.audit.writer import (
    Actor,
    AuditWriter,
    AuditWriterError,
    get_append_hook,
    set_append_hook,
)

__all__ = [
    "GENESIS_PREV_HASH",
    "Actor",
    "AuditWriter",
    "AuditWriterError",
    "BrowserActionWriter",
    "ModelCallWriter",
    "canonicalize_event_fields",
    "canonicalize_value",
    "compute_event_hash",
    "event_hash_fields",
    "get_append_hook",
    "set_append_hook",
    "verify_events_chain",
    "verify_run_chain",
]
