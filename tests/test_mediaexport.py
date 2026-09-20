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
                "headers": {"H": "v"},
            }
        ],
    }
    e = me.loads(json.dumps(raw)).titles[0]
    assert e.primary.url == "https://a/1.mpd"
    assert [(m.url, m.role, m.headers) for m in e.manifests[1:]] == [("https://a/2.mpd", "extra", {"H": "v"})]
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
    assert doc.titles[0].extensions == {"x-foo": {"a": 1}}
    assert json.loads(me.dumps(doc))["titles"][0]["x-foo"] == {"a": 1}


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
