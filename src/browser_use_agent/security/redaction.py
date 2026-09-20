"""Central secret redaction before audit, prompts, or artifact metadata.

**Rule: if unsure, redact.** Callers that persist events, model traces, or
browser-state snapshots must run payloads through this module (prefer
:func:`redact_for_audit`) before any sink write. Do not log or return
unredacted secret candidates from this module.

Deny-listed field names always redact. Allow-listed names (URL path segments,
element roles, intentionally masked last-4) are preserved. Remaining string
values are scanned with regex heuristics for JWTs, Authorization headers,
cookies, API tokens, and payment-card numbers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

REDACTED: Final = "[REDACTED]"
"""Stable placeholder substituted for secret values."""

# Field names (normalized: lowercased, ``-``/`` `` → ``_``) that always redact.
DENY_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "passphrase",
        "secret",
        "secrets",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "apikey",
        "api_secret",
        "client_secret",
        "authorization",
        "auth",
        "cookie",
        "cookies",
        "set_cookie",
        "session",
        "session_id",
        "sessionid",
        "csrf_token",
        "cvv",
        "cvc",
        "cid",
        "security_code",
        "card_number",
        "cardnumber",
        "pan",
        "primary_account_number",
        "ssn",
        "social_security_number",
        "private_key",
        "privatekey",
        "vault",
        "vault_key",
        "master_password",
        "bitwarden",
        "bw_password",
        "otp",
        "totp",
        "pin",
        "credential",
        "credentials",
        "bearer",
        "x_api_key",
        "x_auth_token",
    }
)

# Field names preserved even when values resemble secrets (structure / UI).
ALLOW_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "url",
        "href",
        "path",
        "pathname",
        "role",
        "aria_role",
        "aria_label",
        "tag",
        "tag_name",
        "node_name",
        "element_id",
        "dom_id",
        "xpath",
        "css_selector",
        "selector",
        "name",
        "label",
        "title",
        "placeholder",
        "text",
        "inner_text",
        "visible_text",
        "action",
        "event_type",
        "type",  # element/input type attribute name, not secret material
        "last4",
        "last_4",
        "masked_pan",
        "card_last4",
        "status",
        "success",
        "error_code",
        "run_id",
        "seq",
    }
)

_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+=/]{8,}")
_BASIC_AUTH_RE = re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]{8,}")
_AUTH_HEADER_RE = re.compile(r"(?i)(Authorization\s*[:=]\s*)([^\r\n,;]+)")
_COOKIE_PAIR_RE = re.compile(
    r"(?i)(?:^|[;\s,])("
    r"(?:session|sess|sid|token|auth|jwt|csrf|remember|refresh)"
    r"[^=\s]*=)([^\s;]+)"
)
_SET_COOKIE_RE = re.compile(r"(?i)(Set-Cookie\s*[:=]\s*)([^\r\n;]+)")
_API_KEY_ASSIGN_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|client[_-]?secret|secret[_-]?key)"
    r"\s*[:=]\s*([A-Za-z0-9_\-./+=]{8,})"
)
_PASSWORD_ASSIGN_RE = re.compile(r"(?i)\b(password|passwd|pwd|passphrase)\s*[:=]\s*(\S+)")
# Continuous digit runs of PAN length; validated with Luhn before redacting.
_PAN_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ -]*?){13,19}(?!\d)")
# Already-masked PAN forms such as ****4242 or ••••4242.
_MASKED_PAN_RE = re.compile(r"^[*\u2022xX]{2,}\d{4}$")


def normalize_field_name(name: str) -> str:
    """Normalize a field or attribute name for allow/deny lookup.

    Args:
        name: Raw mapping key or DOM attribute name.

    Returns:
        Lowercased name with hyphens and spaces mapped to underscores.
    """
    return name.strip().lower().replace("-", "_").replace(" ", "_")


def is_deny_field(name: str) -> bool:
    """Return whether ``name`` is on the explicit deny list.

    Args:
        name: Field or attribute name.

    Returns:
        True when the normalized name must always be redacted.
    """
    return normalize_field_name(name) in DENY_FIELD_NAMES


def is_allow_field(name: str) -> bool:
    """Return whether ``name`` is on the explicit allow list.

    Args:
        name: Field or attribute name.

    Returns:
        True when the field is treated as non-secret structure/UI metadata.
    """
    return normalize_field_name(name) in ALLOW_FIELD_NAMES


def _luhn_ok(digits: str) -> bool:
    """Return True when ``digits`` pass the Luhn check.

    Args:
        digits: Decimal digit string with no separators.

    Returns:
        Whether the sequence is a valid Luhn number.
    """
    if not digits.isdigit() or len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    reverse = digits[::-1]
    for i, ch in enumerate(reverse):
        n = ord(ch) - 48
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def mask_pan(digits: str) -> str:
    """Mask a primary account number, keeping only the last four digits.

    Args:
        digits: Digits-only card number (separators already stripped).

    Returns:
        A ``****`` + last-4 string, or :data:`REDACTED` if too short.
    """
    if len(digits) < 4:
        return REDACTED
    return f"****{digits[-4:]}"


def _redact_pan_in_text(text: str) -> str:
    """Replace Luhn-valid card numbers in ``text`` with masked forms.

    Args:
        text: Free-form text that may contain PANs.

    Returns:
        Text with valid PANs replaced by ``****`` + last-4.
    """

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        digits = re.sub(r"\D", "", raw)
        if not _luhn_ok(digits):
            return raw
        return mask_pan(digits)

    return _PAN_CANDIDATE_RE.sub(_replace, text)


def _redact_auth_header_match(match: re.Match[str]) -> str:
    """Replace an Authorization header value while keeping the auth scheme.

    Args:
        match: Regex match with prefix group and value group.

    Returns:
        Header prefix plus a redacted credential, preserving Bearer/Basic labels.
    """
    prefix = match.group(1)
    value = match.group(2).strip()
    lower = value.lower()
    if lower.startswith("bearer "):
        return f"{prefix}Bearer {REDACTED}"
    if lower.startswith("basic "):
        return f"{prefix}Basic {REDACTED}"
    return f"{prefix}{REDACTED}"


def redact_text(text: str) -> str:
    """Redact secret-shaped substrings in free-form text.

    Applies JWT, Authorization, cookie, API-key assignment, password
    assignment, and payment-card heuristics. Ordinary UI copy without those
    shapes is returned unchanged.

    Args:
        text: Arbitrary string (prompt fragment, log line, attribute value).

    Returns:
        Text with secret shapes replaced by stable placeholders.
    """
    if not text:
        return text
    if _MASKED_PAN_RE.fullmatch(text.strip()):
        return text

    out = text
    out = _JWT_RE.sub(REDACTED, out)
    out = _AUTH_HEADER_RE.sub(_redact_auth_header_match, out)
    out = _BEARER_RE.sub(f"Bearer {REDACTED}", out)
    out = _BASIC_AUTH_RE.sub(f"Basic {REDACTED}", out)
    out = _SET_COOKIE_RE.sub(rf"\1{REDACTED}", out)
    out = _COOKIE_PAIR_RE.sub(rf"\1{REDACTED}", out)
    out = _API_KEY_ASSIGN_RE.sub(rf"\1={REDACTED}", out)
    out = _PASSWORD_ASSIGN_RE.sub(rf"\1={REDACTED}", out)
    out = _redact_pan_in_text(out)
    return out


def _redact_scalar_for_field(field: str | None, value: Any) -> Any:
    """Redact a single non-container value in the context of ``field``.

    Args:
        field: Parent mapping key, if any.
        value: Scalar or unknown value.

    Returns:
        Redacted scalar, or the original value when safe to keep.
    """
    if value is None or isinstance(value, bool | int | float):
        if field is not None and is_deny_field(field):
            return REDACTED
        return value

    if not isinstance(value, str):
        if field is not None and is_deny_field(field):
            return REDACTED
        return value

    if field is not None:
        norm = normalize_field_name(field)
        if norm in ALLOW_FIELD_NAMES:
            # Still scrub embedded JWTs/PANs inside otherwise-allowed strings.
            return redact_text(value)
        if norm in DENY_FIELD_NAMES:
            if _MASKED_PAN_RE.fullmatch(value.strip()):
                return value
            if norm in {"card_number", "cardnumber", "pan", "primary_account_number"}:
                digits = re.sub(r"\D", "", value)
                if len(digits) >= 4:
                    return mask_pan(digits)
            return REDACTED

    return redact_text(value)


def redact_mapping(
    data: Mapping[str, Any],
    *,
    path: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Deep-copy a mapping while redacting deny fields and secret shapes.

    Structure (keys, list lengths, nesting) is preserved. Values under deny
    field names become :data:`REDACTED` (or masked PAN when appropriate).
    Nested mappings and sequences are walked recursively.

    Args:
        data: Mapping to redact (e.g. event payload or DOM attribute bag).
        path: Parent key path for nested calls (informational only).

    Returns:
        A new ``dict`` with secrets removed.
    """
    result: dict[str, Any] = {}
    for key, raw in data.items():
        key_str = str(key)
        child_path = (*path, key_str)
        result[key_str] = _redact_any(raw, field=key_str, path=child_path)
    return result


def _redact_sequence(
    values: Sequence[Any],
    *,
    field: str | None,
    path: tuple[str, ...],
) -> list[Any]:
    """Redact each element of a sequence, preserving order and length.

    Args:
        values: List or tuple of values.
        field: Parent field name for deny/allow context.
        path: Key path to this sequence.

    Returns:
        A new list of redacted elements.
    """
    if field is not None and is_deny_field(field):
        # Entire cookie/header lists become opaque placeholders.
        return [REDACTED for _ in values]
    return [_redact_any(item, field=field, path=path) for item in values]


def _redact_any(
    value: Any,
    *,
    field: str | None = None,
    path: tuple[str, ...] = (),
) -> Any:
    """Redact an arbitrary JSON-like value.

    Args:
        value: Mapping, sequence, or scalar.
        field: Parent field name when nested under a mapping key.
        path: Key path for nested structures.

    Returns:
        Redacted structure or scalar.
    """
    if isinstance(value, Mapping):
        return redact_mapping(value, path=path)
    if isinstance(value, list | tuple):
        return _redact_sequence(value, field=field, path=path)
    return _redact_scalar_for_field(field, value)


def _input_type_is_sensitive(attrs: Mapping[str, Any]) -> bool:
    """Return whether DOM attributes describe a sensitive input control.

    Args:
        attrs: Element attribute mapping.

    Returns:
        True for password/hidden-credential style input types.
    """
    raw_type = attrs.get("type") or attrs.get("input_type") or attrs.get("inputType")
    if not isinstance(raw_type, str):
        return False
    return normalize_field_name(raw_type) in {"password", "hidden"}


def redact_browser_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Redact a browser / accessibility tree snapshot for audit or prompts.

    Walks common shapes (``elements``, ``nodes``, ``dom``, ``attributes``,
    ``attrs``) and applies field deny lists plus heuristics. Input ``value``
    attributes on password-like controls are always replaced with
    :data:`REDACTED`. Cookie and Authorization headers never survive.

    Args:
        state: Browser-state or DOM observation mapping.

    Returns:
        A new mapping safe to persist or send to a model.
    """
    return _redact_browser_node(state)


def _redact_browser_node(node: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively redact one browser-state node.

    Args:
        node: Single element or subtree mapping.

    Returns:
        Redacted copy of ``node``.
    """
    out: dict[str, Any] = {}
    attrs_key = None
    attrs_raw: Mapping[str, Any] | None = None
    for candidate in ("attributes", "attrs", "attr"):
        if candidate in node and isinstance(node[candidate], Mapping):
            attrs_key = candidate
            attrs_raw = node[candidate]
            break

    sensitive_input = bool(attrs_raw and _input_type_is_sensitive(attrs_raw))

    for key, raw in node.items():
        key_str = str(key)
        norm = normalize_field_name(key_str)

        if attrs_key is not None and key_str == attrs_key and attrs_raw is not None:
            out[key_str] = _redact_dom_attributes(attrs_raw, force_value=sensitive_input)
            continue

        if norm in {"children", "elements", "nodes", "subtree", "descendants"}:
            if isinstance(raw, list | tuple):
                out[key_str] = [
                    _redact_browser_node(child)
                    if isinstance(child, Mapping)
                    else _redact_any(child)
                    for child in raw
                ]
            elif isinstance(raw, Mapping):
                out[key_str] = _redact_browser_node(raw)
            else:
                out[key_str] = _redact_any(raw, field=key_str)
            continue

        if norm in {"dom", "tree", "root", "document", "snapshot", "a11y", "accessibility"}:
            if isinstance(raw, Mapping):
                out[key_str] = _redact_browser_node(raw)
            else:
                out[key_str] = _redact_any(raw, field=key_str)
            continue

        if norm == "value" and sensitive_input:
            out[key_str] = REDACTED
            continue

        if isinstance(raw, Mapping):
            out[key_str] = _redact_browser_node(raw)
        else:
            out[key_str] = _redact_any(raw, field=key_str)

    return out


def _redact_dom_attributes(
    attrs: Mapping[str, Any],
    *,
    force_value: bool = False,
) -> dict[str, Any]:
    """Redact a DOM attribute bag.

    Args:
        attrs: Attribute name → value mapping.
        force_value: When True, always redact ``value`` (password inputs).

    Returns:
        Redacted attribute mapping.
    """
    out: dict[str, Any] = {}
    for key, raw in attrs.items():
        key_str = str(key)
        norm = normalize_field_name(key_str)
        if force_value and norm == "value":
            out[key_str] = REDACTED
            continue
        if is_deny_field(key_str):
            out[key_str] = REDACTED
            continue
        out[key_str] = _redact_any(raw, field=key_str)
    return out


def redact_for_audit(payload: Any) -> Any:
    """Approved entry point for audit sinks and prompt assembly (T009+).

    Prefer this function over ad-hoc field stripping. Mappings and sequences
    are deep-redacted; strings use :func:`redact_text`; other scalars pass
    through unless the caller wraps them in a deny-named field via
    :func:`redact_mapping`.

    Args:
        payload: Event payload, model trace fragment, or browser-state blob.

    Returns:
        Structure safe to persist or include in model prompts.
    """
    if isinstance(payload, Mapping):
        # Prefer browser-aware walk when the payload looks like a state tree.
        keys = {normalize_field_name(str(k)) for k in payload}
        if keys & {"elements", "nodes", "dom", "attributes", "attrs", "a11y"}:
            return redact_browser_state(payload)
        return redact_mapping(payload)
    if isinstance(payload, list | tuple):
        return [_redact_any(item) for item in payload]
    if isinstance(payload, str):
        return redact_text(payload)
    return payload
