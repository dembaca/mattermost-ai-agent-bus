"""Files an agent attaches to its posts (RT-6.33, ADR-012).

The server never opens a path the agent names: under corral it runs on the
host (RT-6.18), and a path, or a link in the workspace, would hand host files
to the chat with the host's rights. The bytes come from inside the sandbox
instead: the shell sends them to the hook API (`POST /v1/files`, read by curl
with the agent's own rights), or the `attach` tool carries a small text in
its argument. The same one path holds without corral.

Before anything goes to Mattermost the bytes are checked here: the type by
its magic bytes, never by the name (PNG, JPEG, GIF, WebP, PDF; a text type
only as valid UTF-8 without NUL), the name's extension must say the same
type, and the name is cut to a plain base name. SVG is refused (it can carry
script). The upload is remembered for this server, in memory: a post takes
only ids this server uploaded, not used yet, within PENDING_SEC, at most
POST_MAX per post, in the post's channel.
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .port import ChatPort

# The most one file may hold (the hook API refuses a longer body before it
# reads it); Mattermost's own MaxFileSize may be lower and refuses then.
FILE_MAX = 10 * 1024 * 1024
# The most the attach tool's text may hold, in UTF-8 bytes: it costs tokens.
TEXT_MAX = 100 * 1024
# A file name is cut to this many characters, its extension kept.
NAME_MAX = 100
# An upload a post may take this long; an older one is forgotten.
PENDING_SEC = 3600
# Files per post (Mattermost's own limit) and uploads remembered at once.
POST_MAX = 10
PENDING_MAX = 50

# Extension -> the type the bytes must have.
TYPES = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "gif": "gif", "webp": "webp", "pdf": "pdf",
         "txt": "text", "md": "text", "csv": "text", "tsv": "text", "json": "text",
         "log": "text", "yaml": "text", "yml": "text"}
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"), (b"GIF87a", "gif"),
          (b"GIF89a", "gif"), (b"%PDF-", "pdf"))
_ID = re.compile(r"[a-z0-9]{26}")


class NotAllowed(ValueError):
    """The file may not go out; the text says why, for the agent."""


def sniff(data: bytes) -> str:
    """The type by the bytes: png, jpeg, gif, webp, pdf, text (valid UTF-8
    without NUL), or '' (none of these)."""
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if b"\x00" in data:
        return ""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return "text"


def clean_name(name: str) -> str:
    """A plain base name: no directory, no control or format characters,
    at most NAME_MAX characters with the extension kept; NotAllowed when
    nothing usable is left."""
    base = re.split(r"[/\\]", name)[-1]
    base = "".join(ch for ch in base if unicodedata.category(ch)[0] not in "CZ" or ch == " ").strip()
    stem, dot, ext = base.rpartition(".")
    if not dot or not stem.strip(" .") or not ext:
        raise NotAllowed(f"file name {name[:40]!r}: a name with an extension, e.g. chart.png")
    if len(base) > NAME_MAX:
        base = stem[:NAME_MAX - len(ext) - 1].rstrip() + "." + ext
    return base


def check(name: str, data: bytes) -> tuple[str, str]:
    """(clean name, type) of a file that may go out, else NotAllowed."""
    clean = clean_name(name)
    ext = clean.rpartition(".")[2].lower()
    if not data:
        raise NotAllowed("the file is empty")
    if len(data) > FILE_MAX:
        raise NotAllowed(f"the file is over {FILE_MAX // 2**20} MB")
    if ext == "svg":
        raise NotAllowed("SVG is refused: it can carry script; render it as PNG")
    want = TYPES.get(ext)
    if want is None:
        raise NotAllowed(f".{ext} is not allowed: {', '.join(sorted(TYPES))}")
    got = sniff(data)
    if got != want:
        what = f"a {got} file" if got else "no allowed type (binary, or not UTF-8)"
        raise NotAllowed(f"{clean} is named .{ext} but its bytes are {what}")
    return clean, want


@dataclass(frozen=True)
class Upload:
    """A file this server uploaded and no post has taken yet."""

    file_id: str
    name: str
    channel_id: str
    size: int
    at: float


_pending: dict[str, Upload] = {}


def _prune(now: float) -> None:
    for fid in [f for f, u in _pending.items() if now - u.at > PENDING_SEC]:
        del _pending[fid]
    while len(_pending) > PENDING_MAX:
        del _pending[next(iter(_pending))]  # the oldest first


async def upload(client: ChatPort, name: str, data: bytes, channel_id: str) -> dict[str, Any]:
    """Check the file, upload it to the channel and remember it (RT-6.33
    steps 2 to 4): {file_id, name, type, size, channel}. NotAllowed before
    anything is sent; Mattermost's refusal (too large, no permission) raises
    as the adapter raises it."""
    clean, kind = check(name, data)
    info = await client.upload_file(channel_id, clean, data)
    now = time.time()
    _pending[info.id] = Upload(info.id, info.name or clean, channel_id, info.size or len(data), now)
    _prune(now)
    return {"file_id": info.id, "name": info.name or clean, "type": kind,
            "size": info.size or len(data), "channel": channel_id}


def take(ids: list[str], channel_id: str = "") -> tuple[list[Upload], str]:
    """The uploads a post may carry, taken from the store, and '' — or [] and
    why not, nothing taken: more than POST_MAX, an id this server did not
    upload, one used already or older than PENDING_SEC, or one in another
    channel than `channel_id` (the post's; '' skips that check). give_back()
    returns them when the post fails."""
    ids = list(dict.fromkeys(i.strip() for i in ids if i.strip()))
    if len(ids) > POST_MAX:
        return [], f"files: at most {POST_MAX} per post"
    _prune(time.time())
    for fid in ids:
        up = _pending.get(fid)
        if not _ID.fullmatch(fid) or up is None:
            return [], (f"file {fid[:30]}: not an upload of this session waiting for a post "
                        f"(used, older than {PENDING_SEC // 60} min, more than {PENDING_MAX} "
                        "uploads since, or never uploaded here); upload it again")
        if channel_id and up.channel_id != channel_id:
            return [], (f"file {fid} was uploaded to channel {up.channel_id}, the post goes to "
                        f"{channel_id}: upload it again with channel {channel_id}")
    return [_pending.pop(fid) for fid in ids], ""


def give_back(uploads: list[Upload]) -> None:
    """A post that failed leaves its files usable again."""
    for up in uploads:
        _pending.setdefault(up.file_id, up)
