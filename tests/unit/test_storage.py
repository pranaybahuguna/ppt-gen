"""The store: decks and artefacts as files under local/store, referenced as local://deckops/<key>."""
from __future__ import annotations

import pytest

from deck_agent.errors import Boundary
from deck_agent.infra import cleanup
from deck_agent.infra.storage import FileStore, Ref, canonical, is_ref, sha256_file


@pytest.fixture
def fs(tmp_path):
    st = FileStore(tmp_path / "store", "deckops")
    st.ensure_bucket()
    return st


def test_a_deck_round_trips_byte_for_byte(fs, sample_deck, tmp_path):
    uri = fs.put(sample_deck, "pilot/v001.pptx")
    assert uri == "local://deckops/pilot/v001.pptx"
    assert (fs.root / "deckops" / "pilot" / "v001.pptx").is_file()
    back = fs.get(uri, tmp_path / "back.pptx")
    assert sha256_file(back) == sha256_file(sample_deck)


def test_json_keeps_unicode(fs):
    payload = {"title": "Résultats du troisième trimestre", "n": [1, 2]}
    assert fs.get_json(fs.put_json(payload, "a/b.json")) == payload


def test_a_reference_from_the_s3_days_names_the_same_file(fs):
    """Decks made when this was an S3 bucket keep s3://deckops/... in their saved state; the
    files were copied across, and the reference still opens them."""
    fs.put_bytes(b"deck", "voltline/v017.pptx")
    old = "s3://deckops/voltline/v017.pptx"
    assert fs.exists(old) and fs.get_bytes(old) == b"deck"
    assert canonical(old) == "local://deckops/voltline/v017.pptx"
    assert is_ref(old) and is_ref("local://deckops/x") and not is_ref("/tmp/x.pptx")


@pytest.mark.parametrize("uri", ["local://deckops/../outside.txt", "local://deckops/a/../../x",
                                 "file:///etc/passwd", "local://deckops/"])
def test_a_reference_cannot_reach_outside_the_store(fs, uri):
    with pytest.raises(Boundary):
        fs.get_bytes(uri)
    assert not fs.exists(uri)


def test_listing_and_deleting_leave_no_empty_folders(fs):
    for k in ("d/r1/a.json", "d/r1/previews/s1.png", "d/v001.pptx", "e/v001.pptx"):
        fs.put_bytes(b"x", k)
    assert [Ref.parse(u).key for u in fs.list("d/")] == ["d/r1/a.json", "d/r1/previews/s1.png", "d/v001.pptx"]
    objs = fs.objects("d/")
    assert all(o.size == 1 and o.modified > 0 for o in objs)
    assert fs.delete([o.uri for o in objs]) == 3
    assert not (fs.root / "deckops" / "d").exists(), "a deleted deck leaves no empty folders"
    assert fs.list("") == ["local://deckops/e/v001.pptx"]


def test_a_missing_object_is_a_clear_failure(fs, tmp_path):
    with pytest.raises(Boundary, match="no such object"):
        fs.get_bytes("local://deckops/nope.json")
    with pytest.raises(Boundary, match="no such object"):
        fs.get("local://deckops/nope.json", tmp_path / "x")


def test_a_write_is_all_or_nothing(fs):
    """Writes go to a temporary file and are renamed into place; no temporary file is left or
    listed."""
    fs.put_bytes(b"first", "k.json")
    fs.put_bytes(b"second", "k.json")
    assert fs.get_bytes("local://deckops/k.json") == b"second"
    assert [p.name for p in (fs.root / "deckops").iterdir()] == ["k.json"]


def test_cleanup_counts_an_old_s3_reference_as_the_file_it_names():
    """Tidying keeps every file a session's state refers to; a state saved in the S3 days says
    s3://... and must still protect the file listed as local://..."""
    state = {"deck_versions": ["s3://deckops/v/v001.pptx", "local://deckops/v/v002.pptx"],
             "nested": [{"preview": "s3://other/x.png"}]}
    assert cleanup.referenced(state, "deckops") == {"local://deckops/v/v001.pptx",
                                                    "local://deckops/v/v002.pptx"}


def test_a_file_link_points_at_the_file(fs):
    uri = fs.put_bytes(b"x", "k.json")
    assert fs.url(uri) == (fs.root / "deckops" / "k.json").as_uri()
