"""Tests for T035 Ed25519 audit checkpoints."""

from __future__ import annotations

import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from browser_use_agent.audit.signed_checkpoints import (
    sign_checkpoint,
    verify_checkpoint_signature,
)
from browser_use_agent.db.models import AuditCheckpoint


def test_checkpoint_signature_rejects_rewritten_chain_hash() -> None:
    """A changed chain head cannot verify without the signing key."""
    private_key = Ed25519PrivateKey.generate()
    run_id = uuid.uuid4()
    checkpoint = AuditCheckpoint(
        run_id=run_id,
        seq=10,
        chain_hash="a" * 64,
        key_id="old",
        signature=sign_checkpoint(private_key, run_id, 10, "a" * 64),
    )
    public_key = private_key.public_key()

    assert verify_checkpoint_signature(public_key, checkpoint)
    checkpoint.chain_hash = "b" * 64
    assert not verify_checkpoint_signature(public_key, checkpoint)
