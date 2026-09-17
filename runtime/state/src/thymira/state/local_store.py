"""Filesystem-backed :class:`~thymira.state.artifact_store.ArtifactStore`.

Everything a run produces is written under a single ``root`` directory and recorded in
``<root>/manifest.json`` (logical name -> serialised :class:`~thymira.schemas.Artifact`). The
manifest lets the runtime detect tampering (``verify``), invalidate outputs whose inputs changed
(``invalidate``) and keep superseded revisions instead of deleting them (``preserve_history``).
Reopening the store on an existing ``root`` reloads the manifest, so it survives process restarts.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import RLock
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from thymira.events import (
    StoragePermissionError,
    StoragePermissionEvidence,
    canonical_json,
    create_private_file,
    secure_directory,
    secure_file,
    sha256_file,
)
from thymira.schemas import Artifact, ArtifactKind, new_id, utc_now
from thymira.state._atomic import CrossProcessFileLock, fsync_directory, replace_with_retry
from thymira.state.artifact_store import ArtifactWrite

if TYPE_CHECKING:
    from collections.abc import Iterable

_MANIFEST_NAME = "manifest.json"
_MANIFEST_TMP_NAME = f"{_MANIFEST_NAME}.tmp"
_MANIFEST_LOCK_NAME = ".manifest.lock"
_HISTORY_DIR = ".history"
_BATCH_DIR = ".batches"
# A published batch object lives at ``<root>/.batches/<batch id>/<object name>``. Windows refuses
# any path of 260 characters or more, and an artifact store rooted under a pytest ``--basetemp``
# already spends around 150 of them before the store writes anything, so that fixed tail is a
# budget, not a detail: the full ``uuid4().hex`` batch id plus an ``objects/`` segment plus a
# 64-character digest cost 119 characters and pushed real Runs past the limit, where dataset
# registration failed with ``FileNotFoundError`` on the staged object. The same reasoning already
# shapes ``_next_archive_name`` (a 12-character digest), ``LocalCheckpointRepository`` and the
# sandbox private-storage prefix. 64 bits of batch id make a collision within one store root
# unreachable in practice, and ``mkdir(exist_ok=False)`` refuses one outright rather than
# publishing over it; 128 bits of digest keep the object name content-derived, while the
# authoritative full ``sha256`` stays in the manifest, which is what ``verify()`` recomputes.
_BATCH_ID_LENGTH = 16
_OBJECT_NAME_DIGEST_LENGTH = 32
# Windows resolves these to a device in every directory, whatever the extension: a write to ``NUL``
# is discarded and reads back empty, so the store would record the digest of an empty file for
# content it never kept. They are refused on every platform, because a run must produce the same
# evidence wherever it executes.
_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{digit}" for digit in "123456789"}
    | {f"lpt{digit}" for digit in "123456789"}
)
# The store's own bookkeeping shares the artifact directory with the run's artifacts. An artifact
# named after the manifest (or its ``manifest.json.tmp`` scratch file) would overwrite the record
# control ``a5_artifact_integrity`` verifies against — the ``.tmp`` variant is outright data loss —
# and one landing in ``.history`` would clobber an archived revision. They are reserved case-folded
# and refused on the write paths, the same way a device name is; internal archiving reaches
# ``.history`` through :func:`_canonical_key`, which does not apply this refusal.
_RESERVED_STORE_NAMES = frozenset(
    {
        _MANIFEST_NAME.casefold(),
        _MANIFEST_TMP_NAME.casefold(),
        _MANIFEST_LOCK_NAME.casefold(),
        _BATCH_DIR.casefold(),
    }
)


def _canonical_key(name: str) -> str:
    """Return the canonical store-relative key for ``name``.

    One logical artifact must have exactly one key and one file: ``a.json`` and ``./a.json`` are
    the same artifact, and neither may resolve outside the store root. Escaping names are refused
    rather than normalised away, because a caller asking to write outside the store is a bug the
    audit trail should surface, not a request to satisfy quietly.

    This is the total canonicaliser used for the store's own internal keys (``.history`` archive
    paths, and every entry reloaded from the manifest). Incoming writes go through
    :func:`_store_key`, which additionally refuses the store's reserved bookkeeping names.

    Raises:
        ValueError: if ``name`` is blank, rooted (POSIX absolute, Windows drive or UNC share),
            contains a ``..`` component, names a Windows device, or has a component ending in a
            dot or a space.
    """
    if not name.strip():
        msg = "artifact name must not be blank"
        raise ValueError(msg)
    # PureWindowsPath recognises drives, UNC shares and both separators on every platform, so a
    # Windows-shaped name is refused when the runtime happens to be running on Linux too.
    if PureWindowsPath(name).anchor:
        msg = f"artifact name must be relative to the store root: {name!r}"
        raise ValueError(msg)
    parts = [part for part in PurePosixPath(name.replace("\\", "/")).parts if part != "."]
    if any(part == ".." for part in parts):
        msg = f"artifact name must not escape the store root: {name!r}"
        raise ValueError(msg)
    if not parts:
        msg = "artifact name must not be blank"
        raise ValueError(msg)
    for part in parts:
        # Windows strips trailing dots and spaces, so ``a.json`` and ``a.json.`` open one file: the
        # same one-file-under-two-keys bug a device name causes, by a different route.
        if part != part.rstrip(". "):
            msg = f"artifact name component must not end in a dot or a space: {name!r}"
            raise ValueError(msg)
        if ":" in part:
            # On NTFS ``manifest.json::$DATA`` and ``manifest.json`` are the same file reached by
            # two names, so a colon defeats every name-based rule below it -- including the
            # reserved-name refusal in :func:`_store_key`. A colon is not a character any artifact
            # name in this system needs, so it is refused outright rather than stripped.
            msg = f"artifact name component must not contain a colon: {name!r}"
            raise ValueError(msg)
        if part.partition(".")[0].casefold() in _RESERVED_STEMS:
            msg = f"artifact name must not be a reserved device name: {name!r}"
            raise ValueError(msg)
    return PurePosixPath(*parts).as_posix()


def _object_name(data: bytes) -> str:
    """Return the immutable batch-object filename for ``data``.

    The name is derived from the content, so republishing the same bytes never overwrites a
    different object, and it is fixed-width so the published path stays inside the Windows
    ``MAX_PATH`` budget described beside :data:`_BATCH_ID_LENGTH`.
    """
    return f"{hashlib.sha256(data).hexdigest()[:_OBJECT_NAME_DIGEST_LENGTH]}.blob"


def _store_key(name: str) -> str:
    """Return the canonical key for an incoming write, refusing the store's own bookkeeping.

    Beyond the containment and device rules of :func:`_canonical_key`, a caller may not write over
    the manifest (``manifest.json`` or its ``manifest.json.tmp`` scratch file), its lock
    (``.manifest.lock``), nor into the ``.history`` archive: those files are the run's evidence or
    writer coordination, and a caller overwriting them is a bug the audit trail should surface,
    not a request to satisfy quietly. These names are reserved case-folded, following the
    reserved-device-name precedent. Only a top-level ``.history`` is reserved —
    ``reports/.history/x`` is an ordinary artifact in its own subtree.

    Raises:
        ValueError: for every reason :func:`_canonical_key` raises, and additionally when ``name``
            resolves to the store manifest or into the ``.history`` archive directory.
    """
    key = _canonical_key(name)
    if key.casefold() in _RESERVED_STORE_NAMES:
        msg = f"artifact name is reserved by the store: {name!r}"
        raise ValueError(msg)
    if PurePosixPath(key).parts[0].casefold() == _HISTORY_DIR.casefold():
        msg = f"artifact name must not write into the store's history: {name!r}"
        raise ValueError(msg)
    return key


def canonical_artifact_name(name: str) -> str:
    """Return the canonical writable artifact name, applying every reserved-name guard."""
    return _store_key(name)


def _query_key(name: str) -> str | None:
    """Return the canonical key for ``name``, or ``None`` if the store would refuse to write it.

    Queries are total; writes are not. Asking whether an impossible name is stored has a correct
    answer — nothing is — and the callers that ask hold untrusted input: MIRA recomputes a run
    from its event log, so one hostile ``artifact.created`` payload must raise a *finding*, not
    an exception that suppresses the whole audit. :func:`_store_key` still refuses on every write
    path, which is where a caller asking to escape the root is a bug worth surfacing loudly.
    """
    try:
        return _store_key(name)
    except ValueError:
        return None


def _refuse_key_collision(key: str, existing_keys: Iterable[str]) -> None:
    """Refuse ``key`` when a case-insensitive filesystem cannot tell it from an existing key.

    NTFS and APFS resolve ``metrics.json`` and ``Metrics.json`` to one file. Accepting both would
    give that one file two manifest entries: the second write would overwrite the first without
    archiving it — defeating ``preserve_history`` — and :meth:`LocalArtifactStore.verify` would
    then report both entries as modified, raising an integrity alarm about a collision the store
    caused itself. The clash is refused on every platform, so a run's evidence does not depend on
    where it happened to execute.

    Raises:
        ValueError: if a different key differing only in case is already registered.
    """
    folded = key.casefold()
    for existing in existing_keys:
        if existing != key and existing.casefold() == folded:
            msg = f"artifact name collides with {existing!r} on a case-insensitive store: {key!r}"
            raise ValueError(msg)


class LocalArtifactStore:
    """Store artifacts as files under ``root``, tracked by a JSON manifest.

    Args:
        root: Directory that holds the artifacts, their ``.history`` archive and the manifest.
            Created if missing.
        run_id: Id of the run these artifacts belong to; stamped on every recorded artifact.
    """

    def __init__(self, root: Path, run_id: str) -> None:
        self._root = Path(root)
        self._permission_evidence = secure_directory(self._root)
        self._run_id = run_id
        self._manifest_path = self._root / _MANIFEST_NAME
        self._manifest_lock_path = self._root / _MANIFEST_LOCK_NAME
        self._manifest: Mapping[str, Artifact] = MappingProxyType({})
        self._guard = RLock()
        if self._manifest_path.exists():
            secure_file(self._manifest_path)
            self._load_manifest()

    @property
    def permission_evidence(self) -> StoragePermissionEvidence:
        """Return the verified OS permission evidence for this artifact store's root."""
        return self._permission_evidence

    # -- saving ----------------------------------------------------------------------------------

    def save_bytes(
        self,
        name: str,
        data: bytes,
        *,
        produced_by: str,
        kind: ArtifactKind = ArtifactKind.OTHER,
        media_type: str | None = None,
        preserve_history: bool = True,
    ) -> Artifact:
        """Publish one attachment through the immutable batch protocol.

        Raises:
            ValueError: if ``name`` escapes the store root, names a Windows device, or writes over
                the store's own manifest or ``.history`` archive (see :func:`_store_key`), or
                collides with a registered name differing only in case.
        """
        return self.save_batch(
            {name: data},
            produced_by=produced_by,
            kind=kind,
            media_type=media_type,
            preserve_history=preserve_history,
        )[0]

    def save_batch(
        self,
        attachments: Mapping[str, bytes],
        *,
        produced_by: str,
        kind: ArtifactKind = ArtifactKind.OTHER,
        media_type: str | None = None,
        preserve_history: bool = True,
    ) -> tuple[Artifact, ...]:
        """Publish one homogeneous attachment batch through one manifest replacement.

        This convenience API assigns the same kind and media type to every member. Use
        :meth:`save_artifact_batch` when members need different metadata or lineage.

        Raises:
            TypeError: if ``attachments`` or one of its values has the wrong type.
            ValueError: if any name is invalid, duplicated or collides with a registered name.
            OSError: if staging or durable publication fails.
        """
        if not isinstance(attachments, Mapping):
            raise TypeError("attachments must be a mapping of names to bytes")
        return self.save_artifact_batch(
            tuple(
                ArtifactWrite(name=name, data=data, kind=kind, media_type=media_type)
                for name, data in attachments.items()
            ),
            produced_by=produced_by,
            preserve_history=preserve_history,
        )

    def save_artifact_batch(
        self,
        writes: Sequence[ArtifactWrite],
        *,
        produced_by: str,
        preserve_history: bool = True,
    ) -> tuple[Artifact, ...]:
        """Publish heterogeneous artifacts and their lineage through one manifest replacement.

        Content is first written to immutable, content-addressed files under ``.batches``. A
        reader continues to see the previous manifest until every member has been written,
        flushed, read back and hashed. The only publication step is the atomic manifest replace;
        an interrupted or invalid batch therefore cannot become a partially visible batch.

        A member's ``input_artifact_names`` may refer to another member of this batch. All output
        ids are allocated before any bytes are staged, so that reference becomes the exact new
        artifact id in the same candidate manifest rather than whichever revision a later lookup
        happens to find.

        Raises:
            TypeError: if ``writes`` or one of its members has the wrong type.
            ValueError: if any name or lineage reference is invalid, duplicated or colliding.
            OSError: if staging or durable publication fails.
        """
        with self._writer():
            normalized = _validate_artifact_writes(writes, self._manifest)
            if not normalized:
                return ()

            batch_id = uuid.uuid4().hex[:_BATCH_ID_LENGTH]
            result = _build_batch_artifacts(
                normalized,
                existing=self._manifest,
                batch_id=batch_id,
                run_id=self._run_id,
                produced_by=produced_by,
            )
            batch_root = self._contained(f"{_BATCH_DIR}/{batch_id}")
            batch_root.mkdir(parents=True, exist_ok=False)
            # The object layout and its MAX_PATH budget stay as published; the explicit
            # private-ACL calls harden both new directories before any object is staged.
            secure_directory(batch_root.parent)
            secure_directory(batch_root)
            committed = False
            manifest_attempted = False
            try:
                for data in dict.fromkeys(write.data for write in normalized):
                    self._write_staged_bytes(batch_root / _object_name(data), data)
                # Persist the directory entries before the manifest can name these objects. The
                # manifest is the publication point, so a crash after it is replaced must leave
                # every referenced parent directory durable as well as every object file.
                fsync_directory(batch_root)
                fsync_directory(self._root / _BATCH_DIR)

                candidate = dict(self._manifest)
                for write, artifact in zip(normalized, result, strict=True):
                    if preserve_history:
                        self._archive_record(candidate, write.name)
                    candidate[write.name] = artifact
                # Once manifest publication starts, retain the batch directory on every failure:
                # an atomic replace may already have happened before an injected or I/O error is
                # raised. An orphaned invisible batch is safe; deleting a published object is not.
                manifest_attempted = True
                try:
                    self._write_manifest(candidate)
                except BaseException:
                    # The replace may have succeeded before a fault in read-back or a fault
                    # injector raised. A durable exact match is already the commit point: report
                    # that batch as successful, because returning a failure would let callers
                    # retry a publication that readers can already observe. Never delete objects
                    # named by that committed manifest.
                    if not self._manifest_matches(candidate):
                        raise
                committed = True
                self._set_manifest(candidate)
            except BaseException:
                if not committed and not manifest_attempted:
                    shutil.rmtree(batch_root, ignore_errors=True)
                raise
            return result

    def save_text(
        self,
        name: str,
        text: str,
        *,
        produced_by: str,
        kind: ArtifactKind = ArtifactKind.OTHER,
        media_type: str | None = None,
        preserve_history: bool = True,
    ) -> Artifact:
        """Persist ``text`` as UTF-8 bytes."""
        return self.save_bytes(
            name,
            text.encode("utf-8"),
            produced_by=produced_by,
            kind=kind,
            media_type=media_type,
            preserve_history=preserve_history,
        )

    def save_json(
        self,
        name: str,
        data: Any,
        *,
        produced_by: str,
        kind: ArtifactKind = ArtifactKind.OTHER,
        media_type: str | None = None,
        preserve_history: bool = True,
    ) -> Artifact:
        """Persist ``data`` as canonical JSON so equal values hash identically."""
        return self.save_bytes(
            name,
            canonical_json(data).encode("utf-8"),
            produced_by=produced_by,
            kind=kind,
            media_type=media_type,
            preserve_history=preserve_history,
        )

    # -- loading ---------------------------------------------------------------------------------

    def load_bytes(self, name: str) -> bytes:
        """Return the raw bytes stored under ``name``."""
        key = _store_key(name)
        artifact = self._manifest.get(key)
        path = self._contained(artifact.uri if artifact is not None else key)
        if path.exists():
            secure_file(path)
        return path.read_bytes()

    def load_bytes_bounded(self, name: str, max_bytes: int) -> bytes:
        """Read at most ``max_bytes + 1`` bytes and refuse an oversized artifact."""
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        key = _store_key(name)
        artifact = self._manifest.get(key)
        path = self._contained(artifact.uri if artifact is not None else key)
        if path.exists():
            secure_file(path)
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"artifact {name!r} exceeds the {max_bytes}-byte read bound")
        return data

    def load_text(self, name: str) -> str:
        """Return the UTF-8 text stored under ``name``."""
        return self.load_bytes(name).decode("utf-8")

    def load_json(self, name: str) -> Any:
        """Return the JSON value stored under ``name``."""
        return json.loads(self.load_bytes(name))

    # -- inspection ------------------------------------------------------------------------------

    def exists(self, name: str) -> bool:
        """Return whether ``name`` is registered, still valid and present on disk.

        A name the store would refuse to write answers ``False`` rather than raising: see
        :func:`_query_key`.
        """
        key = _query_key(name)
        if key is None:
            return False
        artifact = self._manifest.get(key)
        if artifact is None:
            return False
        path = self._contained(artifact.uri)
        if path.exists():
            secure_file(path)
        return artifact.valid and path.exists()

    def get(self, name: str) -> Artifact | None:
        """Return the artifact recorded under ``name``, or ``None`` if it is unknown.

        A name the store would refuse to write answers ``None`` rather than raising: see
        :func:`_query_key`.
        """
        key = _query_key(name)
        return None if key is None else self._manifest.get(key)

    def list_active(self) -> list[Artifact]:
        """Return the artifacts that are valid and present on disk."""
        active: list[Artifact] = []
        for artifact in self._manifest.values():
            path = self._contained(artifact.uri)
            if path.exists():
                secure_file(path)
            if artifact.valid and path.exists():
                active.append(artifact)
        return active

    def manifest(self) -> dict[str, Artifact]:
        """Return a copy of the full manifest, active and archived entries alike."""
        return dict(self._manifest)

    def verify(self) -> list[str]:
        """Return a list of problems (missing or modified files); empty when the store is intact."""
        problems: list[str] = []
        for name, artifact in self._manifest.items():
            # Keys and uris are canonical and contained (checked on save and on reload), so this
            # can never follow a manifest entry that points outside the root.
            path = self._contained(artifact.uri)
            if not path.exists():
                problems.append(f"missing artifact: {name}")
            else:
                try:
                    secure_file(path)
                except StoragePermissionError:
                    problems.append(f"insecure artifact: {name}")
                else:
                    if sha256_file(path) != artifact.sha256:
                        problems.append(f"modified artifact: {name}")
        return problems

    # -- lineage and invalidation ----------------------------------------------------------------

    def set_lineage(
        self,
        name: str,
        *,
        input_artifact_ids: Sequence[str],
        execution_key: str,
    ) -> Artifact:
        """Attach reproducibility lineage to ``name`` and return the updated artifact.

        ``name`` is canonicalised exactly as :meth:`save_bytes` canonicalises it, so the spelling
        that saved an artifact is the spelling that can amend it.

        Raises:
            KeyError: if no artifact is registered under ``name``.
        """
        with self._writer():
            key = _store_key(name)
            existing = self._manifest.get(key)
            if existing is None:
                msg = f"artifact not registered: {name}"
                raise KeyError(msg)
            updated = existing.model_copy(
                update={
                    "input_artifact_ids": tuple(input_artifact_ids),
                    "execution_key": execution_key,
                }
            )
            candidate = dict(self._manifest)
            candidate[key] = updated
            self._write_manifest(candidate)
            self._set_manifest(candidate)
        return updated

    def invalidate(self, names: Iterable[str], reason: str) -> list[str]:
        """Mark each still-valid name invalid with ``reason``; return the keys actually changed.

        Names are canonicalised as :meth:`save_bytes` canonicalises them, so invalidating
        ``./metrics.json`` invalidates the artifact saved as ``metrics.json`` instead of matching
        nothing.

        Raises:
            TypeError: if ``names`` is a single ``str``. A string is iterable, so it would be
                consumed character by character and invalidate nothing while reporting success —
                and silence is the worst outcome for a governance action.
        """
        if isinstance(names, str):
            msg = f"invalidate() takes a collection of names, not one string: {names!r}"
            raise TypeError(msg)
        with self._writer():
            changed: list[str] = []
            timestamp = utc_now()
            candidate = dict(self._manifest)
            for key in sorted(_store_key(name) for name in names):
                existing = self._manifest.get(key)
                if existing is None or not existing.valid:
                    continue
                candidate[key] = existing.model_copy(
                    update={
                        "valid": False,
                        "invalidated_reason": reason,
                        "invalidated_at": timestamp,
                    }
                )
                changed.append(key)
            if changed:
                self._write_manifest(candidate)
                self._set_manifest(candidate)
        return changed

    # -- internals -------------------------------------------------------------------------------

    def _archive_record(self, manifest: dict[str, Artifact], name: str) -> None:
        """Archive one current revision in an unpublished manifest candidate."""
        existing = manifest.get(name)
        if existing is None:
            return
        path = self._contained(existing.uri)
        if not path.exists():
            return
        secure_file(path)
        archived_name = self._next_archive_name(name, existing.sha256, manifest)
        # Every other write in this class resolves through `_contained`, which follows symlinks:
        # a link planted at `.history/<stem>` would otherwise redirect the archived copy outside
        # the store while the manifest went on claiming the revision was held inside it.
        archived_path = self._contained(archived_name)
        # `create_private_file` secures the parent directory and the file itself, and refuses a
        # pre-existing target -- `_next_archive_name` only ever returns a free key.
        create_private_file(archived_path, path.read_bytes())
        # The archive is referenced by the candidate manifest below. Flush both its bytes and the
        # directory entries before publication, otherwise a power loss after the manifest replace
        # could leave a referenced historical revision missing on reopen.
        fsync_directory(archived_path.parent)
        fsync_directory(archived_path.parent.parent)
        manifest[archived_name] = existing.model_copy(
            update={
                "uri": archived_name,
                "valid": False,
                "invalidated_reason": f"superseded by a new revision of {name}",
                "invalidated_at": utc_now(),
            }
        )

    def _next_archive_name(
        self, name: str, sha256: str, manifest: Mapping[str, Artifact] | None = None
    ) -> str:
        """Return a free ``.history`` key for a revision of ``name`` with the given digest.

        The stem is sanitised the way an incoming name is, and the result is canonicalised through
        :func:`_canonical_key` before it is returned. That canonicaliser, not :func:`_store_key`,
        is used precisely because the key lives under ``.history``, which the write paths reserve.
        An archive key the store cannot read back is worse than a lost revision: it is written to
        ``manifest.json``, and every later open of the store — the whole run's evidence, not just
        this artifact — then raises on it.
        """
        stem = Path(name).stem.replace(" ", "-").rstrip(". ") or "artifact"
        suffix = Path(name).suffix
        counter = 1
        while True:
            candidate = f"{_HISTORY_DIR}/{stem}/{sha256[:12]}-{counter}{suffix}"
            existing = self._manifest if manifest is None else manifest
            if candidate not in existing:
                candidate_path = self._contained(candidate)
                if candidate_path.exists():
                    secure_file(candidate_path)
                else:
                    return _canonical_key(candidate)
            counter += 1

    def _contained(self, key: str) -> Path:
        """Return the path for a canonical ``key``, proving it stays inside the root.

        :func:`_store_key` already rejects escaping names lexically; this resolves symlinks too,
        so a link planted inside the store cannot redirect a write outside it.

        Raises:
            ValueError: if the resolved path falls outside the store root.
        """
        path = self._root / key
        if not path.resolve().is_relative_to(self._root.resolve()):
            msg = f"artifact path escapes the store root: {key!r}"
            raise ValueError(msg)
        return path

    def _load_manifest(self) -> None:
        """Parse the on-disk manifest into ``Artifact`` records, refusing an escaping entry.

        The manifest is ordinary JSON on disk and nothing chains it, so it is treated as
        untrusted input: a hand-edited entry whose key or ``uri`` leaves the root would otherwise
        make :meth:`verify` hash a file the store does not own.

        Raises:
            ValueError: if an entry escapes the root, has a non-canonical URI, or two entries
                differ only in case.
        """
        raw: dict[str, Any] = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        manifest: dict[str, Artifact] = {}
        for name, entry in raw.items():
            # A reloaded manifest legitimately holds ``.history`` archive keys, which the write
            # paths reserve; canonicalise them without applying that write-only refusal.
            key = _canonical_key(name)
            artifact = Artifact.model_validate(entry)
            uri = _canonical_key(artifact.uri)
            if uri != artifact.uri:
                msg = f"manifest entry {name!r} has a non-canonical URI: {artifact.uri!r}"
                raise ValueError(msg)
            self._contained(uri)
            _refuse_key_collision(key, manifest)
            manifest[key] = artifact
        self._set_manifest(manifest)

    def _set_manifest(self, manifest: Mapping[str, Artifact]) -> None:
        """Publish an immutable in-process manifest snapshot to concurrent readers."""
        self._manifest = MappingProxyType(dict(manifest))

    def _write_manifest(self, manifest: Mapping[str, Artifact] | None = None) -> None:
        """Rewrite a complete manifest atomically with flush and filesystem durability."""
        values = self._manifest if manifest is None else manifest
        payload = {name: artifact.to_json_dict() for name, artifact in values.items()}
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        tmp_path = self._manifest_path.with_name(_MANIFEST_TMP_NAME)
        if tmp_path.exists():
            secure_file(tmp_path)
            with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            secure_file(tmp_path)
        else:
            create_private_file(tmp_path, text.encode("utf-8"))
        try:
            replace_with_retry(tmp_path, self._manifest_path)
            secure_file(self._manifest_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
        # ``create_private_file`` already flushed and fsynced the writable temporary manifest
        # before replacement.  The atomic replace plus the directory flush below make the
        # published name durable; opening the replaced manifest read-only and calling ``fsync``
        # is rejected by Windows with ``EBADF``.
        if json.loads(self._manifest_path.read_text(encoding="utf-8")) != json.loads(text):
            raise OSError("artifact manifest read-back differs from the committed snapshot")
        fsync_directory(self._root)

    def _manifest_matches(self, manifest: Mapping[str, Artifact]) -> bool:
        """Return whether the durable manifest already contains ``manifest`` exactly."""
        try:
            payload = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        expected = {name: artifact.to_json_dict() for name, artifact in manifest.items()}
        return payload == expected

    @contextmanager
    def _writer(self) -> Iterator[None]:
        """Serialise one complete manifest transaction across threads and processes.

        The in-memory snapshot may have been opened before another process committed. Reloading it
        only after taking the OS lock is the compare-and-swap step: the candidate is based on the
        exact manifest currently protected by the lock, so a successful batch cannot overwrite a
        concurrent producer's publication.
        """
        with self._guard, CrossProcessFileLock(self._manifest_lock_path):
            if self._manifest_path.exists():
                self._load_manifest()
            else:
                self._set_manifest({})
            yield

    def _write_staged_bytes(self, target: Path, data: bytes) -> None:
        """Write and verify one immutable, content-addressed batch object.

        Raises:
            OSError: if ``target`` is already staged -- the batch directory is created for one
                batch alone, so an existing object name means two of its members share a digest
                prefix without sharing bytes, and publishing would hand one member the other's
                content -- or if the object does not read back as the bytes it was given.
        """
        if target.exists():
            raise OSError(f"staged attachment name is already taken: {target.name!r}")
        # The batch directory is unique, so a deterministic scratch name is safe, and it is no
        # longer than the object it stages, which keeps staging inside the same MAX_PATH budget.
        # `create_private_file` secures the batch directory and the object's own permissions and
        # fsyncs its writable descriptor before the atomic replace.
        temporary = target.with_name(f".{target.stem}.tmp")
        create_private_file(temporary, data)
        try:
            replace_with_retry(temporary, target)
            secure_file(target)
        finally:
            if temporary.exists():
                temporary.unlink()
        readback = target.read_bytes()
        # The name carries a fixed-width prefix of the digest, not the whole of it; the bytes
        # themselves are still compared in full, so a truncated name never weakens the proof.
        if readback != data or not hashlib.sha256(readback).hexdigest().startswith(target.stem):
            raise OSError(f"staged attachment read-back failed for {target.name!r}")
        fsync_directory(target.parent)


def _validate_artifact_writes(
    writes: Sequence[ArtifactWrite], existing: Mapping[str, Artifact]
) -> tuple[ArtifactWrite, ...]:
    """Validate and canonicalize every heterogeneous write before staging any bytes."""
    if isinstance(writes, (str, bytes, bytearray)) or not isinstance(writes, Sequence):
        raise TypeError("writes must be a sequence of ArtifactWrite values")
    normalized: list[ArtifactWrite] = []
    folded: dict[str, str] = {}
    for position, write in enumerate(writes):
        normalized.append(
            _normalize_artifact_write(
                write,
                position=position,
                existing=existing,
                folded=folded,
            )
        )

    batch_names = {write.name for write in normalized}
    active_by_id = {artifact.id for artifact in existing.values() if artifact.valid}
    for write in normalized:
        for input_name in write.input_artifact_names:
            prior = existing.get(input_name)
            if input_name not in batch_names and (prior is None or not prior.valid):
                raise ValueError(
                    f"attachment {write.name!r} input artifact is not active: {input_name!r}"
                )
        unknown_ids = set(write.input_artifact_ids).difference(active_by_id)
        if unknown_ids:
            raise ValueError(f"attachment {write.name!r} has unregistered input artifact ids")
    _refuse_batch_lineage_cycles(normalized)
    return tuple(normalized)


def _normalize_artifact_write(
    write: object,
    *,
    position: int,
    existing: Mapping[str, Artifact],
    folded: dict[str, str],
) -> ArtifactWrite:
    """Validate one write's fields and return its canonical-name representation."""
    if not isinstance(write, ArtifactWrite):
        raise TypeError(f"artifact write at position {position} must be an ArtifactWrite")
    _validate_artifact_write_fields(write)
    key = _store_key(write.name)
    prior = folded.get(key.casefold())
    if prior is not None:
        raise ValueError(f"attachment names collide: {prior!r} and {write.name!r}")
    folded[key.casefold()] = key
    _refuse_key_collision(key, existing)
    input_names = tuple(_store_key(name) for name in write.input_artifact_names)
    if len(input_names) != len(set(input_names)):
        raise ValueError(f"attachment {write.name!r} repeats an input artifact name")
    if key in input_names:
        raise ValueError(f"attachment {write.name!r} cannot depend on itself")
    return ArtifactWrite(
        name=key,
        data=write.data,
        kind=write.kind,
        media_type=write.media_type,
        input_artifact_ids=tuple(dict.fromkeys(write.input_artifact_ids)),
        input_artifact_names=input_names,
        execution_key=write.execution_key,
    )


def _validate_artifact_write_fields(write: ArtifactWrite) -> None:
    """Validate the runtime types of one in-process artifact write command."""
    if not isinstance(write.name, str):
        raise TypeError("attachment names must be strings")
    if not isinstance(write.data, bytes):
        raise TypeError(f"attachment {write.name!r} data must be bytes")
    if not isinstance(write.kind, ArtifactKind):
        raise TypeError(f"attachment {write.name!r} kind must be an ArtifactKind")
    if write.media_type is not None and not isinstance(write.media_type, str):
        raise TypeError(f"attachment {write.name!r} media_type must be a string or None")
    if not isinstance(write.input_artifact_ids, tuple) or not all(
        isinstance(value, str) and value for value in write.input_artifact_ids
    ):
        raise TypeError(f"attachment {write.name!r} input_artifact_ids must be non-empty strings")
    if not isinstance(write.input_artifact_names, tuple) or not all(
        isinstance(value, str) for value in write.input_artifact_names
    ):
        raise TypeError(f"attachment {write.name!r} input_artifact_names must be strings")
    if write.execution_key is not None and not isinstance(write.execution_key, str):
        raise TypeError(f"attachment {write.name!r} execution_key must be a string or None")


def _refuse_batch_lineage_cycles(writes: Sequence[ArtifactWrite]) -> None:
    """Reject cycles among same-batch name references before allocating or writing anything."""
    batch_names = {write.name for write in writes}
    dependencies = {
        write.name: set(write.input_artifact_names).intersection(batch_names) for write in writes
    }
    remaining = set(batch_names)
    while remaining:
        leaves = {name for name in remaining if not dependencies[name].intersection(remaining)}
        if not leaves:
            raise ValueError("artifact batch lineage contains a cycle")
        remaining.difference_update(leaves)


def _build_batch_artifacts(
    writes: Sequence[ArtifactWrite],
    *,
    existing: Mapping[str, Artifact],
    batch_id: str,
    run_id: str,
    produced_by: str,
) -> tuple[Artifact, ...]:
    """Build the complete candidate records, resolving same-batch lineage by allocated id."""
    artifact_ids = {write.name: new_id("artifact") for write in writes}
    result: list[Artifact] = []
    for write in writes:
        previous = existing.get(write.name)
        explicit_lineage = bool(
            write.input_artifact_ids
            or write.input_artifact_names
            or write.execution_key is not None
        )
        inputs = list(write.input_artifact_ids)
        for input_name in write.input_artifact_names:
            input_id = artifact_ids.get(input_name)
            if input_id is None:
                input_id = existing[input_name].id
            inputs.append(input_id)
        execution_key = write.execution_key
        if not explicit_lineage and previous is not None and previous.valid:
            inputs.append(previous.id)
            execution_key = f"supersedes:{previous.id}"
        result.append(
            Artifact(
                id=artifact_ids[write.name],
                run_id=run_id,
                name=write.name,
                kind=write.kind,
                uri=PurePosixPath(_BATCH_DIR, batch_id, _object_name(write.data)).as_posix(),
                sha256=hashlib.sha256(write.data).hexdigest(),
                size_bytes=len(write.data),
                media_type=write.media_type,
                produced_by=produced_by,
                input_artifact_ids=tuple(dict.fromkeys(inputs)),
                execution_key=execution_key,
            )
        )
    return tuple(result)
