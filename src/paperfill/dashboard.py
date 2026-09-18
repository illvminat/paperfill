"""Dashboard for one run directory: report, positions, journal tail, kill switch.

FastAPI (SOURCES.md -> python/fastapi-*.md) serving one HTML page and two JSON
endpoints. It reads the journal file on every request, so it works both during a
run (tail grows) and after it. The only action it can take is raising the kill
switch file, which is exactly what the risk engine watches.

States the page must show (docs/zadanie.md, section 5): empty (no journal yet),
loading, error (unreadable journal), partial (journal but no marks), offline (last
entry is a gap).
"""

from __future__ import annotations

import html
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse

from paperfill.journal import read_entries, verify
from paperfill.report import compute

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>paperfill · {title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {{ color-scheme: light dark; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
body {{ margin: 0 auto; max-width: 960px; padding: 16px; line-height: 1.4; }}
h1 {{ font-size: 1.25rem; }} h2 {{ font-size: 1rem; margin-top: 1.5rem; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
td, th {{ border-bottom: 1px solid #8884; padding: 4px 6px; text-align: left; vertical-align: top; }}
.state {{ display: inline-block; padding: 2px 8px; border-radius: 4px; background: #8883; }}
.halt {{ background: #c0392b; color: white; }} .ok {{ background: #27ae60; color: white; }}
button {{ font: inherit; padding: 8px 14px; border-radius: 6px; border: 1px solid #c0392b; background: #c0392b; color: white; cursor: pointer; }}
pre {{ white-space: pre-wrap; word-break: break-all; font-size: 0.8rem; }}
</style></head><body>
<h1>paperfill · {title}</h1>
<p><span class="state {state_class}">{state}</span> {subtitle}</p>
<form method="post" action="/kill"><input type="hidden" name="token" value="{token}"><button type="submit" {kill_disabled}>Raise kill switch</button>
<span> {kill_note}</span></form>
<h2>Summary</h2>
<table>{summary_rows}</table>
<h2>Positions (from fills)</h2>
<table><tr><th>Token</th><th>Fills</th><th>Shares</th><th>Cost</th><th>Payout</th><th>Settled P&amp;L</th></tr>{position_rows}</table>
<h2>Journal tail (last {tail} of {total})</h2>
<table><tr><th>#</th><th>ts</th><th>kind</th><th>data</th></tr>{journal_rows}</table>
<p><a href="/api/report.json">report.json</a> · <a href="/api/journal?tail=200">journal tail</a></p>
</body></html>"""


def _state(run_dir: Path) -> tuple[str, str, str, list, str | None]:
    """Return (state label, css class, subtitle, entries, error)."""
    path = run_dir / "journal.jsonl"
    if not path.exists():
        return "empty", "", "no journal yet in this directory", [], None
    try:
        entries = read_entries(path)
    except Exception as error:
        return (
            "error",
            "halt",
            f"journal unreadable: {type(error).__name__}: {error}",
            [],
            str(error),
        )
    v = verify(entries)
    if not v.ok:
        return (
            "error",
            "halt",
            f"journal chain broken at seq {v.first_bad_seq}: {v.reason}",
            entries,
            v.reason,
        )
    if not entries:
        return "empty", "", "journal is empty", entries, None
    last = entries[-1]
    if last.kind == "run_end":
        halted = last.data.get("halted")
        return (
            ("halted", "halt", f"run ended: {halted}", entries, None)
            if halted
            else ("finished", "ok", "run ended normally", entries, None)
        )
    if last.kind == "gap":
        return "offline", "halt", "market data gap; waiting for reconnection", entries, None
    if not any(e.kind == "mark" for e in entries):
        return "partial", "", "running, no marks yet", entries, None
    return "running", "ok", f"last entry {last.kind} at {last.ts}", entries, None


def create_app(run_dir: Path, *, kill_token: str | None = None) -> FastAPI:
    """`kill_token` guards POST /kill against cross-site form posts; random by default."""
    app = FastAPI(title="paperfill dashboard", docs_url=None, redoc_url=None)
    kill_file = run_dir / "KILL"
    token = kill_token or secrets.token_urlsafe(16)

    @app.get("/", response_class=HTMLResponse)
    def index(tail: int = 50) -> str:
        state, css, subtitle, entries, _ = _state(run_dir)
        metrics = compute(entries) if entries else None
        summary = ""
        positions = ""
        if metrics:
            rows = [
                ("Market", metrics.question),
                ("Strategy / mode", f"{metrics.strategy} / {metrics.mode}"),
                ("Events", metrics.events),
                ("Orders / fills", f"{metrics.orders_submitted} / {metrics.fills}"),
                ("Risk blocks", metrics.risk_blocks),
                ("Fees / rebates est.", f"{metrics.fees} / {metrics.rebates_estimated}"),
                ("Realized P&L after fees", metrics.realized_pnl),
                ("Max drawdown", f"{metrics.max_drawdown} ({metrics.max_drawdown_pct}%)"),
                ("Settled", "yes" if metrics.settled else "no"),
            ]
            summary = "".join(
                f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
                for k, v in rows
            )
            positions = (
                "".join(
                    "<tr>"
                    + "".join(
                        f"<td>{html.escape(str(x))}</td>"
                        for x in (
                            t[:10] + "…",
                            s["fills"],
                            s["shares"],
                            s["cost"],
                            s["payout"] or "—",
                            s["settled_pnl"] or "—",
                        )
                    )
                    + "</tr>"
                    for t, s in metrics.per_token.items()
                )
                or "<tr><td colspan=6>no fills</td></tr>"
            )
        tail_entries = entries[-tail:] if entries else []
        journal_rows = (
            "".join(
                f"<tr><td>{e.seq}</td><td>{html.escape(e.ts)}</td><td>{html.escape(e.kind)}</td>"
                f"<td><pre>{html.escape(str(e.data))}</pre></td></tr>"
                for e in tail_entries
            )
            or "<tr><td colspan=4>nothing recorded</td></tr>"
        )
        killed = kill_file.exists()
        return PAGE.format(
            token=token,
            title=html.escape(run_dir.name),
            state=state,
            state_class=css,
            subtitle=html.escape(subtitle),
            kill_disabled="disabled" if killed else "",
            kill_note="kill switch is raised"
            if killed
            else "creates the KILL file; the run halts at its next event",
            summary_rows=summary or "<tr><td>no data</td></tr>",
            position_rows=positions or "<tr><td colspan=6>no data</td></tr>",
            tail=len(tail_entries),
            total=len(entries),
            journal_rows=journal_rows,
        )

    @app.post("/kill")
    def kill(token_field: Annotated[str, Form(alias="token")] = "") -> HTMLResponse:
        if not secrets.compare_digest(token_field, token):
            return HTMLResponse("forbidden: bad or missing token", status_code=403)
        kill_file.parent.mkdir(parents=True, exist_ok=True)
        kill_file.write_text(f"kill requested {datetime.now(UTC).isoformat()}\n")
        return HTMLResponse(
            '<meta http-equiv="refresh" content="0; url=/">',
            status_code=303,
            headers={"Location": "/"},
        )

    @app.get("/api/report.json")
    def report_json() -> JSONResponse:
        state, _, subtitle, entries, error = _state(run_dir)
        if error:
            return JSONResponse({"state": state, "error": subtitle}, status_code=409)
        return JSONResponse(
            {"state": state, "report": compute(entries).to_json() if entries else None}
        )

    @app.get("/api/journal")
    def journal_tail(tail: int = 50) -> JSONResponse:
        state, _, _, entries, _ = _state(run_dir)
        return JSONResponse(
            {
                "state": state,
                "total": len(entries),
                "entries": [e.to_json() for e in entries[-tail:]],
            }
        )

    return app


def serve(run_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    uvicorn.run(create_app(run_dir), host=host, port=port, log_level="warning")
