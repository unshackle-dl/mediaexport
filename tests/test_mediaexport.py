import json
import os
import stat
from pathlib import Path

import pytest

import mediaexport as me

FIXTURES = Path(__file__).parent / "fixtures"
TITLE_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def unidl() -> me.Document:
    return me.read(FIXTURES / "unidl_v1.json")


@pytest.fixture
def unshackle() -> me.Document:
    return me.read(FIXTURES / "unshackle_v2.json")


def test_both_legacy_files_converge_on_the_same_title(unidl: me.Document, unshackle: me.Document) -> None:
    a, b = unidl.get(TITLE_ID), unshackle.get(TITLE_ID)
    assert a is not None and b is not None
    assert (a.kind, a.series, a.title, a.season, a.episode) == ("episode", "Example Show", "Pilot", 2, 1)
    assert (b.kind, b.series, b.title, b.season, b.episode) == ("episode", "Example Show", "Pilot", 2, 1)
    assert a.drm[0].system == b.drm[0].system == "widevine"
    assert a.drm[0].pssh in {d.pssh for d in b.drm}
    # each tool licensed only the audio it downloaded, so only the video KID is in both
    assert set(a.keys) & set(b.keys) == {"01" * 16}


def test_legacy_specifics_survive_in_extensions(unidl: me.Document, unshackle: me.Document) -> None:
    assert unidl.get(TITLE_ID).primary.headers["User-Agent"].startswith("Mozilla")
    assert "summary" in unidl.get(TITLE_ID).ext("unidl")
    x = unshackle.get(TITLE_ID).ext("unshackle")
    assert x["meta"]["series_title"] == "Example Show"
    assert len(x["tracks"]) == 5
    assert unshackle.get(TITLE_ID).primary.type == "dash"


def test_round_trip_is_lossless(unidl: me.Document, unshackle: me.Document) -> None:
    for doc in (unidl, unshackle):
        again = me.loads(me.dumps(doc))
        assert again.key_pool() == doc.key_pool()
        assert [e.id for e in again.titles] == [e.id for e in doc.titles]
        assert again.get(TITLE_ID).extensions == doc.get(TITLE_ID).extensions
        assert again.get(TITLE_ID).manifests == doc.get(TITLE_ID).manifests


def test_dumps_shape() -> None:
    doc = me.Document(service_tag="X", titles=[me.Entry("1", "movie", "M", manifests=[me.Manifest("https://a/b.mpd")])])
    raw = json.loads(me.dumps(doc))
    assert raw["kind"] == me.KIND and raw["version"] == me.VERSION
    assert raw["service"] == {"tag": "X", "name": ""}
    assert raw["titles"][0] == {"id": "1", "kind": "movie", "title": "M", "manifests": [{"url": "https://a/b.mpd"}]}


def test_write_is_owner_only(tmp_path: Path) -> None:
    doc = me.Document(service_tag="X", titles=[me.Entry("1", "movie", "M", manifests=[me.Manifest("https://a/b.mpd")])])
    out = me.write(tmp_path / "e.json", doc)
    if os.name != "nt":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert me.read(out).titles[0].title == "M"


def test_add_replaces_by_id() -> None:
    doc = me.Document(service_tag="X")
    doc.add(me.Entry("1", "movie", "old", manifests=[me.Manifest("u")]))
    doc.add(me.Entry("1", "movie", "new", manifests=[me.Manifest("u")]))
    assert [e.title for e in doc.titles] == ["new"]


def test_key_pool_merges_titles_and_rejects_a_conflicting_kid() -> None:
    doc = me.Document(service_tag="X")
    doc.add(me.Entry("1", "movie", "a", manifests=[me.Manifest("u")], keys={"k": "1"}))
    doc.add(me.Entry("2", "movie", "b", manifests=[me.Manifest("u")], keys={"k": "1", "j": "3"}))
    assert doc.key_pool() == {"k": "1", "j": "3"}
    doc.add(me.Entry("3", "movie", "c", manifests=[me.Manifest("u")], keys={"k": "2"}))
    with pytest.raises(me.ExportError, match="KID k"):
        doc.key_pool()
    with pytest.raises(me.ExportError, match="KID k"):
        me.loads(me.dumps(doc))


def test_unidl_alternate_manifests_become_extras() -> None:
    raw = {
        "kind": "unidl-export",
        "version": 1,
        "service": "x",
        "titles": [
            {
                "title": {"id": "1", "kind": "movie", "name": "M"},
                "manifest_url": "https://a/1.mpd",
                "alternate_manifest_urls": ["https://a/2.mpd"],
                "merge_manifests": True,
                "headers": {"Referer": "https://a/"},
            }
        ],
    }
    e = me.loads(json.dumps(raw)).titles[0]
    assert e.primary.url == "https://a/1.mpd"
    assert [(m.url, m.role, m.headers) for m in e.manifests[1:]] == [
        ("https://a/2.mpd", "extra", {"Referer": "https://a/"})
    ]
    assert e.ext("unidl")["merge_manifests"] is True


@pytest.mark.parametrize(
    "text,msg",
    [
        ("[]", "not an export"),
        ('{"kind": "other"}', "not a mediaexport"),
        ('{"kind": "mediaexport", "version": 99, "service": {"tag": "X"}, "titles": [{}]}', "newer"),
        ('{"kind": "mediaexport", "version": 1, "service": {}, "titles": [{}]}', "which service"),
        ('{"kind": "mediaexport", "version": 1, "service": {"tag": "X"}, "titles": []}', "no titles"),
        ('{"kind": "mediaexport", "version": 1, "service": {"tag": "X"}, "titles": [{"id": "1"}]}', "no manifest"),
    ],
)
def test_rejections_name_the_reason(text: str, msg: str) -> None:
    with pytest.raises(me.ExportError, match=msg):
        me.loads(text)


def test_unknown_fields_and_extensions_are_kept() -> None:
    raw = {
        "kind": "mediaexport",
        "version": 1,
        "service": {"tag": "X"},
        "future_field": 1,
        "titles": [
            {"id": "1", "kind": "movie", "title": "M", "manifests": [{"url": "u"}], "x-foo": {"a": 1}, "new": 2}
        ],
    }
    doc = me.loads(json.dumps(raw))
    assert doc.titles[0].extensions == {"x-foo": {"a": 1}, "new": 2}
    again = json.loads(me.dumps(doc))["titles"][0]
    assert again["x-foo"] == {"a": 1} and again["new"] == 2


def test_unknown_manifest_and_drm_fields_survive_a_read_modify_write() -> None:
    manifest = {"url": "https://a/b.m3u8", "type": "hls", "media_sequence": 0, "encrypted": False}
    drm = {"system": "aes-128", "key": "k", "iv": "i", "key_uri": "https://a/key", "media_sequence": 0}
    raw = {
        "kind": "mediaexport",
        "version": 1,
        "service": {"tag": "X"},
        "titles": [
            {"id": "1", "kind": "movie", "title": "M", "requires": ["aes"], "manifests": [manifest], "drm": [drm]}
        ],
    }
    entry = me.loads(json.dumps(raw)).titles[0]
    assert entry.manifests[0].extras == {"media_sequence": 0, "encrypted": False}
    assert entry.drm[0].extras == {"key": "k", "iv": "i", "key_uri": "https://a/key", "media_sequence": 0}
    entry.title = "Renamed"
    doc = me.Document(service_tag="X", titles=[entry])
    again = json.loads(me.dumps(doc))["titles"][0]
    assert again["title"] == "Renamed" and again["requires"] == ["aes"]
    assert again["manifests"] == [manifest] and again["drm"] == [drm]


def test_dumps_rejects_a_document_the_reader_would_refuse() -> None:
    doc = me.Document(service_tag="X")
    doc.add(me.Entry("1", "movie", "a", manifests=[me.Manifest("u")], keys={"k": "1"}))
    doc.add(me.Entry("2", "movie", "b", manifests=[me.Manifest("u")], keys={"k": "2"}))
    with pytest.raises(me.ExportError, match="KID k"):
        me.dumps(doc)


@pytest.mark.parametrize("keys", ["5", "true", '"abc"', '["ab"]', "[]"])
def test_keys_that_are_not_an_object_are_rejected(keys: str) -> None:
    title = '{"id": "1", "kind": "movie", "title": "M", "manifests": [{"url": "u"}], "keys": ' + keys + "}"
    text = '{"kind": "mediaexport", "version": 1, "service": {"tag": "X"}, "titles": [' + title + "]}"
    with pytest.raises(me.ExportError, match="keys is not an object"):
        me.loads(text)


def test_a_null_key_on_one_track_is_not_a_conflict() -> None:
    raw = {
        "version": 2,
        "service": "SVC",
        "titles": {
            "1": {
                "meta": {"type": "movie", "name": "M"},
                "manifest_url": "https://a/b.mpd",
                "tracks": {
                    "v": {"id": "v", "keys": {"01" * 16: None}},
                    "a": {"id": "a", "keys": {"01" * 16: "a1" * 16}},
                },
            }
        },
    }
    assert me.loads(json.dumps(raw)).titles[0].keys == {"01" * 16: "a1" * 16}


def test_add_key_normalises_and_rejects_a_conflict() -> None:
    e = me.Entry("1", "movie", "M", manifests=[me.Manifest("u")])
    e.add_key("0A-" + "01" * 15, "A1" * 16)
    e.add_key("0a" + "01" * 15, "a1" * 16)
    assert e.keys == {"0a" + "01" * 15: "a1" * 16}
    with pytest.raises(me.ExportError, match="two different keys"):
        e.add_key("0a" + "01" * 15, "b2" * 16)


def test_direct_url_title_needs_no_manifest() -> None:
    raw = {
        "kind": "mediaexport",
        "version": 1,
        "service": {"tag": "X"},
        "titles": [{"id": "1", "kind": "movie", "title": "M", "tracks": [{"type": "video", "url": "https://a/v.mp4"}]}],
    }
    entry = me.loads(json.dumps(raw)).titles[0]
    assert entry.primary is None and entry.tracks[0]["url"] == "https://a/v.mp4"


def test_primary_skips_extras() -> None:
    e = me.Entry("1", "movie", "M", manifests=[me.Manifest("a", role="extra"), me.Manifest("b")])
    assert e.primary.url == "b"
    assert me.Entry("1", "movie", "M", manifests=[me.Manifest("a", role="extra")]).primary is None


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://a/b.mpd?x=1", "dash"),
        ("https://a/b.m3u8", "hls"),
        ("https://a/b.ism/Manifest", "ism"),
        ("https://a/b", ""),
    ],
)
def test_guess_type(url: str, expected: str) -> None:
    assert me.guess_type(url) == expected


@pytest.mark.parametrize("ts,ms", [("00:01:02.500", 62500), ("1:02", 62000), (62.5, 62500), (62500, 62500)])
def test_timestamp_to_ms(ts, ms) -> None:
    assert me.ts_ms(ts) == ms


def test_a_kid_that_repeats_inside_one_title_raises() -> None:
    kid = "0A" + "01" * 15
    raw = {
        "kind": "mediaexport",
        "version": 1,
        "service": {"tag": "X"},
        "titles": [
            {
                "id": "1",
                "kind": "movie",
                "title": "M",
                "manifests": [{"url": "https://a/b.mpd"}],
                "keys": {kid: "a1" * 16, kid.lower().replace("0a", "0a-"): "b2" * 16},
            }
        ],
    }
    with pytest.raises(me.ExportError, match="two different keys"):
        me.loads(json.dumps(raw))


def test_unidl_duplicate_kid_in_one_title_raises() -> None:
    raw = {
        "kind": "unidl-export",
        "version": 1,
        "service": "SVC",
        "titles": [
            {
                "title": {"id": "1", "kind": "movie", "name": "M"},
                "manifest_url": "https://a/b.mpd",
                "keys": [f"{'01' * 16}:{'a1' * 16}", f"{'01' * 16}:{'b2' * 16}"],
            }
        ],
    }
    with pytest.raises(me.ExportError, match="two different keys"):
        me.loads(json.dumps(raw))


def test_unshackle_tracks_that_disagree_on_a_kid_raise() -> None:
    raw = {
        "version": 2,
        "service": "SVC",
        "titles": {
            "1": {
                "meta": {"type": "movie", "name": "M"},
                "manifest_url": "https://a/b.mpd",
                "tracks": {
                    "v": {"id": "v", "keys": {"01" * 16: "a1" * 16}},
                    "a": {"id": "a", "keys": {"01" * 16: "b2" * 16}},
                },
            }
        },
    }
    with pytest.raises(me.ExportError, match="two different keys"):
        me.loads(json.dumps(raw))


def test_two_profiles_on_one_endpoint_stay_two_manifests() -> None:
    raw = {
        "version": 2,
        "service": "SVC",
        "titles": {
            "1": {
                "meta": {"type": "movie", "name": "M"},
                "manifest_url": "https://a/manifest?profile=hd",
                "manifest_type": "DASH",
                "tracks": {
                    "v": {"id": "v", "descriptor": "DASH", "url": "https://a/manifest?profile=uhd"},
                },
            }
        },
    }
    entry = me.loads(json.dumps(raw)).titles[0]
    assert [m.url for m in entry.manifests] == ["https://a/manifest?profile=hd", "https://a/manifest?profile=uhd"]


def test_unidl_titles_without_an_id_stay_apart() -> None:
    raw = {
        "kind": "unidl-export",
        "version": 1,
        "service": "SVC",
        "titles": [
            {
                "title": {"kind": "movie", "name": "A"},
                "manifest_url": "https://a/1.mpd",
                "keys": [f"{'01' * 16}:{'a1' * 16}"],
            },
            {
                "title": {"kind": "movie", "name": "B"},
                "manifest_url": "https://a/2.mpd",
                "keys": [f"{'02' * 16}:{'b2' * 16}"],
            },
        ],
    }
    doc = me.loads(json.dumps(raw))
    assert [e.title for e in doc.titles] == ["A", "B"]
    assert len(doc.key_pool()) == 2


def test_unidl_hls_aes_fields_survive_in_the_drm_entry() -> None:
    aes = {"hls_key": "00" * 16, "hls_iv": "11" * 16, "hls_method": "AES-128", "clear": False}
    title = {"title": {"id": "1", "kind": "movie", "name": "M"}, "manifest_url": "https://a/b.m3u8"}
    entry = me.loads(_unidl(title | {"drm": {"system": "clearkey"} | aes})).titles[0]
    assert (entry.drm[0].system, entry.drm[0].extras) == ("clearkey", aes)
    again = json.loads(me.dumps(me.loads(me.dumps(me.Document("X", titles=[entry])))))["titles"][0]
    assert again["drm"] == [{"system": "clearkey"} | aes]


def test_unshackle_drm_without_a_pssh_is_kept_once() -> None:
    raw = {
        "version": 2,
        "service": "SVC",
        "titles": {
            "1": {
                "meta": {"type": "movie", "name": "M"},
                "manifest_url": "https://a/b.m3u8",
                "tracks": {
                    "v": {"id": "v", "drm": [{"system": "clearkey"}]},
                    "a": {"id": "a", "drm": [{"system": "clearkey"}, {"system": "widevine", "pssh_b64": "AAAA"}]},
                },
            }
        },
    }
    assert [(d.system, d.pssh) for d in me.loads(json.dumps(raw)).titles[0].drm] == [
        ("clearkey", ""),
        ("widevine", "AAAA"),
    ]


def test_a_cookie_or_null_header_is_dropped_on_read_and_on_write() -> None:
    raw = {
        "kind": "mediaexport",
        "version": 1,
        "service": {"tag": "X"},
        "titles": [
            {
                "id": "1",
                "kind": "movie",
                "title": "M",
                "manifests": [{"url": "u", "headers": {"Cookie": "sid=1", "X-Null": None, "Referer": "https://a/"}}],
            }
        ],
    }
    entry = me.loads(json.dumps(raw)).titles[0]
    assert entry.manifests[0].headers == {"Referer": "https://a/"}
    entry.manifests[0].headers["cookie"] = "sid=2"
    doc = me.Document(service_tag="X", titles=[entry])
    assert json.loads(me.dumps(doc))["titles"][0]["manifests"][0]["headers"] == {"Referer": "https://a/"}


def test_a_key_conflict_is_its_own_error() -> None:
    doc = me.Document(service_tag="X")
    doc.add(me.Entry("1", "movie", "a", manifests=[me.Manifest("u")], keys={"01" * 16: "a1" * 16}))
    doc.add(me.Entry("2", "movie", "b", manifests=[me.Manifest("u")], keys={"01" * 16: "b2" * 16}))
    with pytest.raises(me.KeyConflict):
        me.dumps(doc)
    assert issubclass(me.KeyConflict, me.ExportError)


def test_an_authorization_header_is_dropped_like_a_cookie() -> None:
    raw = {
        "kind": "mediaexport",
        "version": 1,
        "service": {"tag": "X"},
        "titles": [
            {
                "id": "1",
                "kind": "movie",
                "title": "M",
                "manifests": [{"url": "u", "headers": {"authorization": "Bearer t", "User-Agent": "ua"}}],
            }
        ],
    }
    assert me.loads(json.dumps(raw)).titles[0].manifests[0].headers == {"User-Agent": "ua"}


def test_titles_without_an_id_stay_apart() -> None:
    raw = {
        "kind": "mediaexport",
        "version": 1,
        "service": {"tag": "X"},
        "titles": [
            {"kind": "movie", "title": "A", "manifests": [{"url": "https://a/1.mpd"}]},
            {"kind": "movie", "title": "B", "manifests": [{"url": "https://a/2.mpd"}]},
        ],
    }
    doc = me.loads(json.dumps(raw))
    assert [(e.id, e.title) for e in doc.titles] == [("title-1", "A"), ("title-2", "B")]
    assert doc.get("title-2") is not None


def _one_title(**extra: object) -> str:
    body: dict[str, object] = {"id": "1", "kind": "movie", "title": "M", "manifests": [{"url": "https://a/b.mpd"}]}
    body.update(extra)
    return json.dumps({"kind": "mediaexport", "version": 1, "service": {"tag": "X"}, "titles": [body]})


@pytest.mark.parametrize(
    "title,message",
    [
        ({"crit": []}, "not a list"),
        ({"crit": "segments"}, "not a list"),
        ({"crit": ["a", "a"], "a": 1}, "same capability twice"),
        ({"crit": ["title"]}, "every reader already understands"),
    ],
)
def test_a_malformed_crit_rejects_the_file(title: dict, message: str) -> None:
    with pytest.raises(me.ExportError, match=message):
        me.loads(_one_title(**title), understood={"a", "segments", "title"})


def test_a_crit_token_the_caller_understands_is_accepted_and_kept() -> None:
    text = _one_title(crit=["segments"], segments=[{"url": "https://a/1.m4s"}])
    entry = me.loads(text, understood={"segments"}).titles[0]
    assert entry.extensions["crit"] == ["segments"]
    assert json.loads(me.dumps(me.loads(me.dumps(me.Document("X", titles=[entry])))))["titles"][0]["crit"] == [
        "segments"
    ]


def test_an_all_zero_kid_never_conflicts() -> None:
    zero = "0" * 32
    raw = {
        "kind": "mediaexport",
        "version": 1,
        "service": {"tag": "X"},
        "titles": [
            {"id": "1", "kind": "movie", "title": "M", "manifests": [{"url": "u"}], "keys": {zero: "a1" * 16}},
            {"id": "2", "kind": "movie", "title": "N", "manifests": [{"url": "u"}], "keys": {zero: "b2" * 16}},
        ],
    }
    doc = me.loads(json.dumps(raw))
    assert doc.key_pool() == {}
    assert all(e.keys == {} for e in doc.titles)


@pytest.mark.parametrize(
    "kid,key",
    [
        ("zz", "a1" * 16),
        ("01" * 15, "a1" * 16),
        ("01" * 17, "a1" * 16),
        ("0g" * 16, "a1" * 16),
        ("01" * 16, "zz"),
        ("01" * 16, "a1" * 15),
        ("01" * 16, True),
    ],
)
def test_a_kid_or_key_that_is_not_32_hex_digits_is_rejected(kid: str, key: object) -> None:
    with pytest.raises(me.ExportError, match="not 32 hex digits"):
        me.loads(_one_title(keys={kid: key}))


def test_a_malformed_unidl_key_pair_is_rejected() -> None:
    raw = {
        "kind": "unidl-export",
        "version": 1,
        "service": "SVC",
        "titles": [{"title": {"id": "1"}, "keys": ["zz:11"]}],
    }
    with pytest.raises(me.ExportError, match="not 32 hex digits"):
        me.loads(json.dumps(raw))


@pytest.mark.parametrize("field", ["tracks", "chapters", "drm"])
@pytest.mark.parametrize("value", [5, "abc", {"a": 1}, ["x", 1, None, []]])
def test_a_row_that_is_not_an_object_is_dropped(field: str, value: object) -> None:
    entry = me.loads(_one_title(**{field: value})).titles[0]
    assert getattr(entry, field) == []


def test_object_rows_survive_beside_dropped_ones() -> None:
    track = {"type": "video", "codec": "hevc"}
    entry = me.loads(_one_title(tracks=["x", track], drm=[1, {"system": "widevine"}])).titles[0]
    assert entry.tracks == [track]
    assert [d.system for d in entry.drm] == ["widevine"]


@pytest.mark.parametrize("manifests", [5, "https://a/b.mpd"])
def test_manifests_that_are_not_a_list_are_no_manifest(manifests: object) -> None:
    text = _one_title(manifests=manifests, tracks=5)
    with pytest.raises(me.ExportError, match="no manifest"):
        me.loads(text)


def test_chapters_read_back_with_an_int_start() -> None:
    chapters = [
        {"start_ms": "abc", "title": "bad"},
        {"title": "no start"},
        {"start_ms": None},
        {"start_ms": 0, "title": "A"},
        {"start_ms": "1500", "title": 7, "end_ms": 2000},
    ]
    entry = me.loads(_one_title(chapters=chapters)).titles[0]
    assert entry.chapters == [{"start_ms": 0, "title": "A"}, {"start_ms": 1500, "title": "7", "end_ms": 2000}]


def _unidl(title: object) -> str:
    return json.dumps({"kind": "unidl-export", "version": 1, "service": "SVC", "titles": [title]})


def _v2(title: object) -> str:
    return json.dumps({"version": 2, "service": "SVC", "titles": {"1": title}})


@pytest.mark.parametrize(
    "text",
    [
        _unidl("x"),
        _unidl({"title": "x", "manifest_url": "https://a/b.mpd"}),
        _unidl({"title": {"id": "1"}, "manifest_url": "https://a/b.mpd", "drm": "widevine"}),
        _unidl({"title": {"id": "1"}, "manifest_url": "https://a/b.mpd", "keys": "abc"}),
        _unidl({"title": {"id": "1"}, "manifest_url": "https://a/b.mpd", "keys": {"01" * 16: "a1" * 16}}),
        json.dumps({"kind": "unidl-export", "version": 1, "service": "SVC", "titles": "abc"}),
        _v2("x"),
        _v2({"meta": "x", "manifest_url": "https://a/b.mpd"}),
        _v2({"manifest_url": "https://a/b.mpd", "tracks": ["x"]}),
        _v2({"manifest_url": "https://a/b.mpd", "tracks": {"v": "x"}}),
        _v2({"manifest_url": "https://a/b.mpd", "tracks": {"v": {"keys": ["x"]}}}),
        _v2({"manifest_url": "https://a/b.mpd", "tracks": {"v": {"drm": ["x"]}}}),
        _v2({"manifest_url": "https://a/b.mpd", "chapters": [{"timestamp": "x:y"}]}),
        _v2({"manifest_url": "https://a/b.mpd", "chapters": ["x"]}),
    ],
)
def test_a_malformed_legacy_file_raises_export_error(text: str) -> None:
    with pytest.raises(me.ExportError):
        me.loads(text)


@pytest.mark.parametrize(
    "text",
    [
        "[" * 100_000,
        _one_title(season=float("inf")),
        _one_title().replace('"version": 1', '"version": Infinity'),
        _one_title().replace('"titles"', '"generator": 5, "titles"'),
        _one_title().replace('"titles"', '"generator": "ab", "titles"'),
    ],
)
def test_hostile_json_raises_export_error_or_reads(text: str) -> None:
    try:
        me.loads(text)
    except me.ExportError:
        pass


def test_only_allowlisted_headers_are_kept_on_read_and_on_write() -> None:
    headers = {
        "user-agent": "ua",
        "Referer": "https://a/",
        "Origin": "https://a",
        "Accept": "*/*",
        "Accept-Language": "en",
        "X-Api-Key": "k",
        "X-Auth-Token": "t",
        "X-Session-Id": "s",
    }
    kept = {k: headers[k] for k in ("user-agent", "Referer", "Origin", "Accept", "Accept-Language")}
    entry = me.loads(_one_title(manifests=[{"url": "u", "headers": headers}])).titles[0]
    assert entry.manifests[0].headers == kept
    entry.manifests[0].headers = headers
    doc = me.Document(service_tag="X", titles=[entry])
    assert json.loads(me.dumps(doc))["titles"][0]["manifests"][0]["headers"] == kept


def test_unidl_headers_are_filtered_to_the_allowlist() -> None:
    raw = {"title": {"id": "1"}, "manifest_url": "https://a/b.mpd", "headers": {"User-Agent": "ua", "X-Token": "t"}}
    assert me.loads(_unidl(raw)).titles[0].primary.headers == {"User-Agent": "ua"}


def test_dumps_rejects_a_kid_the_reader_would_refuse() -> None:
    doc = me.Document(
        service_tag="X", titles=[me.Entry("1", "movie", "M", manifests=[me.Manifest("u")], keys={"zz": "1"})]
    )
    with pytest.raises(me.ExportError, match="not 32 hex digits"):
        me.dumps(doc)


KID = "01" * 16
TWO_KEYS = f'{{"{KID}": "{"11" * 16}", "{KID}": "{"22" * 16}"}}'


@pytest.mark.parametrize(
    "text",
    [
        _one_title(keys={}).replace('"keys": {}', f'"keys": {TWO_KEYS}'),
        _one_title().replace('"titles"', f'"keys": {TWO_KEYS}, "titles"'),
        _one_title().replace('"id": "1"', '"id": "1", "id": "2"'),
        _one_title().replace('"kind": "mediaexport"', '"kind": "mediaexport", "kind": "mediaexport"'),
        _unidl({"title": {"id": "1"}, "manifest_url": "u"}).replace('"id": "1"', '"id": "1", "id": "2"'),
    ],
)
def test_a_repeated_property_name_is_rejected(text: str) -> None:
    with pytest.raises(me.ExportError, match="more than once"):
        me.loads(text)


def _entry(id: str = "1", **keys: str) -> me.Entry:
    return me.Entry(id, "movie", "M", manifests=[me.Manifest("u")], keys=dict(keys))


@pytest.mark.parametrize(
    "titles",
    [
        [_entry(**{"0A" + "01" * 15: "a1" * 16, "0a" + "01" * 15: "b2" * 16})],
        [_entry(**{"0a-" + "01" * 15: "a1" * 16, "0a" + "01" * 15: "b2" * 16})],
        [_entry("1", **{"0A" + "01" * 15: "a1" * 16}), _entry("2", **{"0a" + "01" * 15: "b2" * 16})],
    ],
)
def test_dumps_rejects_kids_that_collide_once_normalised(titles: list[me.Entry]) -> None:
    with pytest.raises(me.KeyConflict):
        me.dumps(me.Document("X", titles=titles))


def test_dumps_writes_keys_the_way_the_reader_reads_them() -> None:
    kid = "0a" + "01" * 15
    entry = _entry(**{kid.upper(): "A1" * 16, "0a-" + "01" * 15: "a1" * 16, "0" * 32: "b2" * 16})
    raw = json.loads(me.dumps(me.Document("X", titles=[entry])))
    assert raw["titles"][0]["keys"] == {kid: "a1" * 16}
    assert me.loads(me.dumps(me.Document("X", titles=[entry]))).titles[0].keys == {kid: "a1" * 16}


def _two_titles(refused: dict) -> str:
    usable = {"id": "1", "kind": "movie", "title": "M", "manifests": [{"url": "u"}], "keys": {KID: "a1" * 16}}
    return json.dumps({"kind": "mediaexport", "version": 1, "service": {"tag": "X"}, "titles": [usable, refused]})


# capability tokens with no field of their name, and an odd header: the reader checks only the keys
REFUSED = {
    "id": "2",
    "kind": "movie",
    "crit": ["segments", "hls-aes"],
    "tracks": [{"type": "video", "source": "segments", "segments": [{"url": "https://a/1.ts"}]}],
    "keys": {"02" * 16: "b2" * 16},
    "manifests": [{"url": "v", "headers": {"Cookie": "c"}}],
}


def test_only_the_title_a_crit_names_is_refused() -> None:
    doc = me.loads(_two_titles(REFUSED))
    assert [e.id for e in doc.titles] == ["1"]
    assert [r.raw for r in doc.refused] == [REFUSED]
    assert "segments" in doc.refused[0].reason and "hls-aes" in doc.refused[0].reason
    assert doc.key_pool() == {KID: "a1" * 16, "02" * 16: "b2" * 16}


def test_a_refused_title_is_written_back_unchanged() -> None:
    doc = me.loads(_two_titles(REFUSED))
    doc.titles[0].title = "Renamed"
    raw = json.loads(me.dumps(doc))
    assert raw["titles"][0]["title"] == "Renamed" and raw["titles"][1] == REFUSED
    assert me.loads(me.dumps(doc)).refused[0].raw == REFUSED


def test_a_caller_that_understands_every_token_gets_the_title() -> None:
    doc = me.loads(_two_titles(REFUSED), understood=frozenset({"segments", "hls-aes"}))
    assert [e.id for e in doc.titles] == ["1", "2"] and doc.refused == []
    assert me.loads(_two_titles(REFUSED), understood={"segments"}).refused[0].raw == REFUSED


def test_a_file_whose_every_title_is_refused_still_reads() -> None:
    doc = me.loads(_one_title(crit=["segments"], segments=[]))
    assert doc.titles == [] and len(doc.refused) == 1


def test_read_takes_the_callers_tokens(tmp_path: Path) -> None:
    path = tmp_path / "e.json"
    path.write_text(_one_title(crit=["segments"], segments=[]), encoding="utf-8")
    assert me.read(path).titles == []
    assert len(me.read(path, understood={"segments"}).titles) == 1


def test_add_replaces_a_refused_title_with_the_same_id() -> None:
    doc = me.loads(_two_titles(REFUSED))
    doc.add(_entry("2"))
    assert [e.id for e in doc.titles] == ["1", "2"] and doc.refused == []


def test_a_crit_token_is_a_capability_not_a_field() -> None:
    text = _one_title(crit=["hls-aes"])
    assert me.loads(text).refused[0].raw["crit"] == ["hls-aes"]
    assert [e.id for e in me.loads(text, understood={"hls-aes"}).titles] == ["1"]


@pytest.mark.parametrize("kid", [KID, KID.upper(), "0101-" + KID[4:]])
def test_a_refused_title_that_disagrees_on_a_kid_rejects_the_file(kid: str) -> None:
    with pytest.raises(me.KeyConflict):
        me.loads(_two_titles(REFUSED | {"keys": {kid: "b2" * 16}}))


def test_a_malformed_key_in_a_refused_title_rejects_the_file() -> None:
    with pytest.raises(me.ExportError, match="not 32 hex digits"):
        me.loads(_two_titles(REFUSED | {"keys": {"zz": "b2" * 16}}))


def test_a_writer_cannot_add_a_kid_that_conflicts_with_a_refused_title() -> None:
    doc = me.loads(_two_titles(REFUSED))
    doc.titles[0].add_key("02" * 16, "c3" * 16)
    with pytest.raises(me.KeyConflict):
        doc.key_pool()
    with pytest.raises(me.KeyConflict):
        me.dumps(doc)


VIDEO_KID, AUDIO_KID = "0a" * 16, "0b" * 16
TWO_TRACK_KEYS = {VIDEO_KID: "a1" * 16, AUDIO_KID: "b2" * 16}


def test_track_kids_are_normalised_and_collapse() -> None:
    kids = [VIDEO_KID.upper(), "0a0a0a0a-" + VIDEO_KID[8:], "0" * 32, "00000000-0000-0000-0000-000000000000"]
    entry = me.loads(_one_title(tracks=[{"id": "v", "kids": kids}])).titles[0]
    assert entry.tracks == [{"id": "v", "kids": [VIDEO_KID]}]


@pytest.mark.parametrize("kids", [["zz"], [VIDEO_KID[:30]], [None], [""], [5]])
def test_a_malformed_track_kid_rejects_the_file(kids: list) -> None:
    with pytest.raises(me.ExportError, match="not 32 hex digits"):
        me.loads(_one_title(tracks=[{"id": "v", "kids": kids}]))


@pytest.mark.parametrize("kids", [VIDEO_KID, {VIDEO_KID: "a1" * 16}, 5, True])
def test_track_kids_that_are_not_a_list_reject_the_file(kids: object) -> None:
    with pytest.raises(me.ExportError, match="kids is not a list"):
        me.loads(_one_title(tracks=[{"id": "v", "kids": kids}]))


@pytest.mark.parametrize("kids", [None, [], ["0" * 32]])
def test_track_kids_that_come_out_empty_are_unknown(kids: object) -> None:
    entry = me.loads(_one_title(keys=TWO_TRACK_KEYS, tracks=[{"id": "v", "kids": kids}])).titles[0]
    assert entry.tracks == [{"id": "v"}]
    assert entry.keys_for("v") == TWO_TRACK_KEYS


def test_track_kids_round_trip_normalised() -> None:
    entry = _entry(**TWO_TRACK_KEYS)
    entry.tracks = [{"id": "v", "kids": [VIDEO_KID.upper(), VIDEO_KID]}, {"id": "a", "kids": [AUDIO_KID]}]
    raw = json.loads(me.dumps(me.Document("X", titles=[entry])))
    assert raw["titles"][0]["tracks"] == [{"id": "v", "kids": [VIDEO_KID]}, {"id": "a", "kids": [AUDIO_KID]}]
    assert entry.tracks[0]["kids"] == [VIDEO_KID.upper(), VIDEO_KID]
    again = me.loads(me.dumps(me.Document("X", titles=[entry]))).titles[0]
    assert again.keys_for("a") == {AUDIO_KID: "b2" * 16}


def test_dumps_rejects_a_track_kid_the_reader_would_refuse() -> None:
    entry = _entry()
    entry.tracks = [{"id": "v", "kids": ["zz"]}]
    with pytest.raises(me.ExportError, match="not 32 hex digits"):
        me.dumps(me.Document("X", titles=[entry]))


def test_keys_for_takes_the_tracks_kids_else_the_whole_title() -> None:
    entry = _entry(**TWO_TRACK_KEYS)
    entry.tracks = [{"id": "v", "kids": [VIDEO_KID]}, {"id": "s", "type": "subtitle"}]
    assert entry.keys_for("v") == {VIDEO_KID: "a1" * 16}
    assert entry.keys_for("s") == TWO_TRACK_KEYS
    assert entry.keys_for("nope") == TWO_TRACK_KEYS


def test_a_track_kid_with_no_key_in_the_file_is_allowed() -> None:
    entry = me.loads(_one_title(keys=TWO_TRACK_KEYS, tracks=[{"id": "v", "kids": [VIDEO_KID, "0c" * 16]}])).titles[0]
    assert entry.tracks[0]["kids"] == [VIDEO_KID, "0c" * 16]
    assert entry.keys_for("v") == {VIDEO_KID: "a1" * 16}


def test_a_refused_titles_track_kids_are_not_read() -> None:
    refused = REFUSED | {"tracks": [{"id": "v", "kids": ["zz"]}]}
    assert me.loads(_two_titles(refused)).refused[0].raw == refused


def test_unshackle_v2_direct_url_tracks_keep_their_kids() -> None:
    title = {
        "meta": {"type": "movie", "name": "M"},
        "tracks": {
            "v": {"id": "v", "url": "https://a/v.mp4", "keys": {VIDEO_KID.upper(): "a1" * 16}},
            "a": {"id": "a", "url": "https://a/a.mp4", "drm": [{"system": "widevine", "kids": [AUDIO_KID]}]},
            "s": {"id": "s", "url": "https://a/s.vtt", "keys": {"": None}},
        },
    }
    entry = me.loads(_v2(title)).titles[0]
    assert entry.tracks == [
        {"id": "v", "url": "https://a/v.mp4", "kids": [VIDEO_KID]},
        {"id": "a", "url": "https://a/a.mp4", "kids": [AUDIO_KID]},
        {"id": "s", "url": "https://a/s.vtt"},
    ]
