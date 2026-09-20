"""Append-only audit writer and per-run hash-chain verification."""

from browser_use_agent.audit.browser_actions import BrowserActionWriter
from browser_use_agent.audit.checkpoints import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointReason,
    CheckpointResult,
    CheckpointSettings,
    CheckpointWriter,
    decide_checkpoint,
    load_checkpoint_payload,
    load_checkpoint_settings,
    observation_from_checkpoint,
)
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
    "CHECKPOINT_SCHEMA_VERSION",
    "GENESIS_PREV_HASH",
    "Actor",
    "AuditWriter",
    "AuditWriterError",
    "BrowserActionWriter",
    "CheckpointReason",
    "CheckpointResult",
    "CheckpointSettings",
    "CheckpointWriter",
    "ModelCallWriter",
    "canonicalize_event_fields",
    "canonicalize_value",
    "compute_event_hash",
    "decide_checkpoint",
    "event_hash_fields",
    "get_append_hook",
    "load_checkpoint_payload",
    "load_checkpoint_settings",
    "observation_from_checkpoint",
    "set_append_hook",
    "verify_events_chain",
    "verify_run_chain",
]
