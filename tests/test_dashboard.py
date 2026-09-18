"""Dashboard states, JSON endpoints and the kill switch, through FastAPI's test client."""

from datetime import UTC, datetime
from decimal import Decimal as D

from fastapi.testclient import TestClient

from paperfill.dashboard import create_app
from paperfill.journal import Journal


def _journal(path, kinds):
    j = Journal.open(path, clock=lambda: datetime(2026, 9, 19, tzinfo=UTC))
    for kind, data in kinds:
        j.record(kind, **data)
    j.close()


def test_empty_state(tmp_path):
    client = TestClient(create_app(tmp_path))
    r = client.get("/")
    assert r.status_code == 200 and ">empty<" in r.text and "no journal yet" in r.text
    assert client.get("/api/report.json").json() == {"state": "empty", "report": None}


def test_running_partial_offline_finished_and_halted(tmp_path):
    start = (
        "run_start",
        {"mode": "replay", "market": "0xm", "question": "Q?", "strategy": "s", "capital": D("100")},
    )
    _journal(tmp_path / "journal.jsonl", [start])
    client = TestClient(create_app(tmp_path))
    assert ">partial<" in client.get("/").text
    _journal(
        tmp_path / "journal.jsonl",
        [("mark", {"equity": D("100"), "cash": D("100"), "exposure": D("0"), "marks": {}})],
    )
    assert ">running<" in client.get("/").text
    _journal(tmp_path / "journal.jsonl", [("gap", {"reason": "socket closed"})])
    assert ">offline<" in client.get("/").text
    _journal(
        tmp_path / "journal.jsonl",
        [
            (
                "run_end",
                {
                    "events": 3,
                    "fills": 0,
                    "cash": D("100"),
                    "realized_pnl": D("0"),
                    "fees": D("0"),
                    "rebates_estimated": D("0"),
                    "halted": None,
                    "settled": False,
                },
            )
        ],
    )
    page = client.get("/").text
    assert ">finished<" in page and "Realized P&amp;L after fees" in page
    body = client.get("/api/report.json").json()
    assert body["state"] == "finished" and body["report"]["events"] == 3
    tail = client.get("/api/journal?tail=2").json()
    assert tail["total"] == 4 and [e["kind"] for e in tail["entries"]] == ["gap", "run_end"]


def test_halted_state_and_broken_chain(tmp_path):
    start = (
        "run_start",
        {"mode": "replay", "market": "0xm", "question": "Q?", "strategy": "s", "capital": D("100")},
    )
    end = (
        "run_end",
        {
            "events": 1,
            "fills": 0,
            "cash": D("90"),
            "realized_pnl": D("-10"),
            "fees": D("0"),
            "rebates_estimated": D("0"),
            "halted": "kill switch file present",
            "settled": False,
        },
    )
    _journal(tmp_path / "journal.jsonl", [start, end])
    client = TestClient(create_app(tmp_path))
    assert ">halted<" in client.get("/").text
    lines = (tmp_path / "journal.jsonl").read_text().splitlines()
    (tmp_path / "journal.jsonl").write_text(
        lines[0].replace('"capital":"100"', '"capital":"1"') + "\n" + lines[1] + "\n"
    )
    assert ">error<" in client.get("/").text
    assert client.get("/api/report.json").status_code == 409


def test_kill_button_needs_the_page_token(tmp_path):
    client = TestClient(create_app(tmp_path, kill_token="t0k"))
    assert "disabled" not in client.get("/").text.split("Raise kill switch")[0][-120:]
    assert client.post("/kill", follow_redirects=False).status_code == 403  # cross-site POST
    assert client.post("/kill", data={"token": "wrong"}, follow_redirects=False).status_code == 403
    assert not (tmp_path / "KILL").exists()
    r = client.post("/kill", data={"token": "t0k"}, follow_redirects=False)
    assert r.status_code == 303 and (tmp_path / "KILL").exists()
    assert "kill switch is raised" in client.get("/").text
