# Proposal: HLS segments, AES content keys and capabilities

Status: shapes proposed, open questions decided by the unshackle and unidl maintainers (see
the last section). Only the capability rule of section 3 is implemented. References are to [RFC 8216](https://www.rfc-editor.org/rfc/rfc8216) (HLS).

## Problem

Today a title can carry two kinds of media: a manifest the reader fetches and parses again,
and a `tracks[]` row with a `url` that the reader fetches as one complete file. Two cases fit
neither:

- An HLS title encrypted with `AES-128` or `SAMPLE-AES`. Its content key is not for CENC, so
  it has no KID and no place in `keys`. unidl puts `hls_key`, `hls_iv` and `hls_method` in
  its `drm` object, one set for the whole title. One set is not enough: a key URI names a
  content key, but not its IV and not the segments it covers.
- A frozen track: a list of segment URLs the writer resolved from a playlist that the reader
  cannot fetch again. A reader that sees only a URL fetches the init segment as if it were
  the whole file and writes a broken file with no error.

## 1. AES content keys and IVs

What decides the IV (RFC 8216):

- An `EXT-X-KEY` tag applies to every media segment, and to every media initialization
  section that an `EXT-X-MAP` tag declares, between it and the next `EXT-X-KEY` with the same
  `KEYFORMAT` (section 4.3.2.4).
- With no `IV` attribute, the IV of an `AES-128` segment is its media sequence number as a
  128-bit big-endian integer (section 5.2). The number comes from `EXT-X-MEDIA-SEQUENCE` plus
  the position of the segment in the playlist (sections 3 and 4.3.3.2). So one content key URI can
  serve many IVs, and the IV of a segment depends on the playlist of its rendition.
- `EXT-X-KEY` is never inside `EXT-X-MAP`. A map takes the `EXT-X-KEY` declaration in effect where
  the map appears. An `AES-128` map requires an explicit `IV` on that `EXT-X-KEY`
  (section 4.3.2.5), because a map has no media sequence number.
- `SAMPLE-AES` encrypts samples inside the elementary stream, not the whole segment
  (section 4.3.2.4, and the Apple sample encryption specification it references). It
  needs a different decrypter from `AES-128`.

So the file stores the content key once, by the key URI, and stores the IV only where the
playlist does not decide it: on a frozen segment or map.

Proposed title field, a table of content keys:

```json
"aes_keys": [
  {"id": "k1", "method": "AES-128", "uri": "https://cdn.example.com/keys/1", "key": "00112233445566778899aabbccddeeff"},
  {"id": "k2", "method": "SAMPLE-AES", "uri": "https://cdn.example.com/keys/2", "keyformat": "identity", "key": "..."}
]
```

- `uri` is the absolute key URI as the writer resolved it. `key` is 32 hex digits.
- `id` is unique in the title. A frozen segment refers to it (section 2).
- The URI only identifies the content key: the key bytes are in the export, so a reader
  never fetches `uri`. A signed key URI can change its query on each playlist fetch, so a
  reader matches an `EXT-X-KEY` to an entry by the URI with its query string removed.
- A playlist-backed track (the reader fetches the playlist again) takes the IV from the
  playlist as section 5.2 says. The file stores no IV for it.
- Two entries with one `uri` and two different content keys reject the file, as a KID
  conflict does.
- `aes_keys` is separate from `keys` and from `key_pool()`: these content keys have no KID.
- The legacy unidl `drm[].hls_key` and `hls_iv` stay in `Drm.extras` for now. unidl applies
  `hls_iv` to every segment of the title, so it is one explicit IV for the whole title. When
  `aes_keys` lands, the unidl converter maps them to one `aes_keys` entry with that explicit
  title-wide IV (for example an `iv` field on the entry, which overrides section 5.2 for
  every segment the entry covers).

## 2. Frozen tracks and source kinds

A `tracks[]` row gets a `source` kind:

| `source` | `url` is | the reader |
|---|---|---|
| absent or `file` | one complete file | fetches it as one file, as today |
| `playlist` | an HLS media playlist | fetches and parses it, and decrypts from `aes_keys` |
| `segments` | absent, or informational only | never fetches `url`; downloads `segments[]` in order |

A frozen row:

```json
{
  "id": "a1", "type": "audio", "codec": "aac", "language": "en",
  "source": "segments",
  "media_sequence": 120,
  "discontinuity_sequence": 0,
  "maps": [
    {"id": "m1", "url": "https://cdn.example.com/a/init.mp4", "range": [0, 812],
     "enc": {"method": "AES-128", "key": "k1", "iv": "000000000000000000000000000000a0"}},
    {"id": "m2", "url": "https://cdn.example.com/b/init.mp4"}
  ],
  "segments": [
    {"url": "https://cdn.example.com/a/seg120.m4s", "duration": 6.006, "seq": 120, "map": "m1",
     "enc": {"method": "AES-128", "key": "k1", "iv": "00000000000000000000000000000078"}},
    {"url": "https://cdn.example.com/a/seg121.m4s", "duration": 6.006, "seq": 121, "map": "m1",
     "range": [0, 204800], "enc": {"method": "NONE"}},
    {"url": "https://cdn.example.com/b/seg0.m4s", "duration": 4.0, "seq": 122, "discontinuity": true, "map": "m2"}
  ]
}
```

- `segments[]` are objects, in playback order. `url` is absolute.
- `range` is `[offset, length]` in bytes. The writer resolves an `EXT-X-BYTERANGE` with no
  offset (section 4.3.2.2) to an absolute offset, so the reader does no arithmetic.
- `enc` is the encryption of that segment: `method` (`NONE`, `AES-128`, `SAMPLE-AES`), `key`
  (an `aes_keys` id) and `iv`. A missing `enc` is `NONE`.
- A writer MUST write the resolved `iv` on each encrypted frozen segment and map, from the
  `IV` attribute or from section 5.2. A reader MUST NOT derive an IV for a frozen segment:
  an encrypted frozen segment with no `iv` is malformed, and the reader refuses it. `seq`
  is for information only.
- `maps[]` holds each initialization section once. A segment names its map by id, so a map
  change (a new `EXT-X-MAP`, section 4.3.2.5) is a new id. An encrypted map always has `iv`.
- `discontinuity: true` marks an `EXT-X-DISCONTINUITY` (section 4.3.2.3) before the segment.
  `discontinuity_sequence` is the `EXT-X-DISCONTINUITY-SEQUENCE` of the first segment
  (section 4.3.3.3).
- `duration` is the `EXTINF` value in seconds (section 4.3.2.1). `program_date_time` is
  optional.

A reader that does not support segmented sources must never see these rows as plain
side-loads, so a title with any row whose `source` is not `file` lists a token in `crit`
(section 3).

## 3. Capabilities

`crit` tokens name shared features with neutral names, not app blocks. A tool that reads a
unidl file needs no knowledge of `x-unidl`:

| Token | The title uses |
|---|---|
| `playlist` | a `tracks[]` row with `source: playlist` |
| `segments` | a `tracks[]` row with `source: segments` |
| `hls-aes` | an `AES-128` entry in `aes_keys` |
| `sample-aes` | a `SAMPLE-AES` entry in `aes_keys` |

A caller declares what it implements through the reader API from this branch:

```python
doc = mediaexport.read(path, understood={"segments", "hls-aes"})
for r in doc.refused:
    log.warning(f"{r.raw.get('id')}: {r.reason}")
```

The package default is none: a newer package does not make a tool claim a feature that the
tool did not add. A title that needs a token outside `understood` goes to `doc.refused`, and
a rewrite keeps it unchanged.

These tokens are capability names, not title fields. The reader on this branch already
applies that rule: it no longer requires a field of the same name. A reader from before this
branch rejects the whole file on such a token, which fails safe.

## Version

No `version` bump. Each addition is new fields behind `crit`, and a row with no `source`
keeps its meaning today. A reader that does not know a token refuses that title. A reader
from before the per-title refusal rejects the whole file, which fails safe. A bump is
necessary only if the maintainers change the meaning of an existing field, for example if a
`tracks[]` row with a `url` and no `source` stops meaning one complete file.

## Decisions

The maintainers decided the open questions:

1. `crit` tokens are capability names (`segments`, `hls-aes`, `sample-aes`), not field
   names. The reader no longer checks for a field of the same name. The list stays
   non-empty, with non-empty strings, no duplicates, and no base field, `id`, `kind` or
   `crit`. A malformed `crit` rejects the file. Implemented.
2. A reader matches an `EXT-X-KEY` to an `aes_keys` entry by the key URI with its query
   string removed. The content key bytes and the IV are in the export, so the URI only
   identifies the content key.
3. A writer always writes the resolved IV on each frozen segment. A reader never derives an
   IV for a frozen segment.
4. unidl's `hls_iv` is one explicit IV for the whole title. It stays in `Drm.extras` until
   `aes_keys` lands. Then the converter maps it to one `aes_keys` entry with that IV.
5. The `keys` of a refused title take part in the KID conflict check of `loads()`,
   `key_pool()` and `dumps()`, with the same normalisation: `keys` is a base field, and a
   writer must not produce a file that a capable reader rejects. They are for that check
   only, never for use. Implemented.

Still open:

- Should `SAMPLE-AES-CTR` and `KEYFORMAT` values other than `identity` get their own tokens,
  or stay out of scope?
- Are frozen segment URLs expected to outlive their signature? If not, is `segments` worth
  the size, or only for services whose playlists cannot be fetched again?
