# mediaexport

One export/import file format for streaming download tools. An export is what is left
after the expensive half of a download: the manifest, the DRM init data, the content
keys and the title metadata. Any tool that reads the format can finish the download
with no account and no CDM.

Used by [unshackle](https://github.com/unshackle-dl/unshackle) and
[unidl](https://github.com/chis47747/unidl). Stdlib only, Python 3.10+.

```python
import mediaexport as me

doc = me.read("export.json")          # mediaexport, or legacy unidl v1 / unshackle v2
for entry in doc.titles:
    print(entry.series, entry.title, entry.primary.url, entry.keys)

doc = me.Document(service_tag="SVC", service_name="Example Service")
doc.add(me.Entry(
    id="12345", kind="episode", series="Example Show", title="Pilot",
    season=1, episode=1, year=2026,
    manifests=[me.Manifest("https://cdn.example.com/vod/abc/master.mpd?token=...", "dash", {"User-Agent": "..."})],
    drm=[me.Drm("widevine", pssh="AAAAOHBzc2g...")],
    keys={"0123456789abcdef0123456789abcdef": "fedcba9876543210fedcba9876543210"},
))
me.write("export.json", doc)          # atomic, owner-only permissions
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
- `title` is the item (movie, episode, song). `series` is set only for episodes. There is
  no bare `name`.
- `manifests[0]` is the primary. A reader that handles one manifest takes it and ignores
  `role: extra` entries. `headers` never contain `Cookie`.
- DRM and keys are title-level. Keys are `kid_hex: key_hex`, lower-case, no dashes. A
  reader matches by KID at download time. It reports a track whose KID is missing and
  does not download it. A reader rejects a file where one KID has two different keys.
- `pssh` is the full box, base64, for Widevine and PlayReady alike. `wrm_header` is
  optional beside it.
- `chapters` use `start_ms`, optional `title`, `end_ms`, `kind`.
- `tracks` is informational. The reader reselects from the live manifest. A track with a
  `url` is a side-load the manifest does not know about.
- A reader ignores unknown fields. Every other app carries an `x-<app>` block through
  untouched.
- The file holds keys and usually a signed URL: `write()` is atomic and owner-only.

## Legacy formats

`loads()` converts `kind: unidl-export` (v1) and unshackle's `version: 2` shape in
memory. They are read, never written.
