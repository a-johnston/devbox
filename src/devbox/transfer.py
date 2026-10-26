"""Ad-hoc files between the host and an instance's inbox and outbox, streamed as tar.

Both directions name each file after its own basename, flat in the target directory, and
check every name before transferring anything. The guest controls what it sends back, so
received archives extract only the requested names, through tarfile's data filter.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import tarfile
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import IO

from devbox.errors import DevboxError

INBOX = PurePosixPath("/devbox/inbox")
OUTBOX = PurePosixPath("/devbox/outbox")

# Guest: $1 the inbox, $2 the target directory within it, $3 "1" to replace existing names,
# then the top-level names.
# Checks every name before reading the archive, so a refusal leaves the inbox untouched.
RECEIVE = r"""
inbox=$1 d=$2 force=$3
shift 3
[ -d "$inbox" ] || {
  echo "This instance's image predates the inbox; run devbox build, then devbox new" >&2
  exit 1
}
mkdir -p -- "$d" || exit
for n in "$@"; do
  if [ -e "$d/$n" ] || [ -L "$d/$n" ]; then
    if [ "$force" = 1 ]; then
      rm -rf -- "${d:?}/$n" || exit
    else
      printf '%s already exists; use --force to replace it\n' "$d/$n" >&2
      exit 1
    fi
  fi
done
exec tar -x -f - -C "$d" --no-same-owner
"""

# Guest: expand each pattern (an unquoted, unsplit expansion), printing NUL-separated
# (match, resolved path) pairs. A pattern that matches nothing stays literal and fails.
MATCHES = r"""
IFS=
for p in "$@"; do
  set -- $p
  [ -e "$1" ] || [ -L "$1" ] || { printf 'No match in the guest: %s\n' "$p" >&2; exit 1; }
  for f in "$@"; do
    r=$(realpath -- "$f") && { [ -f "$r" ] || [ -d "$r" ]; } || {
      printf '%s is not a regular file or directory\n' "$f" >&2
      exit 1
    }
    printf '%s\000%s\000' "$f" "$r"
  done
done
"""

# Guest: list $1's entries for completion, directories with a trailing slash.
ENTRIES = r"""
for f in "$1"/* "$1"/.*; do
  n=${f##*/}
  case $n in .|..) continue ;; esac
  [ -e "$f" ] || [ -L "$f" ] || continue
  if [ -d "$f" ]; then printf '%s/\n' "$n"; else printf '%s\n' "$n"; fi
done
"""


def expand(patterns: Sequence[str]) -> list[Path]:
    """Existing host paths as given, else a (quoted) glob's matches, with ** recursion."""
    paths = []
    for pattern in patterns:
        path = os.path.expanduser(pattern)
        if os.path.lexists(path):
            paths.append(Path(path))
        elif re.search(r"[*?[]", path):
            matches = sorted(glob.glob(path, recursive=True))
            if not matches:
                raise DevboxError(f"No files match {pattern}")
            paths.extend(map(Path, matches))
        else:
            raise DevboxError(f"No such file or directory: {pattern}")
    return paths


def unique(pairs: Sequence[tuple[str, str]]) -> dict[str, str]:
    """Map each flat name to its source, refusing two different sources for one name."""
    named: dict[str, str] = {}
    for name, source in pairs:
        if name in {"", ".", ".."} or "/" in name:
            raise DevboxError(f"Cannot name a transferred file after {source}")
        if named.setdefault(name, source) != source:
            raise DevboxError(f"{named[name]} and {source} would both be named {name}")
    return named


def uploads(paths: Sequence[Path]) -> dict[str, Path]:
    """Name each path after itself, but send what a symlinked path points to."""
    pairs = []
    for path in paths:
        source = path.resolve(strict=True)
        if not (source.is_file() or source.is_dir()):
            raise DevboxError(f"{path} is not a regular file or directory")
        pairs.append((Path(os.path.abspath(path)).name, str(source)))
    return {name: Path(source) for name, source in unique(pairs).items()}


def measure(sources: Sequence[Path]) -> tuple[int, int]:
    """Regular files and their total size, as an upload would send them."""
    files = size = 0
    for source in sources:
        walked = os.walk(source) if source.is_dir() else [(source.parent, [], [source.name])]
        for root, _, names in walked:
            for name in names:
                path = Path(root, name)
                if path.is_file() and not path.is_symlink():
                    files += 1
                    size += path.stat().st_size
    return files, size


def portable(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Keep files, directories and links; drop devices, FIFOs and host ownership."""
    if not (info.isfile() or info.isdir() or info.issym() or info.islnk()):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def write(stream: IO[bytes], named: dict[str, Path]) -> None:
    """Links inside directories stay links, so the archive never leaves those trees."""
    with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        for name, source in named.items():
            archive.add(source, arcname=name, filter=portable)


def downloads(matches: Sequence[tuple[str, str]]) -> tuple[dict[str, str], dict[str, str]]:
    """Name guest matches like uploads: host name -> resolved guest path, and the archive
    name (the resolved basename, as tar stores it) -> host name."""
    named = unique(
        [(PurePosixPath(match).name or PurePosixPath(resolved).name, resolved) for match, resolved in matches]
    )
    archived = unique([(PurePosixPath(resolved).name, name) for name, resolved in named.items()])
    return named, archived


def tar_arguments(named: dict[str, str]) -> list[str]:
    """`./` keeps names that start with a dash from reading as options."""
    arguments = ["tar", "-c", "-f", "-"]
    for resolved in named.values():
        path = PurePosixPath(resolved)
        arguments += ["-C", str(path.parent), f"./{path.name}"]
    return arguments


def existing(destination: Path, names: Sequence[str]) -> list[Path]:
    return [destination / name for name in names if os.path.lexists(destination / name)]


def extract(stream: IO[bytes], destination: Path, archived: dict[str, str]) -> None:
    """Stage the whole archive, then move each top-level name into place, replacing what
    is there: a failure partway leaves the destination as it was."""
    staging = Path(tempfile.mkdtemp(prefix=".devbox-get-", dir=destination))
    try:
        with tarfile.open(fileobj=stream, mode="r|") as archive:

            def requested() -> Iterator[tarfile.TarInfo]:
                for member in archive:
                    top = PurePosixPath(member.name).parts[:1]
                    if not top or top[0] not in archived:
                        raise DevboxError(f"The guest sent an unrequested file: {member.name}")
                    yield member

            try:
                archive.extractall(staging, members=requested(), filter="data")
            except tarfile.FilterError as exc:
                raise DevboxError(f"Refusing a received file: {exc}") from exc
        for name, host_name in archived.items():
            target = destination / host_name
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif os.path.lexists(target):
                target.unlink()
            os.replace(staging / name, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
