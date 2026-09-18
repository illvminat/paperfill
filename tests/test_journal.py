from datetime import UTC, datetime
from decimal import Decimal

import pytest

from paperfill.journal import GENESIS, Journal, read_entries, verify, verify_file


def _clock():
    t = [datetime(2026, 9, 19, tzinfo=UTC)]

    def tick():
        return t[0]

    return tick


def test_chain_links_and_verifies(tmp_path):
    path = tmp_path / "journal.jsonl"
    j = Journal.open(path, clock=_clock())
    a = j.record("run_start", capital=Decimal("100"))
    b = j.record(
        "order_submitted",
        order_id="o1",
        price=Decimal("0.5"),
        when=datetime(2026, 9, 19, tzinfo=UTC),
    )
    j.close()
    assert a.prev == GENESIS and b.prev == a.hash and j.head == b.hash
    assert b.data == {"order_id": "o1", "price": "0.5", "when": "2026-09-19T00:00:00+00:00"}
    v = verify_file(path)
    assert v.ok and v.entries == 2 and v.first_bad_seq is None


def test_tampering_any_line_breaks_verification(tmp_path):
    path = tmp_path / "journal.jsonl"
    j = Journal.open(path, clock=_clock())
    for i in range(4):
        j.record("fill", order_id=f"o{i}", size=Decimal("5"))
    j.close()
    lines = path.read_text().splitlines()
    lines[1] = lines[1].replace('"size":"5"', '"size":"50"')
    path.write_text("\n".join(lines) + "\n")
    v = verify_file(path)
    assert not v.ok and v.first_bad_seq == 1 and "content" in v.reason
    lines = path.read_text().splitlines()
    del lines[2]
    path.write_text("\n".join(lines) + "\n")
    v = verify(read_entries(path))
    assert not v.ok and v.first_bad_seq == 1


def test_deleting_the_last_line_is_undetectable_by_chain_alone(tmp_path):
    # Documented limitation: truncation at the tail leaves a valid shorter chain.
    # The run_end entry (recorded by the runner) is what makes truncation visible.
    path = tmp_path / "journal.jsonl"
    j = Journal.open(path, clock=_clock())
    j.record("a")
    j.record("run_end")
    j.close()
    lines = path.read_text().splitlines()
    path.write_text(lines[0] + "\n")
    v = verify_file(path)
    assert v.ok and v.entries == 1
    assert not any(e.kind == "run_end" for e in read_entries(path))


def test_unserialisable_data_is_an_error_not_silence():
    j = Journal(clock=_clock())
    with pytest.raises(TypeError):
        j.record("x", obj=object())


def test_reopening_continues_the_chain_and_refuses_a_broken_file(tmp_path):
    path = tmp_path / "journal.jsonl"
    j = Journal.open(path, clock=_clock())
    a = j.record("a")
    j.close()
    j2 = Journal.open(path, clock=_clock())
    b = j2.record("b")
    j2.close()
    assert b.prev == a.hash and b.seq == 1 and verify_file(path).ok
    lines = path.read_text().splitlines()
    path.write_text(lines[0].replace('"kind":"a"', '"kind":"z"') + "\n" + lines[1] + "\n")
    with pytest.raises(ValueError, match="chain broken"):
        Journal.open(path, clock=_clock())
