"""Mirror the hub's art library to a local folder, following its index.

The hub's ``snaphouse-artadmin`` service publishes ``art-index.json``: the whole
library (folders, pieces, trash) with each piece's download ``url`` and metadata.
The frame mirrors only the pieces in enabled folders into its local art
directory, keeping the ``folder/file`` layout the hub uses, and writes a sidecar
caption beside each image from the index's title, artist, and source URL. This
supersedes the old flat ``/art/`` autoindex sync.

- a piece not present locally is downloaded (``added``);
- a piece whose upstream size or mtime differs is re-downloaded (``updated``);
- a local image or sidecar no longer listed, or in a folder no longer enabled, is
  deleted (``removed``);
- an emptied folder directory is removed.

Each piece id is validated per path segment so nothing can escape the art
directory (``..``, absolute paths, hidden folders, or more than one folder deep
are refused). Each download streams to a temporary name and is renamed into
place, so a reader never sees a half-written file, and is capped so a spoofed hub
cannot fill the disk. A missing or malformed index changes nothing locally: a
transient outage never empties the frame's art.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
# Cap each file so a compromised or spoofed hub cannot stream an unbounded body
# onto the device and fill its disk. Art is modest in size; 64 MiB is generous.
_MAX_FILE_BYTES = 64 * 1024 * 1024
_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True)
class SyncResult:
    """How many files a sync added, updated, and removed (images and sidecars)."""

    added: int = 0
    updated: int = 0
    removed: int = 0


def sync_index(
    client: httpx.Client,
    index_url: str,
    base_url: str,
    dest: Path,
    enabled: set[str] | None,
) -> SyncResult:
    """Mirror the enabled pieces of the index at ``index_url`` into ``dest``.

    ``base_url`` is the hub origin each piece's ``url`` is resolved against.
    ``enabled`` is the set of folder names to mirror, or ``None`` to mirror every
    folder (the fallback when the control file is missing). See the module
    docstring for the rules.
    """
    dest = Path(dest)
    pieces = _fetch_pieces(client, index_url)
    if pieces is None:
        # A missing or unusable index changes nothing locally.
        return SyncResult()

    wanted = {}
    for piece in pieces:
        if not isinstance(piece, dict):
            continue
        pid = piece.get("id")
        url = piece.get("url")
        folder = piece.get("folder")
        if not isinstance(pid, str) or not isinstance(url, str):
            continue
        if enabled is not None and folder not in enabled:
            continue
        if not _is_safe_relpath(pid):
            continue
        wanted[pid] = piece

    dest.mkdir(parents=True, exist_ok=True)

    added = 0
    updated = 0
    kept: set[str] = set()
    for pid, piece in wanted.items():
        local = dest / Path(pid)
        existed = local.is_file()
        if not existed or _differs(local, piece):
            if not _download(client, base_url, piece, local):
                continue
            if existed:
                updated += 1
            else:
                added += 1
        kept.add(pid)
        if _write_sidecar(local, piece):
            kept.add(pid + ".txt")

    removed = _remove_stale(dest, kept)
    return SyncResult(added=added, updated=updated, removed=removed)


def _fetch_pieces(client: httpx.Client, index_url: str) -> list | None:
    """The index's ``pieces`` list, or ``None`` when the index cannot be read."""
    try:
        response = client.get(index_url, timeout=_TIMEOUT)
    except httpx.HTTPError as error:
        logger.warning("art index fetch failed: %s", error)
        return None
    if response.status_code != 200:
        logger.warning("art index returned %s", response.status_code)
        return None
    try:
        data = response.json()
    except ValueError as error:
        logger.warning("art index was not JSON: %s", error)
        return None
    if not isinstance(data, dict):
        logger.warning("art index was not a JSON object")
        return None
    pieces = data.get("pieces")
    if not isinstance(pieces, list):
        logger.warning("art index has no pieces list")
        return None
    return pieces


def _differs(local: Path, piece: dict) -> bool:
    """Whether the upstream piece's size or mtime differs from the local file."""
    stat = local.stat()
    size = piece.get("size")
    if isinstance(size, int) and stat.st_size != size:
        return True
    mtime = piece.get("mtime")
    if isinstance(mtime, int) and int(stat.st_mtime) != mtime:
        return True
    return False


def _download(
    client: httpx.Client, base_url: str, piece: dict, local: Path
) -> bool:
    """Stream the piece's ``url`` to a temp file and rename it over ``local``.

    Returns ``True`` on success. A failed download is logged and skipped, leaving
    any existing local copy in place. The piece ``url`` is already percent-encoded
    per segment, so it is appended to the origin without re-quoting.
    """
    url = base_url.rstrip("/") + "/" + str(piece["url"]).lstrip("/")
    local.parent.mkdir(parents=True, exist_ok=True)
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
                logger.warning("art file %s returned %s", url, response.status_code)
                return False
            if _declared_length(response) > _MAX_FILE_BYTES:
                logger.warning("art file %s declares too large a body", url)
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
                    "art file %s exceeded the %d byte cap", url, _MAX_FILE_BYTES
                )
                _unlink_quietly(tmp)
                return False
    except httpx.HTTPError as error:
        logger.warning("art file %s fetch failed: %s", url, error)
        _unlink_quietly(tmp)
        return False

    os.replace(tmp, local)
    mtime = piece.get("mtime")
    if isinstance(mtime, int):
        os.utime(local, (mtime, mtime))
    return True


def _write_sidecar(local: Path, piece: dict) -> bool:
    """Write the piece's caption sidecar from the index; return whether it exists.

    The sidecar is ``<image>.txt`` beside the image: line 1 the title, line 2 the
    artist (blank when none), line 3 an https source URL (blank when none). A
    piece with no title gets no sidecar (and a stale one is swept away), so the
    local caption always matches the hub's index.
    """
    sidecar = local.with_name(local.name + ".txt")
    title = piece.get("title")
    if not isinstance(title, str) or not title.strip():
        _unlink_quietly(sidecar)
        return False
    artist = piece.get("artist")
    source_url = piece.get("source_url")
    lines = [
        title.strip(),
        artist.strip() if isinstance(artist, str) else "",
        source_url.strip() if isinstance(source_url, str) else "",
    ]
    text = "\n".join(lines) + "\n"
    if sidecar.is_file() and sidecar.read_text(encoding="utf-8") == text:
        return True
    sidecar.write_text(text, encoding="utf-8")
    return True


def _remove_stale(dest: Path, keep: set[str]) -> int:
    """Delete images and sidecars under ``dest`` not in ``keep``; count them.

    Walks the top level and one folder deep, removing any image or ``.txt`` file
    whose relative path is not kept, then removing a user folder left empty.
    """
    removed = 0
    for entry in list(dest.iterdir()):
        if entry.is_file():
            if _is_managed(entry) and entry.name not in keep:
                _unlink_quietly(entry)
                removed += 1
        elif entry.is_dir() and not entry.name.startswith("."):
            for sub in list(entry.iterdir()):
                rel = f"{entry.name}/{sub.name}"
                if sub.is_file() and _is_managed(sub) and rel not in keep:
                    _unlink_quietly(sub)
                    removed += 1
            _remove_if_empty(entry)
    return removed


def _is_managed(path: Path) -> bool:
    """Whether a file is one the sync owns: an image or a sidecar caption."""
    if path.suffix.lower() in _EXTENSIONS:
        return True
    return path.name.endswith(".txt")


def _is_safe_relpath(pid: str) -> bool:
    """Whether ``pid`` is a safe library-relative path, at most one folder deep.

    The index comes from the hub, trusted but not implicitly: a spoofed index
    could carry ``../`` or an absolute path to make a download land outside the
    art cache. Only a bare filename, or a single folder plus a filename, both
    plain segments, is accepted.
    """
    segments = pid.split("/")
    if not 1 <= len(segments) <= 2:
        return False
    for segment in segments:
        if segment in ("", ".", "..") or segment.startswith("."):
            return False
        if "\\" in segment or "\x00" in segment:
            return False
        if segment != Path(segment).name:
            return False
    return True


def _declared_length(response: httpx.Response) -> int:
    """The Content-Length header as an int, or 0 when absent or malformed."""
    raw = response.headers.get("content-length")
    if raw is None or not raw.isdigit():
        return 0
    return int(raw)


def _remove_if_empty(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
