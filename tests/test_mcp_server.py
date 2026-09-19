"""MCP server: tools and resource through the SDK's in-process client, no network."""

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import Client

from paperfill.mcp_server import create_server


def _call(server, name, args=None):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(name, args or {})

    return asyncio.run(go())


def test_tools_are_listed_with_annotations_and_schemas(tmp_path):
    server = create_server(data_dir=tmp_path, source_factory=lambda: None)

    async def go():
        async with Client(server) as c:
            return await c.list_tools()

    tools = {t.name: t for t in asyncio.run(go()).tools}
    assert {
        "discover_markets",
        "run_paper",
        "read_report",
        "verify_journal",
        "raise_kill_switch",
    } <= set(tools)
    assert tools["discover_markets"].annotations.read_only_hint is True
    assert tools["raise_kill_switch"].annotations.destructive_hint is True
    assert "condition_id" in tools["run_paper"].input_schema["properties"]
    assert tools["run_paper"].output_schema is not None


def test_discover_and_run_replay_and_read_report(tmp_path, gamma_market_raw):
    from tests.test_cli import _fake_market_source

    raw = dict(gamma_market_raw)
    raw["closed"], raw["umaResolutionStatus"], raw["outcomePrices"] = True, "resolved", '["1", "0"]'
    toks = json.loads(raw["clobTokenIds"])
    rows = [
        {"proxy_wallet": "0x" + "ab" * 20, "side": "BUY", "token_id": toks[0],
         "condition_id": raw["conditionId"], "size": "50", "price": "0.60",
         "timestamp": 1000, "transaction_hash": "0x" + "cd" * 32}
    ]  # fmt: skip
    server = create_server(data_dir=tmp_path, source_factory=_fake_market_source(raw, rows))

    hist = _call(server, "fetch_history", {"condition_id": raw["conditionId"]})
    assert not hist.is_error and hist.structured_content["trades"] == 1
    assert (tmp_path / "history").exists()

    run = _call(
        server,
        "run_paper",
        {"condition_id": raw["conditionId"], "strategy": "two-sided", "capital": "50"},
    )
    assert not run.is_error, run.content
    body = run.structured_content
    assert body["mode"] == "replay" and body["settled"] is True and body["events"] == 1
    assert "# paperfill run report" in body["report_markdown"]

    rep = _call(server, "read_report", {"run_dir": body["run_dir"]})
    assert rep.structured_content["fills"] == body["fills"]
    ver = _call(server, "verify_journal", {"path": str(Path(body["run_dir"]) / "journal.jsonl")})
    assert ver.structured_content["ok"] is True
    runs = _call(server, "list_runs")
    assert runs.structured_content["names"] == [Path(body["run_dir"]).name]

    async def read():
        async with Client(server) as c:
            name = Path(body["run_dir"]).name
            return await c.read_resource(f"paperfill://runs/{name}/report.md")

    res = asyncio.run(read())
    assert "# paperfill run report" in res.contents[0].text

    kill = _call(server, "raise_kill_switch", {"run_dir": body["run_dir"]})
    assert (Path(body["run_dir"]) / "KILL").exists() and "raised" in kill.content[0].text


def test_tool_errors_are_reported_not_raised(tmp_path):
    server = create_server(data_dir=tmp_path, source_factory=lambda: None)
    r = _call(server, "read_report", {"run_dir": str(tmp_path / "nope")})
    assert r.is_error and "no journal" in r.content[0].text
    r = _call(server, "run_batch", {"strategy": "two-sided"})
    assert r.is_error and "no recordings" in r.content[0].text
    r = _call(server, "verify_journal", {"path": str(tmp_path / "x.jsonl")})
    assert r.is_error
