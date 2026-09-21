"""Ed25519 signatures for periodic audit-chain checkpoints."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from browser_use_agent.audit.hashchain import verify_run_chain
from browser_use_agent.db.engine import create_engine_from_settings
from browser_use_agent.db.models import AgentEvent, AuditCheckpoint, Run
from browser_use_agent.runs.status import TERMINAL_STATUSES

CHECKPOINT_DOMAIN = b"browser-use-audit-checkpoint:v1\0"
DEFAULT_INTERVAL = 10
TERMINAL_EVENT_TYPES = frozenset(
    {
        "run_succeeded",
        "run_failed",
        "run_cancelled",
        "approval_denied",
        "approval_timeout",
    }
)


class CheckpointKeyError(ValueError):
    """Raised when an audit checkpoint key is missing or invalid."""


def checkpoint_message(run_id: uuid.UUID, seq: int, chain_hash: str) -> bytes:
    """Build the versioned, canonical message signed for a checkpoint.

    Args:
        run_id: Owning run.
        seq: Event sequence represented by the checkpoint.
        chain_hash: SHA-256 hash of the event at ``seq``.

    Returns:
        UTF-8 signing preimage.
    """
    payload = json.dumps(
        {"chain_hash": chain_hash, "run_id": str(run_id), "seq": int(seq)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return CHECKPOINT_DOMAIN + payload.encode("utf-8")


def _load_private_key(data: bytes) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from PEM or raw 32-byte seed."""
    if len(data) == 32:
        return Ed25519PrivateKey.from_private_bytes(data)
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        raise CheckpointKeyError("audit signing key is not valid Ed25519 PEM/raw data") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise CheckpointKeyError("audit signing key must be Ed25519")
    return key


def _load_public_key(data: bytes) -> Ed25519PublicKey:
    """Load an Ed25519 public key from PEM or raw 32-byte bytes."""
    if len(data) == 32:
        return Ed25519PublicKey.from_public_bytes(data)
    try:
        key = serialization.load_pem_public_key(data)
    except (ValueError, TypeError) as exc:
        raise CheckpointKeyError("audit public key is not valid Ed25519 PEM/raw data") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise CheckpointKeyError("audit public key must be Ed25519")
    return key


def load_private_key(path: str | Path | None = None) -> Ed25519PrivateKey:
    """Load the configured Ed25519 private key from a secret file.

    Args:
        path: Explicit key path, or ``AUDIT_SIGNING_KEY_FILE`` when omitted.

    Returns:
        Ed25519 private key.

    Raises:
        CheckpointKeyError: When no key or an invalid key is configured.
    """
    raw_path = str(path) if path is not None else os.environ.get("AUDIT_SIGNING_KEY_FILE")
    if not raw_path:
        raise CheckpointKeyError("AUDIT_SIGNING_KEY_FILE is not configured")
    key_path = Path(raw_path)
    try:
        data = key_path.read_bytes()
    except OSError as exc:
        raise CheckpointKeyError(f"cannot read audit signing key {key_path}") from exc
    return _load_private_key(data)


def load_public_keys(
    path: str | Path | None = None,
    *,
    key_id: str | None = None,
) -> dict[str, Ed25519PublicKey]:
    """Load the current public key for checkpoint verification.

    ``AUDIT_SIGNING_PUBLIC_KEYS_FILE`` may contain a JSON object mapping key ids
    to PEM strings or base64-encoded raw public keys. This preserves old keys
    during rotation; the singular file remains the Docker default.

    Args:
        path: Explicit current public-key path.
        key_id: Key id for the singular key (default: ``AUDIT_SIGNING_KEY_ID``).

    Returns:
        Mapping of key id to public key.

    Raises:
        CheckpointKeyError: When no public key or invalid key is configured.
    """
    keys: dict[str, Ed25519PublicKey] = {}
    bundle_path = os.environ.get("AUDIT_SIGNING_PUBLIC_KEYS_FILE")
    if bundle_path:
        try:
            bundle = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointKeyError(f"cannot read public-key bundle {bundle_path}") from exc
        if not isinstance(bundle, dict) or not bundle:
            raise CheckpointKeyError("public-key bundle must be a non-empty JSON object")
        for bundle_key_id, encoded in bundle.items():
            if not isinstance(bundle_key_id, str) or not isinstance(encoded, str):
                raise CheckpointKeyError("public-key bundle entries must be string key/value pairs")
            try:
                raw = encoded.encode("utf-8")
                if not encoded.lstrip().startswith("-----"):
                    raw = base64.b64decode(encoded, validate=True)
                keys[bundle_key_id] = _load_public_key(raw)
            except (ValueError, CheckpointKeyError) as exc:
                raise CheckpointKeyError(f"invalid public key {bundle_key_id!r}") from exc

    raw_path = str(path) if path is not None else os.environ.get("AUDIT_SIGNING_PUBLIC_KEY_FILE")
    if raw_path:
        current_path = Path(raw_path)
        try:
            keys[key_id or os.environ.get("AUDIT_SIGNING_KEY_ID", "default")] = _load_public_key(
                current_path.read_bytes()
            )
        except OSError as exc:
            raise CheckpointKeyError(f"cannot read audit public key {current_path}") from exc
    if not keys:
        raise CheckpointKeyError("AUDIT_SIGNING_PUBLIC_KEY_FILE is not configured")
    return keys


def load_checkpoint_interval() -> int:
    """Load the periodic checkpoint interval in events.

    Returns:
        Positive interval, or zero to disable periodic checkpoints.

    Raises:
        ValueError: When the environment value is not a non-negative integer.
    """
    raw = os.environ.get("AUDIT_CHECKPOINT_INTERVAL", str(DEFAULT_INTERVAL)).strip()
    try:
        interval = int(raw)
    except ValueError as exc:
        raise ValueError("AUDIT_CHECKPOINT_INTERVAL must be a non-negative integer") from exc
    if interval < 0:
        raise ValueError("AUDIT_CHECKPOINT_INTERVAL must be a non-negative integer")
    return interval


def sign_checkpoint(
    private_key: Ed25519PrivateKey,
    run_id: uuid.UUID,
    seq: int,
    chain_hash: str,
) -> bytes:
    """Sign one run chain head."""
    return private_key.sign(checkpoint_message(run_id, seq, chain_hash))


def verify_checkpoint_signature(
    public_key: Ed25519PublicKey,
    checkpoint: AuditCheckpoint,
) -> bool:
    """Return whether a checkpoint signature matches its stored contents."""
    try:
        public_key.verify(
            checkpoint.signature,
            checkpoint_message(checkpoint.run_id, checkpoint.seq, checkpoint.chain_hash),
        )
    except InvalidSignature, ValueError:
        return False
    return True


class SignedCheckpointWriter:
    """Create signed chain-head rows at configured event boundaries."""

    def __init__(
        self,
        session: Session,
        *,
        private_key: Ed25519PrivateKey | None = None,
        key_id: str | None = None,
        interval: int | None = None,
    ) -> None:
        """Bind checkpointing to a database session.

        Args:
            session: SQLAlchemy session used for checkpoint inserts.
            private_key: Explicit signer, or configured secret when omitted.
            key_id: Rotation identifier (default: ``AUDIT_SIGNING_KEY_ID``).
            interval: Event interval; environment default when omitted.
        """
        self.session = session
        self.private_key = private_key
        self.key_id = key_id or os.environ.get("AUDIT_SIGNING_KEY_ID", "default")
        self.interval = load_checkpoint_interval() if interval is None else interval
        if self.interval < 0:
            raise ValueError("checkpoint interval must be non-negative")
        if self.private_key is None and os.environ.get("AUDIT_SIGNING_KEY_FILE"):
            self.private_key = load_private_key()

    def maybe_checkpoint(
        self, event: AgentEvent, *, terminal: bool = False
    ) -> AuditCheckpoint | None:
        """Insert a checkpoint when an event reaches a configured boundary.

        Args:
            event: Newly flushed audit event.
            terminal: Whether the owning run has reached a terminal state.

        Returns:
            The inserted row, an existing row for an already checkpointed
            sequence, or ``None`` when signing is not configured/boundary absent.
        """
        if self.private_key is None or not event.event_hash:
            return None
        if not terminal and (self.interval == 0 or int(event.seq) % self.interval):
            return None

        existing = self.session.scalar(
            select(AuditCheckpoint).where(
                AuditCheckpoint.run_id == event.run_id,
                AuditCheckpoint.seq == event.seq,
            )
        )
        if existing is not None:
            return existing

        row = AuditCheckpoint(
            run_id=event.run_id,
            seq=int(event.seq),
            chain_hash=event.event_hash,
            key_id=self.key_id,
            signature=sign_checkpoint(
                self.private_key, event.run_id, int(event.seq), event.event_hash
            ),
        )
        self.session.add(row)
        self.session.flush()
        return row


def verify_run_audit(
    session: Session,
    run_id: uuid.UUID,
    public_keys: Mapping[str, Ed25519PublicKey],
) -> bool:
    """Verify the event chain and every signed checkpoint for a run.

    Args:
        session: SQLAlchemy session bound to the audit database.
        run_id: Run to verify.
        public_keys: Key-id to public-key mapping, including rotated keys.

    Returns:
        True only when the chain and all checkpoints verify fail-closed.
    """
    events = session.scalars(
        select(AgentEvent).where(AgentEvent.run_id == run_id).order_by(AgentEvent.seq.asc())
    ).all()
    if not events or not verify_run_chain(session, run_id):
        return False
    event_hashes = {int(event.seq): event.event_hash for event in events}
    checkpoints = session.scalars(
        select(AuditCheckpoint)
        .where(AuditCheckpoint.run_id == run_id)
        .order_by(AuditCheckpoint.seq.asc())
    ).all()
    if not checkpoints:
        return False
    for checkpoint in checkpoints:
        if event_hashes.get(int(checkpoint.seq)) != checkpoint.chain_hash:
            return False
        public_key = public_keys.get(checkpoint.key_id)
        if public_key is None or not verify_checkpoint_signature(public_key, checkpoint):
            return False

    run = session.get(Run, run_id)
    terminal = bool(run and run.status in {status.value for status in TERMINAL_STATUSES})
    last_event = events[-1]
    if (terminal or last_event.event_type in TERMINAL_EVENT_TYPES) and int(
        checkpoints[-1].seq
    ) != int(last_event.seq):
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    """Verify one run from the configured database and public key files."""
    parser = argparse.ArgumentParser(description="Verify an audit chain and signed checkpoints.")
    parser.add_argument("run_id", type=uuid.UUID)
    parser.add_argument("--database-url", help="SQLAlchemy database URL override")
    parser.add_argument("--public-key-file", type=Path)
    args = parser.parse_args(argv)

    try:
        from browser_use_agent.db.migrate import resolve_database_url

        url = args.database_url or resolve_database_url()
        engine = create_engine_from_settings() if not args.database_url else None
        if engine is None:
            from sqlalchemy import create_engine

            engine = create_engine(url, pool_pre_ping=True)
        with Session(engine) as session:
            valid = verify_run_audit(session, args.run_id, load_public_keys(args.public_key_file))
        engine.dispose()
    except (CheckpointKeyError, ValueError, RuntimeError, OSError) as exc:
        print(f"audit verification failed: {exc}", file=sys.stderr)
        return 1
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
