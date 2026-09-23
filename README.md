# mediaexport

One export/import file format for streaming download tools. An export is what is left
after the expensive half of a download: the manifest, the DRM init data, the content
keys and the title metadata. Any tool that reads the format can finish the download
with no account and no CDM.

Used by [unshackle](https://github.com/unshackle-dl/unshackle) and
[unidl](https://github.com/chis47747/unidl). Stdlib only, Python 3.10+.

```python
import mediaexport as me

doc = me.read("export.json")  # mediaexport, or legacy unidl v1 / unshackle v2
for entry in doc.titles:
    print(entry.series, entry.title, entry.primary.url, entry.keys)

doc = me.Document(service_tag="SVC", service_name="Example Service")
doc.add(
    me.Entry(
        id="12345",
        kind="episode",
        series="Example Show",
        title="Pilot",
        season=1,
        episode=1,
        year=2026,
        manifests=[me.Manifest("https://cdn.example.com/vod/abc/master.mpd?token=...", "dash", {"User-Agent": "..."})],
        drm=[me.Drm("widevine", pssh="AAAAOHBzc2g...")],
        keys={"0123456789abcdef0123456789abcdef": "fedcba9876543210fedcba9876543210"},
    )
)
me.write("export.json", doc)  # atomic, owner-only permissions
```

## The file

```jsonc
{
  "kind": "mediaexport",
  "version": 1,
  "generator": {"app": "yourtool", "version": "1.0"},
  "created": "2026-09-12T14:04:19",
  "service": {"tag": "SVC", "name": "Example Service"},
  "region": "us",
  "titles": [
    {
      "id": "12345",
      "kind": "episode",
      "series": "Example Show",
      "title": "Pilot",
      "season": 1, "episode": 1, "year": 2026, "language": "en",
      "release_name": "Example.Show.S01E01.Pilot.2160p.WEB-DL...",
      "manifests": [{"url": "https://cdn.example.com/vod/abc/master.mpd?token=...", "type": "dash", "headers": {"User-Agent": "..."}}],
      "drm": [{"system": "widevine", "pssh": "AAAAOHBzc2g..."}],
      "keys": {"0123456789abcdef0123456789abcdef": "fedcba9876543210fedcba9876543210"},
      "chapters": [{"start_ms": 0, "title": "Cold Open"}],
      "tracks": [{"type": "video", "codec": "hevc", "width": 3840, "height": 2160, "selected": true}],
      "x-yourtool": {}
    }
  ]
}
```

## Rules

- A reader checks `kind` and `version` first. It accepts any version up to its own and
  rejects a newer one. Additive fields never bump the version.
- Every title has an `id`, unique in the file: a writer that exports one title twice
  replaces it. A title with no id gets one from its position in the file, so two such titles
  stay two titles. `kind` is `movie`, `episode`, `song` or `clip`; a reader treats an unknown
  kind as `movie`.
- `title` is the item (movie, episode, song). `series` is set only for episodes. There is
  no bare `name`.
- The primary manifest is the one with `role: primary`, else the first one without
  `role: extra`. A writer puts it first. A reader that handles one manifest takes the
  primary and ignores the rest. A reader identifies a manifest by its whole URL: two
  profiles on one endpoint are two manifests. `type` is `dash`, `hls` or `ism`; a reader
  guesses a missing type from the URL. `headers` hold only the request headers a CDN can
  gate a manifest on: `User-Agent`, `Referer`, `Origin`, `Accept` and `Accept-Language`,
  in any case. The package drops every other header, and a null value, on read and on
  write: an HTTP session can hide in any custom header, and one tool cannot replay another's.
- DRM and keys are title-level. `keys` is an object of `kid_hex: key_hex`, lower-case, no
  dashes, 32 hex digits each; any other shape rejects the file. A reader matches by KID at download time. It
  reports a track whose KID is missing and does not download it. A reader normalises a KID
  before it compares it. A KID that has two different keys rejects the file, whether the
  two are in one title, in two titles, or come out of a legacy conversion. A KID of all
  zeros is a licence that returned no KID, so a reader drops the pair instead of reading it
  as a conflict. `dumps()` and
  `write()` apply the same check, so a writer cannot produce a file the reader refuses. This
  one failure raises `KeyConflict`, an `ExportError`: the file itself is readable, so a
  writer keeps it and reports the conflict, and quarantines only a file it cannot read.
- A writer adds a content key with `Entry.add_key(kid, key)`, which normalises the pair and raises
  on a conflict. A writer that merges keys into an existing file and meets a conflict keeps
  the content key the file already has, logs a warning and does not overwrite it: the download in
  progress continues, and the file stays readable.
- `pssh` is the full box, base64, for Widevine and PlayReady alike. `wrm_header` is
  optional beside it.
- `manifests`, `drm`, `chapters` and `tracks` are lists of objects. A reader drops an entry
  that is not an object, and reads a field that is not a list as an empty list.
- `chapters` use `start_ms`, an integer, and optional `title`, `end_ms`, `kind`. A reader
  drops a chapter with no integer `start_ms`.
- `tracks` is informational. The reader reselects from the live manifest. `type` is
  `video`, `audio` or `subtitle`. A track with a `url` is a side-load the manifest does not
  know about: a reader adds it to what the manifest gives. For a subtitle side-load, `codec`
  is the file format (`srt`, `vtt`, `ttml`, `ass`, `ssa`, `smi`) and `language` is set.
- A reader ignores unknown fields and writes them back as they came, at title level and
  inside each `manifests[]` and `drm[]` entry (`Entry.extensions`, `Manifest.extras`,
  `Drm.extras`). Every other app carries an `x-<app>` block through untouched. A tool that
  rewrites a file it did not create loses nothing from it.
- A title lists in `crit` the fields a reader must understand to use it at all. Each entry
  names a field the title carries, never a field of the base format, and the list has no
  duplicates and is never empty. A reader that does not know an entry refuses that title
  and says which name stopped it. `UNDERSTOOD` holds the names this reader knows, so a
  reader that adds a feature adds its name there. Silence is the reason the rule exists: a
  reader that ignored `crit` would write a file it could not decrypt.
- The file holds keys and usually a signed URL: `write()` is atomic and owner-only.

## Legacy formats

`loads()` converts `kind: unidl-export` (v1) and unshackle's `version: 2` shape in
memory. They are read, never written. A legacy file that does not have the shape of its
format raises `ExportError`. A unidl title with no id of its own gets one from its
position in the file, so two of them stay two titles. Fields with no shared meaning, such as
unidl's HLS AES `hls_key`, `hls_iv` and `hls_method`, come through under `x-unidl`.
