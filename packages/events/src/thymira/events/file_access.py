"""Platform evidence for the private filesystem boundary of canonical local event storage.

This primitive lives with :mod:`thymira.events` because the package owns the canonical JSONL
writer. Runtime state reuses it for projections, artifacts and checkpoints; keeping the check here
means a direct :class:`~thymira.events.JsonlEventLog` cannot create an unprotected raw log.
"""

from __future__ import annotations

import ctypes
import os
import stat
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


class StoragePermissionError(PermissionError):
    """Raised when local Run storage cannot be made private and verified."""


@dataclass(frozen=True, slots=True)
class StoragePermissionEvidence:
    """Evidence that one local storage path has a restrictive ACL or mode."""

    path: Path
    platform: str
    detail: str


def secure_directory(path: Path) -> StoragePermissionEvidence:
    """Apply and verify restrictive permissions to a local storage directory."""
    target = Path(path)
    _refuse_reparse_path(target)
    try:
        target.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            return _secure_windows(target)
        target.chmod(_MODE_DIRECTORY)
        mode = stat.S_IMODE(target.stat().st_mode)
    except StoragePermissionError:
        raise
    except Exception as exc:  # fail closed when platform verification misbehaves
        raise StoragePermissionError(f"cannot secure local storage directory {target}") from exc
    if mode != _MODE_DIRECTORY:
        raise StoragePermissionError(f"restrictive mode verification failed for {target}: {mode:o}")
    return StoragePermissionEvidence(target, os.name, f"mode={mode:o}")


def secure_file(path: Path) -> StoragePermissionEvidence:
    """Apply and verify restrictive permissions to one local storage file."""
    target = Path(path)
    _refuse_reparse_path(target)
    if target.is_dir():
        raise StoragePermissionError(f"local storage path is a directory, not a file: {target}")
    try:
        if os.name == "nt":
            return _secure_windows(target)
        target.chmod(_MODE_FILE)
        mode = stat.S_IMODE(target.stat().st_mode)
    except StoragePermissionError:
        raise
    except Exception as exc:  # fail closed when platform verification misbehaves
        raise StoragePermissionError(f"cannot secure local storage file {target}") from exc
    if mode != _MODE_FILE:
        raise StoragePermissionError(f"restrictive mode verification failed for {target}")
    return StoragePermissionEvidence(target, os.name, f"mode={mode:o}")


def create_private_file(path: Path, data: bytes) -> StoragePermissionEvidence:
    """Create one private file exclusively, establish its permissions, then write ``data``.

    The file is created with exclusive semantics and no secret bytes. Its parent and its own
    effective permissions are verified before ``data`` is written. A pre-existing target, symlink,
    junction or other reparse component is refused; any failure removes the incomplete file.
    """
    if not isinstance(data, bytes):
        raise TypeError("private file data must be bytes")
    target = Path(path)
    secure_directory(target.parent)
    _refuse_reparse_path(target)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, _MODE_FILE)
    except FileExistsError:
        raise
    except OSError as exc:
        raise StoragePermissionError(f"cannot create private local storage file {target}") from exc

    completed = False
    try:
        secure_file(target)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    except StoragePermissionError:
        raise
    except OSError as exc:
        raise StoragePermissionError(f"cannot write private local storage file {target}") from exc
    else:
        evidence = secure_file(target)
        completed = True
        return evidence
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not completed:
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                raise StoragePermissionError(
                    f"cannot clean up private local storage file {target}"
                ) from exc


def _secure_windows(path: Path) -> StoragePermissionEvidence:
    """Set and verify a protected Windows DACL using native security APIs."""
    _refuse_reparse_path(path)
    if not path.exists():
        raise StoragePermissionError(f"cannot secure missing Windows path {path}")
    try:
        sid = _current_user_sid()
        inheritance = "OICI" if path.is_dir() else ""
        ace_flags = f"{inheritance};" if inheritance else ";"
        sddl = f"D:P(A;{ace_flags}FA;;;{sid})(A;{ace_flags}FA;;;SY)(A;{ace_flags}FA;;;BA)"
        descriptor, dacl = _descriptor_dacl(sddl)
        try:
            security_info = _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
            result = _ADVAPI.SetNamedSecurityInfoW(
                str(path),
                _SE_FILE_OBJECT,
                security_info,
                None,
                None,
                dacl,
                None,
            )
            if result != 0:
                raise StoragePermissionError(
                    f"cannot set Windows ACL for {path}: winerror={result}"
                )
        finally:
            _KERNEL32.LocalFree(descriptor)
        verified = _read_sddl(path)
    except StoragePermissionError:
        raise
    except Exception as exc:  # fail closed when native APIs misbehave
        raise StoragePermissionError(f"cannot verify Windows ACL for {path}") from exc
    expected = _expected_sddl(sid, path.is_dir())
    if verified != expected:
        raise StoragePermissionError(f"Windows ACL is not restrictive for {path}")
    return StoragePermissionEvidence(path, "nt", verified)


def _refuse_reparse_path(path: Path) -> None:
    r"""Refuse symlink/junction components before changing or creating protected state.

    Windows junctions are reparse points: an ACL operation on ``root\junction\file`` can affect
    a target outside ``root`` while the path still appears contained to a lexical check. Refusing
    every reparse component also prevents POSIX symlink traversal and makes the guarantee uniform.
    """
    target = Path(path)
    for candidate in (target, *target.parents):
        try:
            is_junction = getattr(candidate, "is_junction", lambda: False)()
            is_reparse = _windows_reparse(candidate)
        except StoragePermissionError:
            raise
        except OSError as exc:
            raise StoragePermissionError(f"cannot inspect local storage path {candidate}") from exc
        if candidate.is_symlink() or is_junction or is_reparse:
            raise StoragePermissionError(f"reparse paths are not allowed for local storage: {path}")


def _windows_reparse(path: Path) -> bool:
    """Return whether Windows marks ``path`` as any reparse point, not only a junction."""
    if os.name != "nt" or _KERNEL32 is None:
        return False
    attributes = _KERNEL32.GetFileAttributesW(str(path))
    if attributes == _INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SDDL_REVISION_1 = 1
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_MODE_DIRECTORY = 0o700
_MODE_FILE = 0o600

_ADVAPI = ctypes.WinDLL("advapi32", use_last_error=True) if os.name == "nt" else None
_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None

if _ADVAPI is not None and _KERNEL32 is not None:
    _ADVAPI.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _ADVAPI.OpenProcessToken.restype = wintypes.BOOL
    _ADVAPI.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _ADVAPI.GetTokenInformation.restype = wintypes.BOOL
    _ADVAPI.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    _ADVAPI.ConvertSidToStringSidW.restype = wintypes.BOOL
    _ADVAPI.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _ADVAPI.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _ADVAPI.GetSecurityDescriptorDacl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _ADVAPI.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _ADVAPI.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    _ADVAPI.SetNamedSecurityInfoW.restype = wintypes.DWORD
    _ADVAPI.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    _ADVAPI.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _ADVAPI.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _ADVAPI.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    _KERNEL32.GetCurrentProcess.restype = wintypes.HANDLE
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32.LocalFree.restype = wintypes.LPVOID
    _KERNEL32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    _KERNEL32.GetFileAttributesW.restype = wintypes.DWORD


class _SidAndAttributes(ctypes.Structure):
    """The native TOKEN_USER field needed to identify this process owner."""

    _fields_ = [("sid", wintypes.LPVOID), ("attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    """The native TOKEN_USER record returned by GetTokenInformation."""

    _fields_ = [("user", _SidAndAttributes)]


def _current_user_sid() -> str:
    """Return the current process token SID in canonical string form."""
    if _ADVAPI is None or _KERNEL32 is None:
        raise StoragePermissionError("Windows security APIs are unavailable")
    token = wintypes.HANDLE()
    if not _ADVAPI.OpenProcessToken(
        _KERNEL32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        raise StoragePermissionError("cannot open the current process token")
    try:
        size = wintypes.DWORD()
        _ADVAPI.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        if size.value == 0:
            raise StoragePermissionError("cannot size the current process token")
        buffer = ctypes.create_string_buffer(size.value)
        if not _ADVAPI.GetTokenInformation(token, _TOKEN_USER, buffer, size, ctypes.byref(size)):
            raise StoragePermissionError("cannot read the current process token")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.user.sid
        return _sid_to_string(sid_pointer)
    finally:
        _KERNEL32.CloseHandle(token)


def _sid_to_string(sid: wintypes.LPVOID) -> str:
    """Convert a native SID pointer into a stable identifier."""
    if _ADVAPI is None or _KERNEL32 is None:
        raise StoragePermissionError("Windows security APIs are unavailable")
    result = wintypes.LPWSTR()
    if not _ADVAPI.ConvertSidToStringSidW(sid, ctypes.byref(result)):
        raise StoragePermissionError("cannot convert the current process SID")
    try:
        return result.value or ""
    finally:
        _KERNEL32.LocalFree(result)


def _descriptor_dacl(sddl: str) -> tuple[wintypes.LPVOID, wintypes.LPVOID]:
    """Build a native protected DACL from a fixed trustee allowlist."""
    if _ADVAPI is None:
        raise StoragePermissionError("Windows security APIs are unavailable")
    descriptor = wintypes.LPVOID()
    if not _ADVAPI.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
    ):
        raise StoragePermissionError("cannot build the local Run security descriptor")
    present = wintypes.BOOL()
    dacl = wintypes.LPVOID()
    defaulted = wintypes.BOOL()
    if not _ADVAPI.GetSecurityDescriptorDacl(
        descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
    ):
        _KERNEL32.LocalFree(descriptor)
        raise StoragePermissionError("cannot inspect the local Run security descriptor")
    return descriptor, dacl


def _read_sddl(path: Path) -> str:
    """Read a path's DACL as SDDL for independent post-write verification."""
    if _ADVAPI is None or _KERNEL32 is None:
        raise StoragePermissionError("Windows security APIs are unavailable")
    descriptor = wintypes.LPVOID()
    result = _ADVAPI.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _DACL_SECURITY_INFORMATION,
        None,
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise StoragePermissionError(f"cannot read Windows ACL for {path}: winerror={result}")
    try:
        rendered = wintypes.LPWSTR()
        length = wintypes.DWORD()
        if not _ADVAPI.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            _SDDL_REVISION_1,
            _DACL_SECURITY_INFORMATION,
            ctypes.byref(rendered),
            ctypes.byref(length),
        ):
            raise StoragePermissionError("cannot render the local Run security descriptor")
        try:
            return rendered.value or ""
        finally:
            _KERNEL32.LocalFree(rendered)
    finally:
        _KERNEL32.LocalFree(descriptor)


def _expected_sddl(sid: str, directory: bool) -> str:
    """Return the exact protected DACL string permitted for local Run state."""
    flags = "OICI" if directory else ""
    dacl_prefix = "D:PAI"
    return f"{dacl_prefix}(A;{flags};FA;;;{sid})(A;{flags};FA;;;SY)(A;{flags};FA;;;BA)"


__all__ = [
    "StoragePermissionError",
    "StoragePermissionEvidence",
    "create_private_file",
    "secure_directory",
    "secure_file",
]
