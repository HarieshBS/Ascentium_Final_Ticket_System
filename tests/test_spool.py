import json

import pytest

import app


@pytest.fixture
def stored(tmp_path, monkeypatch):
    """Spool in a temp dir, storage replaced by a list."""
    monkeypatch.setattr(app, "SPOOL", tmp_path / "spool.jsonl")
    monkeypatch.setattr(app, "BAD_SPOOL", tmp_path / "spool.bad")
    calls = []
    monkeypatch.setattr(app, "store_message", lambda mid, text: calls.append(mid))
    monkeypatch.setattr(app, "save_result", lambda mid, fields, n: calls.append(mid))
    return calls


def line(mid, text="hello"):
    return json.dumps({"kind": "msg", "message_id": mid, "text": text}) + "\n"


def test_truncated_line_is_quarantined_and_replay_continues(stored):
    torn = line("m3", "my order ORD-12345 never came")[:-20]  # crash mid-write: no closing quote, brace or newline
    app.SPOOL.write_text(line("m1") + torn + "\n" + line("m2"), encoding="utf-8")

    app.replay_spool()

    assert stored == ["m1", "m2"]
    assert app.SPOOL.read_text() == ""
    assert app.BAD_SPOOL.read_text() == torn + "\n"
    assert app.quarantined() == [{"message_id": "m3", "preview": torn[:120]}]


def test_new_record_is_not_glued_onto_a_torn_tail(stored):
    app.SPOOL.write_text(line("m1")[:15], encoding="utf-8")  # torn before the id was written
    app.load_spooled_results()  # startup must not crash on it
    app.spool({"kind": "msg", "message_id": "m2", "text": "hi"})

    app.replay_spool()

    assert stored == ["m2"]
    assert app.quarantined() == [{"message_id": None, "preview": line("m1")[:15]}]


@pytest.mark.parametrize("garbage", ["\x00\x00\x00", "[1, 2]", '{"kind": "msg"}', "�{"])
def test_wrong_shaped_lines_are_quarantined_not_replayed(stored, garbage):
    app.SPOOL.write_text(garbage + "\n" + line("ok"), encoding="utf-8")
    app.replay_spool()
    assert stored == ["ok"] and len(app.quarantined()) == 1


def test_storage_failure_still_keeps_the_rest_in_the_spool(stored, monkeypatch):
    def down(mid, text):
        raise app.psycopg.OperationalError("down")
    monkeypatch.setattr(app, "store_message", down)
    app.SPOOL.write_text("{torn\n" + line("m1"), encoding="utf-8")

    with pytest.raises(app.psycopg.OperationalError):
        app.replay_spool()

    assert app.SPOOL.read_text() == line("m1")  # good line kept for the next replay
    assert app.BAD_SPOOL.read_text() == "{torn\n"


def test_clear_renames_instead_of_deleting(stored, monkeypatch):
    monkeypatch.setattr(app, "OPS_TOKEN", "")
    app.BAD_SPOOL.write_text("{torn\n", encoding="utf-8")
    assert app.clear_quarantine(type("R", (), {"headers": {}})())["cleared"] == 1
    assert not app.BAD_SPOOL.exists()
    assert [p.read_text() for p in app.BAD_SPOOL.parent.glob("spool.bad.*.reviewed")] == ["{torn\n"]
