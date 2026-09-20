"""One export file for streaming download tools.

An export is what is left after the expensive half of a download: the manifest, the
DRM init data, the content keys, and the title metadata. A reader can finish the
download with no account and no CDM. The reader matches content keys by KID at download time, so
the file never decides which tracks it takes.

Format: ``kind`` is ``mediaexport``, ``version`` is an integer bumped only for breaking
changes. A reader accepts any version up to its own, ignores unknown fields, and carries
``x-<app>`` blocks through untouched. ``loads`` converts the legacy ``unidl-export`` v1
and unshackle v2 shapes on read and never writes them.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

KIND = "mediaexport"
VERSION = 1

__all__ = [
    "UNDERSTOOD",
    "KIND",
    "VERSION",
    "Document",
    "Drm",
    "Entry",
    "ExportError",
    "KeyConflict",
    "Manifest",
    "dumps",
    "from_unidl_v1",
    "from_unshackle_v2",
    "guess_type",
    "loads",
    "read",
    "ts_ms",
    "write",
]


class ExportError(ValueError):
    """Not an export, or one this reader cannot use."""


class KeyConflict(ExportError):
    """One KID with two different content keys.

    A writer that merges into an existing file tells this apart from a corrupt file: the
    file itself is readable, so it keeps its other titles and is not thrown away.
    """


@dataclass
class Manifest:
    """``type`` is ``dash``, ``hls``, ``ism`` or empty. ``role`` is ``primary``, ``extra`` or empty.

    ``extras`` holds the fields this reader does not know, written back as they came.
    """

    url: str
    type: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    role: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class Drm:
    """``system`` is ``widevine``, ``playready`` or ``clearkey``. ``wrm_header`` is optional beside ``pssh``.

    ``extras`` holds the fields this reader does not know, written back as they came.
    """

    system: str
    pssh: str = ""
    wrm_header: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class Entry:
    """One title. ``kind`` is ``movie``, ``episode``, ``song`` or ``clip``.

    ``extensions`` holds the ``x-<app>`` blocks and every other field this reader does not
    know, written back as they came.
    """

    id: str
    kind: str
    title: str
    series: str = ""
    season: int | None = None
    episode: int | None = None
    year: int | None = None
    language: str = ""
    artist: str = ""
    album: str = ""
    track_number: int | None = None
    release_name: str = ""
    manifests: list[Manifest] = field(default_factory=list)
    drm: list[Drm] = field(default_factory=list)
    keys: dict[str, str] = field(default_factory=dict)
    chapters: list[dict[str, Any]] = field(default_factory=list)
    tracks: list[dict[str, Any]] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)

    @property
    def primary(self) -> Manifest | None:
        """The manifest to fetch first: the one marked primary, else the first unmarked one."""
        for m in self.manifests:
            if m.role == "primary":
                return m
        return next((m for m in self.manifests if m.role != "extra"), None)

    def ext(self, app: str) -> dict[str, Any]:
        """The ``x-<app>`` block, created on first use."""
        return self.extensions.setdefault(f"x-{app}", {})

    def add_key(self, kid: Any, key: Any) -> None:
        """Put one KID:KEY into ``keys``, normalised. A second, different key for one KID raises."""
        _merge_key(self.keys, kid, key)


@dataclass
class Document:
    service_tag: str
    service_name: str = ""
    region: str = ""
    generator: dict[str, str] = field(default_factory=dict)
    created: str = ""
    titles: list[Entry] = field(default_factory=list)

    def get(self, title_id: str) -> Entry | None:
        return next((e for e in self.titles if e.id == title_id), None)

    def add(self, entry: Entry) -> None:
        """Same id replaces in place; a title exported twice is still one title."""
        for i, e in enumerate(self.titles):
            if e.id == entry.id:
                self.titles[i] = entry
                return
        self.titles.append(entry)

    def key_pool(self) -> dict[str, str]:
        """Every KID:KEY in the file. A KID with two different keys is a writer bug and raises."""
        pool: dict[str, str] = {}
        for e in self.titles:
            for k, v in e.keys.items():
                if pool.setdefault(k, v) != v:
                    raise KeyConflict(f"KID {k[:40]} has two different keys")
        return pool


def dumps(doc: Document) -> str:
    """Serialise ``doc``. A document the reader would refuse raises here, not at the next read."""
    doc.key_pool()
    raw: dict[str, Any] = {
        "kind": KIND,
        "version": VERSION,
        "generator": doc.generator,
        "created": doc.created or datetime.now().isoformat(timespec="seconds"),
        "service": {"tag": doc.service_tag, "name": doc.service_name},
    }
    if doc.region:
        raw["region"] = doc.region
    raw["titles"] = [_entry_out(e) for e in doc.titles]
    return json.dumps(raw, indent=2, ensure_ascii=False) + "\n"


def _entry_out(e: Entry) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in asdict(e).items():
        if k == "extensions" or v in (None, "", [], {}):
            continue
        if k == "manifests":
            v = [_manifest_out(m) for m in v]
        elif k == "drm":
            v = [{dk: dv for dk, dv in d.items() if dk != "extras" and dv != ""} | d["extras"] for d in v]
        out[k] = v
    out.update(e.extensions)
    return out


def _manifest_out(m: dict[str, Any]) -> dict[str, Any]:
    m = m | {"headers": _headers(m["headers"])}
    return {k: v for k, v in m.items() if k != "extras" and v not in ("", {})} | m["extras"]


def write(path: Path | str, doc: Document) -> Path:
    """Atomic and owner-only: the file holds keys and usually a signed URL."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dumps(doc))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def loads(text: str) -> Document:
    """Read a mediaexport file, or convert a legacy unidl v1 / unshackle v2 file."""
    try:
        raw = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ExportError(f"not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise ExportError("not an export")
    kind = raw.get("kind")
    if kind == "unidl-export":
        doc = from_unidl_v1(raw)
    elif kind is None and raw.get("version") == 2 and isinstance(raw.get("titles"), dict):
        doc = from_unshackle_v2(raw)
    else:
        doc = _media_export_in(raw)
    doc.key_pool()
    return doc


def _media_export_in(raw: dict[str, Any]) -> Document:
    kind = raw.get("kind")
    if kind != KIND:
        raise ExportError(f"not a {KIND} file")
    try:
        version = int(raw.get("version", 0))
    except (TypeError, ValueError):
        raise ExportError("version is not a number") from None
    if version > VERSION:
        raise ExportError(f"version {version} is newer than this reader ({VERSION})")
    svc = raw.get("service") or {}
    if not isinstance(svc, dict) or not svc.get("tag"):
        raise ExportError("does not say which service it came from")
    titles = raw.get("titles")
    if not isinstance(titles, list) or not titles:
        raise ExportError("has no titles in it")
    return Document(
        service_tag=str(svc["tag"]),
        service_name=str(svc.get("name", "")),
        region=str(raw.get("region", "")),
        generator=dict(raw.get("generator") or {}),
        created=str(raw.get("created", "")),
        titles=[_entry_in(t, i) for i, t in enumerate(titles)],
    )


def read(path: Path | str) -> Document:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ExportError(f"could not be read ({exc})") from exc
    return loads(text)


def _entry_in(t: Any, index: int) -> Entry:
    """One title. ``index`` is its position in the file, the id it gets when it has none of its own."""
    if not isinstance(t, dict):
        raise ExportError("one of the titles is not an object")
    _check_crit(t)
    manifests = [
        Manifest(
            url=str(m["url"]),
            type=str(m.get("type", "")).lower(),
            headers=_headers(m.get("headers")),
            role=str(m.get("role", "")),
            extras={k: v for k, v in m.items() if k not in _MANIFEST_FIELDS},
        )
        for m in t.get("manifests") or []
        if isinstance(m, dict) and m.get("url")
    ]
    # a DRM-free title of direct file URLs has no manifest; its tracks carry the URLs
    if not manifests and not any(isinstance(r, dict) and r.get("url") for r in t.get("tracks") or []):
        raise ExportError(f"{t.get('title') or t.get('id') or 'a title'}: no manifest to fetch")
    return Entry(
        # two id-less titles would otherwise land on one Document.add slot and one would go
        id=str(t.get("id") or "") or f"title-{index + 1}",
        kind=str(t.get("kind") or "movie"),
        title=str(t.get("title", "")),
        series=str(t.get("series", "")),
        season=_int(t.get("season")),
        episode=_int(t.get("episode")),
        year=_int(t.get("year")),
        language=str(t.get("language", "")),
        artist=str(t.get("artist", "")),
        album=str(t.get("album", "")),
        track_number=_int(t.get("track_number")),
        release_name=str(t.get("release_name", "")),
        manifests=manifests,
        drm=[
            Drm(
                str(d.get("system", "")).lower(),
                str(d.get("pssh", "")),
                str(d.get("wrm_header", "")),
                extras={k: v for k, v in d.items() if k not in _DRM_FIELDS},
            )
            for d in t.get("drm") or []
            if isinstance(d, dict)
        ],
        keys=_keys_in(t.get("keys")),
        chapters=list(t.get("chapters") or []),
        tracks=list(t.get("tracks") or []),
        extensions={k: v for k, v in t.items() if k not in _ENTRY_FIELDS},
    )


UNDERSTOOD: frozenset[str] = frozenset()
"""The ``crit`` tokens this reader knows. A reader that adds a feature adds its token here."""


def _check_crit(t: dict[str, Any]) -> None:
    """A title names in ``crit`` the fields a reader must understand to use it at all."""
    if "crit" not in t:
        return
    crit = t["crit"]
    if not isinstance(crit, list) or not crit or not all(isinstance(x, str) and x for x in crit):
        raise ExportError("crit is not a list of field names")
    if len(set(crit)) != len(crit):
        raise ExportError("crit names the same field twice")
    for token in crit:
        if token in _ENTRY_FIELDS or token in ("id", "kind", "crit"):
            raise ExportError(f"crit names {token}, which every reader already understands")
        if token not in t:
            raise ExportError(f"crit names {token}, which the title does not carry")
        if token not in UNDERSTOOD:
            raise ExportError(f"this reader does not understand {token}, which the title requires")


_MANIFEST_FIELDS = frozenset(Manifest.__dataclass_fields__)
_DRM_FIELDS = frozenset(Drm.__dataclass_fields__)
_ENTRY_FIELDS = frozenset(Entry.__dataclass_fields__)


def _headers(raw: Any) -> dict[str, str]:
    """The headers a reader may send, as strings.

    A null value would reach the wire as the string "None", so the header goes instead. A
    ``Cookie`` or ``Authorization`` goes too: the file travels between tools, and one
    tool's session is not another's to replay.
    """
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if v is not None and str(k).lower() not in _SESSION_HEADERS}


_SESSION_HEADERS = frozenset({"cookie", "authorization"})


def _int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _hex(s: Any) -> str:
    return str(s).strip().lower().replace("-", "")


def _merge_key(keys: dict[str, str], kid: Any, key: Any) -> None:
    """Put one pair into ``keys``. Two different content keys for one KID raises, never overwrites.

    Skips an empty or null KID or content key: a legacy track that never licensed holds one.
    An all-zero KID is the same thing: a licence that returned no KID reads back as sixteen
    zero bytes, so two titles would otherwise collide on it and take the file down with them.
    """
    if not kid or not key:
        return
    k, v = _hex(kid), _hex(key)
    if not k.strip("0"):
        return
    if keys.setdefault(k, v) != v:
        # the KID comes from the file, so the message shows only a sane length of it
        raise KeyConflict(f"KID {k[:40]} has two different keys")


def _keys_in(items: Any) -> dict[str, str]:
    """The ``keys`` object, built one pair at a time, so a KID that repeats with a new value raises."""
    if items is None:
        return {}
    if not isinstance(items, dict):
        raise ExportError("keys is not an object")
    keys: dict[str, str] = {}
    for kid, key in items.items():
        _merge_key(keys, kid, key)
    return keys


def guess_type(url: str) -> str:
    """Manifest type from the URL path alone: ``dash``, ``hls``, ``ism`` or ``""``."""
    path = url.split("?", 1)[0].lower()
    if path.endswith(".mpd"):
        return "dash"
    if path.endswith((".m3u8", ".m3u")):
        return "hls"
    if path.endswith((".ism", ".isml", "/manifest")) or ".ism/" in path:
        return "ism"
    return ""


def from_unidl_v1(raw: dict[str, Any]) -> Document:
    doc = Document(
        service_tag=str(raw.get("service", "")),
        service_name=str(raw.get("service_name", "")),
        generator={"app": str(raw.get("app", "unidl"))},
        created=str(raw.get("created", "")),
    )
    for i, t in enumerate(raw.get("titles") or []):
        meta = t.get("title") or {}
        kind = str(meta.get("kind") or "movie")
        drm = t.get("drm") or {}
        keys: dict[str, str] = {}
        for pair in t.get("keys") or []:
            kid, _, key = str(pair).partition(":")
            _merge_key(keys, kid, key)
        extras = {
            k: t[k]
            for k in (
                "summary",
                "note",
                "lyrics",
                "audio_codec_hint",
                "json_manifest",
                "tracks",
                "merge_manifests",
                "manifest_base_url",
                "proxy",
                "is_live",
                "hls_key",
                "hls_iv",
                "hls_method",
                "clear",
            )
            if t.get(k)
        }
        headers = _headers(t.get("headers"))
        manifests = [Manifest(str(u), "", headers, "extra") for u in t.get("alternate_manifest_urls") or []]
        if t.get("manifest_url"):
            manifests.insert(0, Manifest(str(t["manifest_url"]), "", headers))
        # unidl's synthetic json_manifest is app-private; a placeholder URL keeps the entry
        # readable so the keys and metadata are not lost, and x-unidl carries the real thing
        elif t.get("json_manifest"):
            manifests.insert(0, Manifest("x-unidl:json_manifest", "", headers))
        doc.add(
            Entry(
                # unidl does not insist on an id; without a fallback two id-less titles
                # would land on the same Document.add slot and the first one's keys would go
                id=str(meta.get("id") or "") or f"unidl-{i + 1}",
                kind=kind,
                title=str(meta.get("episode_name") or meta.get("name", ""))
                if kind == "episode"
                else str(meta.get("name", "")),
                series=str(meta.get("name", "")) if kind == "episode" else "",
                season=_int(meta.get("season")),
                episode=_int(meta.get("episode")),
                year=_int(meta.get("year")),
                language=str(meta.get("language", "")),
                artist=str(meta.get("artist", "")),
                album=str(meta.get("album", "")),
                track_number=_int(meta.get("track_number")),
                release_name=str(t.get("save_name", "")),
                manifests=manifests,
                drm=[Drm(str(drm.get("system", "")).lower(), str(drm.get("pssh", "")), str(drm.get("wrm_header", "")))]
                if drm
                else [],
                keys=keys,
                chapters=list(t.get("chapters") or []),
                extensions={"x-unidl": extras} if extras else {},
            )
        )
    return doc


def from_unshackle_v2(raw: dict[str, Any]) -> Document:
    doc = Document(
        service_tag=str(raw.get("service", "")),
        region=str(raw.get("region", "")),
        generator={"app": "unshackle"},
    )
    for tid, t in (raw.get("titles") or {}).items():
        meta = t.get("meta") or {}
        tracks = t.get("tracks") or {}
        primary = str(t.get("manifest_url", ""))
        manifests = [Manifest(primary, str(t.get("manifest_type", "")).lower(), role="primary")] if primary else []
        seen = {primary}
        drm: list[Drm] = []
        keys: dict[str, str] = {}
        for tr in tracks.values():
            url = str(tr.get("url", ""))
            if tr.get("descriptor") in ("DASH", "ISM") and url and url not in seen:
                seen.add(url)
                manifests.append(Manifest(url, str(tr["descriptor"]).lower(), role="extra"))
            for d in tr.get("drm") or []:
                # ClearKey (and AES) name a system with no PSSH; only a nameless empty entry is noise
                system, pssh = str(d.get("system", "")).lower(), str(d.get("pssh_b64", ""))
                if (system or pssh) and all((x.system, x.pssh) != (system, pssh) for x in drm):
                    drm.append(Drm(system, pssh))
            for kid, key in (tr.get("keys") or {}).items():
                _merge_key(keys, kid, key)
        kind = str(meta.get("type") or "movie")
        doc.add(
            Entry(
                id=str(tid),
                kind=kind,
                title=str(meta.get("name") or ""),
                series=str(meta.get("series_title") or ""),
                season=_int(meta.get("season")),
                episode=_int(meta.get("number")),
                year=_int(meta.get("year")),
                language=str(meta.get("language") or ""),
                artist=str(meta.get("artist") or ""),
                album=str(meta.get("album") or ""),
                track_number=_int(meta.get("track")),
                manifests=manifests,
                drm=drm,
                keys=keys,
                tracks=[{"id": str(tr.get("id", "")), "url": str(tr["url"])} for tr in tracks.values() if tr.get("url")]
                if not manifests
                else [],
                chapters=[
                    {"start_ms": ts_ms(c["timestamp"]), "title": c.get("name") or ""}
                    for c in t.get("chapters") or []
                    if c.get("timestamp") is not None
                ],
                extensions={
                    "x-unshackle": {
                        "meta": meta,
                        "manifest_type": str(t.get("manifest_type") or ""),
                        "tracks": tracks,
                        "attachments": t.get("attachments") or [],
                    }
                },
            )
        )
    return doc


def ts_ms(ts: Any) -> int:
    """'HH:MM:SS(.mmm)', float seconds, or int milliseconds -> milliseconds."""
    if isinstance(ts, bool):
        return 0
    if isinstance(ts, int):
        return ts
    if isinstance(ts, float):
        return int(ts * 1000)
    parts = str(ts).split(":")
    while len(parts) < 3:
        parts.insert(0, "0")
    h, m, s = parts[-3:]
    return int((int(h) * 3600 + int(m) * 60 + float(s)) * 1000)
