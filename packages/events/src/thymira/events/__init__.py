"""Event API building blocks: hashing, source scrubbing, export redaction and hash-chained logs.

Ported from the thesis prototype's trace (``tracing.py`` + ``utils.py``) onto the typed
``thymira.schemas.Event`` contract. Owner (MVP roadmap): P1.
"""

from thymira.events.file_access import (
    StoragePermissionError,
    StoragePermissionEvidence,
    create_private_file,
    secure_directory,
    secure_file,
)
from thymira.events.hashing import (
    PDF_EXPORT_EXECUTION_VERSION,
    canonical_json,
    hash_authorization_context,
    hash_event,
    hash_pdf_export_execution,
    sha256_bytes,
    sha256_file,
    sha256_text,
)
from thymira.events.log import (
    EventFormatError,
    EventLog,
    InMemoryEventLog,
    JsonlEventLog,
    MalformedEventFormatError,
    MissingEventFormatError,
    UnknownEventTypeError,
    UnsupportedEventFormatError,
    VerificationResult,
    deserialize_event,
    read_events,
    validate_event_type,
    verify_events,
    verify_log,
)
from thymira.events.redaction import (
    REDACTION_PATTERNS,
    ExportRedactionError,
    credential_values,
    is_credential_environment_name,
    redact,
    redact_export,
    redact_value,
    scrub_credentials,
    scrub_credentials_value,
)
from thymira.events.surface import (
    SHADOWED_SEQS_KEY,
    current_surface,
    derive_surface,
    shadowed_seqs,
)
from thymira.schemas import GENESIS_HASH

__all__ = [
    "GENESIS_HASH",
    "PDF_EXPORT_EXECUTION_VERSION",
    "REDACTION_PATTERNS",
    "SHADOWED_SEQS_KEY",
    "EventFormatError",
    "EventLog",
    "ExportRedactionError",
    "InMemoryEventLog",
    "JsonlEventLog",
    "MalformedEventFormatError",
    "MissingEventFormatError",
    "StoragePermissionError",
    "StoragePermissionEvidence",
    "UnknownEventTypeError",
    "UnsupportedEventFormatError",
    "VerificationResult",
    "canonical_json",
    "create_private_file",
    "credential_values",
    "current_surface",
    "derive_surface",
    "deserialize_event",
    "hash_authorization_context",
    "hash_event",
    "hash_pdf_export_execution",
    "is_credential_environment_name",
    "read_events",
    "redact",
    "redact_export",
    "redact_value",
    "scrub_credentials",
    "scrub_credentials_value",
    "secure_directory",
    "secure_file",
    "sha256_bytes",
    "sha256_file",
    "sha256_text",
    "shadowed_seqs",
    "validate_event_type",
    "verify_events",
    "verify_log",
]
