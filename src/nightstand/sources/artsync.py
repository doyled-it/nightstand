"""Mirror the hub's art share to a local folder.

The hub serves its art folder read-only as an nginx JSON autoindex at
``http://<hub>/art/`` (``autoindex on; autoindex_format json;``). Each entry is
``{"name", "type": "file" | "directory", "mtime", "size"}`` where ``mtime`` is an
HTTP-date in GMT. This mirrors that listing one level deep, files only:

- a file not present locally is downloaded (``added``);
- a file whose upstream size or mtime differs from the local copy is
  re-downloaded (``updated``);
- a local file no longer in the listing is deleted (``removed``);
- directory entries are ignored.

Each download streams to a temporary name in the destination and is renamed into
place, so a reader (the art library) never sees a half-written file. The local
file's mtime is set to the upstream mtime so the next sync can tell unchanged
files apart without re-downloading them. Sidecar captions are ordinary
``<image>.txt`` files in the same listing, so they sync by the same rules.

An unreachable hub, or any failure fetching the listing, leaves the destination
untouched: a transient outage never empties the frame's art.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
# Cap each file so a compromised or spoofed hub cannot stream an unbounded body
# onto the device and fill its disk. Art is modest in size; 64 MiB is generous.
_MAX_FILE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class SyncResult:
    """How many files a sync added, updated, and removed."""

    added: int = 0
    updated: int = 0
    removed: int = 0


def sync_art(client: httpx.Client, base_url: str, dest: Path) -> SyncResult:
    """Mirror the listing at ``base_url`` into ``dest``; see the module docstring."""
    dest = Path(dest)
    entries = _fetch_listing(client, base_url)
    if entries is None:
        # The hub is unreachable or the listing is unusable: change nothing.
        return SyncResult()

    files = {
        entry["name"]: entry
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("type") == "file"
        and isinstance(entry.get("name"), str)
        and _is_safe_name(entry["name"])
    }

    dest.mkdir(parents=True, exist_ok=True)

    added = 0
    updated = 0
    for name, entry in files.items():
        local = dest / name
        existed = local.is_file()
        if existed and not _differs(local, entry):
            continue
        if not _download(client, base_url, name, local, entry):
            continue
        if existed:
            updated += 1
        else:
            added += 1

    removed = _remove_stale(dest, set(files))
    return SyncResult(added=added, updated=updated, removed=removed)


def _fetch_listing(client: httpx.Client, base_url: str) -> list | None:
    """The autoindex entries, or ``None`` when the hub cannot be read."""
    try:
        response = client.get(base_url, timeout=_TIMEOUT)
    except httpx.HTTPError as error:
        logger.warning("art listing fetch failed: %s", error)
        return None
    if response.status_code != 200:
        logger.warning("art listing returned %s", response.status_code)
        return None
    try:
        data = response.json()
    except ValueError as error:
        logger.warning("art listing was not JSON: %s", error)
        return None
    if not isinstance(data, list):
        logger.warning("art listing was not a JSON array")
        return None
    return data


def _differs(local: Path, entry: dict) -> bool:
    """Whether the upstream entry's size or mtime differs from the local file."""
    stat = local.stat()
    upstream_mtime = _mtime_epoch(entry.get("mtime"))
    size = entry.get("size")
    if isinstance(size, int) and stat.st_size != size:
        return True
    if upstream_mtime is not None and int(stat.st_mtime) != upstream_mtime:
        return True
    return False


def _download(
    client: httpx.Client, base_url: str, name: str, local: Path, entry: dict
) -> bool:
    """Stream ``name`` to a temp file and rename it over ``local``.

    Returns ``True`` on success. A failed download is logged and skipped,
    leaving any existing local copy in place.
    """
    url = base_url.rstrip("/") + "/" + quote(name)
    tmp = local.with_name(local.name + ".part")
    try:
        with client.stream(
            "GET",
            url,
            # Refuse content-encoding so a small compressed body cannot amplify
            # past the byte cap before it is written.
            headers={"Accept-Encoding": "identity"},
            timeout=_TIMEOUT,
        ) as response:
            if response.status_code != 200:
                logger.warning("art file %s returned %s", name, response.status_code)
                return False
            if _declared_length(response) > _MAX_FILE_BYTES:
                logger.warning("art file %s declares too large a body", name)
                return False
            total = 0
            exceeded = False
            with tmp.open("wb") as handle:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > _MAX_FILE_BYTES:
                        exceeded = True
                        break
                    handle.write(chunk)
            if exceeded:
                logger.warning(
                    "art file %s exceeded the %d byte cap", name, _MAX_FILE_BYTES
                )
                _unlink_quietly(tmp)
                return False
    except httpx.HTTPError as error:
        logger.warning("art file %s fetch failed: %s", name, error)
        _unlink_quietly(tmp)
        return False

    os.replace(tmp, local)
    upstream_mtime = _mtime_epoch(entry.get("mtime"))
    if upstream_mtime is not None:
        os.utime(local, (upstream_mtime, upstream_mtime))
    return True


def _remove_stale(dest: Path, keep: set[str]) -> int:
    """Delete top-level files in ``dest`` not named in ``keep``; count them."""
    removed = 0
    for entry in dest.iterdir():
        if entry.is_file() and entry.name not in keep:
            _unlink_quietly(entry)
            removed += 1
    return removed


def _is_safe_name(name: str) -> bool:
    """Whether ``name`` is a plain filename that cannot escape the destination.

    The listing comes from the hub, which is trusted but not implicitly so: a
    spoofed or compromised hub could return ``../`` or an absolute path to make a
    download land outside the art cache. Only a bare filename is accepted.
    """
    if name in ("", ".", ".."):
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    return name == Path(name).name


def _declared_length(response: httpx.Response) -> int:
    """The Content-Length header as an int, or 0 when absent or malformed."""
    raw = response.headers.get("content-length")
    if raw is None or not raw.isdigit():
        return 0
    return int(raw)


def _mtime_epoch(raw: object) -> int | None:
    """Parse an HTTP-date ``mtime`` to integer epoch seconds, or ``None``."""
    if not isinstance(raw, str):
        return None
    try:
        return int(parsedate_to_datetime(raw).timestamp())
    except (TypeError, ValueError):
        return None


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
