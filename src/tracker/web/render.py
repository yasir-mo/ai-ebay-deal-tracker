"""HTML rendering.

No template engine, so every value that reaches the page goes through esc().
Listing titles and seller names come from eBay and are therefore untrusted.
"""
from __future__ import annotations

import html
from datetime import datetime

from ..models import Verdict

CSS = """
:root {
  --bg: #10151c; --panel: #171e27; --line: #26303d; --text: #e6edf5;
  --muted: #8b9bb0; --accent: #4a9eff;
  --priority: #ff4d4d; --buy: #ff8c42; --good: #35c46b; --watch: #ffd93d;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header {
  background: var(--panel); border-bottom: 1px solid var(--line);
  padding: 0 20px; display: flex; align-items: center; gap: 24px;
  position: sticky; top: 0; z-index: 10;
}
header h1 { font-size: 16px; margin: 0; padding: 16px 0; font-weight: 600; }
nav { display: flex; gap: 4px; }
nav a {
  padding: 16px 14px; color: var(--muted); border-bottom: 2px solid transparent;
}
nav a.active { color: var(--text); border-bottom-color: var(--accent); }
main { max-width: 1100px; margin: 0 auto; padding: 24px 20px 64px; }
h2 { font-size: 20px; margin: 0 0 4px; }
.sub { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 16px; margin-bottom: 12px;
}
.deal { display: flex; gap: 16px; align-items: flex-start; }
.deal .body { flex: 1; min-width: 0; }
.deal h3 { margin: 0 0 6px; font-size: 15px; font-weight: 600; }
.badge {
  display: inline-block; padding: 3px 9px; border-radius: 4px;
  font-size: 11px; font-weight: 700; letter-spacing: .04em; color: #0d1117;
  white-space: nowrap;
}
.badge.PRIORITY { background: var(--priority); color: #fff; }
.badge.BUY_NOW { background: var(--buy); }
.badge.GOOD_DEAL { background: var(--good); }
.badge.WATCH { background: var(--watch); }
.badge.SKIP { background: var(--muted); }
.meta { color: var(--muted); font-size: 13px; display: flex; flex-wrap: wrap; gap: 14px; }
.meta b { color: var(--text); font-weight: 600; }
.warn { color: var(--watch); font-size: 13px; margin-top: 8px; }
.rationale { font-size: 13px; color: var(--muted); font-style: italic; margin-top: 8px; }
.actions { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
button, .btn {
  background: #223044; border: 1px solid var(--line); color: var(--text);
  padding: 7px 13px; border-radius: 6px; font-size: 13px; cursor: pointer;
  font-family: inherit;
}
button:hover, .btn:hover { background: #2b3c53; text-decoration: none; }
button.primary { background: var(--accent); border-color: var(--accent); color: #04121f; font-weight: 600; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
form.inline { display: inline; }
input, select {
  background: #0e141b; border: 1px solid var(--line); color: var(--text);
  padding: 7px 9px; border-radius: 6px; font-size: 14px; font-family: inherit;
}
label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 4px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; }
.empty { color: var(--muted); padding: 40px 0; text-align: center; }
.flash {
  background: #1d3a2a; border: 1px solid #2e7d51; padding: 10px 14px;
  border-radius: 6px; margin-bottom: 16px; font-size: 14px;
}
.chart { width: 100%; height: 130px; }
.cold { color: var(--watch); }
@media (max-width: 640px) {
  header { gap: 8px; padding: 0 12px; } nav a { padding: 14px 9px; }
  main { padding: 16px 12px 48px; } .meta { gap: 10px; }
}
"""

NAV = [
    ("/", "Deals"),
    ("/searches", "Searches"),
    ("/history", "History"),
    ("/settings", "Settings"),
]


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def money(pence: int | None, currency: str = "GBP") -> str:
    if pence is None:
        return "n/a"
    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, "")
    return f"{symbol}{pence / 100:,.2f}"


def time_left(end: datetime | None, now: datetime) -> str | None:
    if not end:
        return None
    seconds = int((end - now).total_seconds())
    if seconds <= 0:
        return "ended"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def page(title: str, active: str, body: str, flash: str | None = None) -> str:
    nav = "".join(
        f'<a href="{esc(href)}" class="{"active" if href == active else ""}">{esc(label)}</a>'
        for href, label in NAV
    )
    flash_html = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{CSS}</style>
</head><body>
<header><h1>Deal Tracker</h1><nav>{nav}</nav></header>
<main>{flash_html}{body}</main>
</body></html>"""


def _flags(reasons: list[str]) -> str:
    pretty = {
        "shipping_unknown": "postage not stated, total is a lower bound",
        "provisional_baseline": "baseline is your manual estimate, not observed data",
    }
    hits = [pretty[r] for r in reasons if r in pretty]
    return f'<div class="warn">{esc("; ".join(hits))}</div>' if hits else ""


def deal_card(row: dict, now: datetime) -> str:
    verdict = row["verdict"]
    left = time_left(row.get("end_time"), now)
    ends = f'<span>Ends in <b>{esc(left)}</b></span>' if left else ""
    bids = (
        f'<span>{esc(row["bid_count"])} bids</span>'
        if row.get("bid_count")
        else ""
    )
    discount = (
        f'<span><b>{row["discount_pct"]:.0%}</b> below market</span>'
        if row.get("discount_pct") is not None
        else ""
    )
    margin = (
        f'<span>Margin <b>{money(row["margin_pence"])}</b></span>'
        if row.get("margin_pence")
        else ""
    )
    shipping = (
        money(row["shipping_pence"])
        if row.get("shipping_pence") is not None
        else "not stated"
    )
    rationale = (
        f'<div class="rationale">{esc(row["rationale"])}</div>'
        if row.get("rationale")
        else ""
    )
    item = esc(row["item_id"])
    return f"""<div class="card deal">
  <span class="badge {esc(verdict)}">{esc(verdict.replace("_", " "))}</span>
  <div class="body">
    <h3><a href="{esc(row["item_url"])}" target="_blank" rel="noopener noreferrer">{esc(row["title"])}</a></h3>
    <div class="meta">
      <span>Total <b>{money(row["total_pence"])}</b></span>
      <span>Postage {esc(shipping)}</span>
      {discount}{margin}{ends}{bids}
      <span>{esc(row.get("profile_name") or row["profile_id"])}</span>
    </div>
    {_flags(row.get("reasons") or [])}
    {rationale}
    <div class="actions">
      <a class="btn" href="{esc(row["item_url"])}" target="_blank" rel="noopener noreferrer">Open on eBay</a>
      <form class="inline" method="post" action="/action">
        <input type="hidden" name="item_id" value="{item}">
        <input type="hidden" name="verdict" value="{esc(verdict)}">
        <button name="outcome" value="bought" class="primary">Bought</button>
        <button name="outcome" value="missed">Missed</button>
        <button name="outcome" value="bad">Not a deal</button>
      </form>
    </div>
  </div>
</div>"""


def deals_page(rows: list[dict], now: datetime, flash: str | None = None) -> str:
    if not rows:
        body = (
            "<h2>Deals</h2>"
            '<div class="sub">Alerts awaiting a decision.</div>'
            '<div class="empty">Nothing waiting. Alerts appear here as they are found.</div>'
        )
    else:
        cards = "".join(deal_card(r, now) for r in rows)
        body = (
            "<h2>Deals</h2>"
            f'<div class="sub">{len(rows)} awaiting a decision, most urgent first. '
            "Refreshes every 15 seconds.</div>" + cards
        )
    return page("Deals", "/", body, flash)


def searches_page(rows: list[dict], flash: str | None = None) -> str:
    body = ["<h2>Searches</h2>", '<div class="sub">What the tracker polls, and how each baseline is doing.</div>']

    if rows:
        body.append("<table><tr><th>Search</th><th>Query</th><th class='num'>Ceiling</th>"
                    "<th class='num'>Target</th><th>Baseline</th><th class='num'>Alerts 7d</th>"
                    "<th></th></tr>")
        for r in rows:
            baseline = (
                f'{money(r["median_pence"])} <span class="sub">n={r["sample_n"]}</span>'
                if r.get("median_pence")
                else '<span class="cold">still building</span>'
            )
            toggle = "Disable" if r["enabled"] else "Enable"
            body.append(
                f"""<tr>
                <td><b>{esc(r["name"])}</b>{"" if r["enabled"] else ' <span class="sub">(off)</span>'}</td>
                <td class="sub">{esc(r["query"])}</td>
                <td class="num">{money(r["ceiling_pence"])}</td>
                <td class="num">{money(r.get("target_pence"))}</td>
                <td>{baseline}</td>
                <td class="num">{r.get("alerts_7d", 0)}</td>
                <td>
                  <form class="inline" method="post" action="/searches/toggle">
                    <input type="hidden" name="id" value="{esc(r["id"])}">
                    <button>{toggle}</button>
                  </form>
                  <form class="inline" method="post" action="/searches/delete"
                        onsubmit="return confirm('Delete this search and stop tracking it?')">
                    <input type="hidden" name="id" value="{esc(r["id"])}">
                    <button>Delete</button>
                  </form>
                </td></tr>"""
            )
        body.append("</table>")
    else:
        body.append('<div class="empty">No searches yet. Add one below.</div>')

    body.append("""
    <div class="card"><h3>Add a search</h3>
    <form method="post" action="/searches/add">
      <div class="grid">
        <div><label>Id (short, no spaces)</label><input name="id" required pattern="[a-zA-Z0-9_-]+"></div>
        <div><label>Name</label><input name="name" required></div>
        <div><label>eBay query</label><input name="query" required></div>
        <div><label>Ceiling (pounds)</label><input name="ceiling" type="number" step="0.01" required></div>
        <div><label>Target (pounds, optional)</label><input name="target" type="number" step="0.01"></div>
        <div><label>Min seller feedback %</label><input name="min_feedback_pct" type="number" step="0.1" value="95"></div>
        <div><label>Min feedback count</label><input name="min_feedback_count" type="number" value="10"></div>
      </div>
      <div class="actions"><button class="primary">Add search</button></div>
    </form></div>""")
    return page("Searches", "/searches", "".join(body), flash)


def sparkline(points: list[int], width: int = 640, height: int = 130) -> str:
    """Inline SVG price chart. No chart library, no build step."""
    if len(points) < 2:
        return '<div class="sub">Not enough price history yet.</div>'
    lo, hi = min(points), max(points)
    span = max(1, hi - lo)
    step = width / (len(points) - 1)
    coords = " ".join(
        f"{i * step:.1f},{height - 10 - ((p - lo) / span) * (height - 24):.1f}"
        for i, p in enumerate(points)
    )
    return f"""<svg class="chart" viewBox="0 0 {width} {height}" preserveAspectRatio="none"
  role="img" aria-label="Observed price history">
  <polyline points="{coords}" fill="none" stroke="#4a9eff" stroke-width="2"/>
</svg>
<div class="meta"><span>Low <b>{money(lo)}</b></span><span>High <b>{money(hi)}</b></span>
<span>{len(points)} observations</span></div>"""


def history_page(
    profiles: list[dict], selected: str | None, points: list[int], rows: list[dict]
) -> str:
    options = "".join(
        f'<option value="{esc(p["id"])}"{" selected" if p["id"] == selected else ""}>{esc(p["name"])}</option>'
        for p in profiles
    )
    body = [
        "<h2>History</h2>",
        '<div class="sub">Observed prices and every decision recorded.</div>',
        f"""<form method="get" action="/history" class="card">
        <label>Search</label>
        <select name="profile" onchange="this.form.submit()">{options}</select>
        </form>""",
    ]
    if selected:
        body.append(f'<div class="card">{sparkline(points)}</div>')

    if rows:
        body.append("<table><tr><th>When</th><th>Verdict</th><th>Item</th>"
                    "<th class='num'>Total</th><th class='num'>Discount</th><th>Outcome</th></tr>")
        for r in rows:
            disc = f'{r["discount_pct"]:.0%}' if r.get("discount_pct") is not None else ""
            body.append(
                f"""<tr><td class="sub">{esc(r["decided_at"][:16].replace("T", " "))}</td>
                <td><span class="badge {esc(r["verdict"])}">{esc(r["verdict"].replace("_", " "))}</span></td>
                <td><a href="{esc(r.get("item_url") or "#")}" target="_blank" rel="noopener noreferrer">{esc((r.get("title") or r["item_id"])[:70])}</a></td>
                <td class="num">{money(r["total_pence"])}</td>
                <td class="num">{esc(disc)}</td>
                <td class="sub">{esc(r.get("outcome") or "")}</td></tr>"""
            )
        body.append("</table>")
    else:
        body.append('<div class="empty">No decisions recorded for this search yet.</div>')
    return page("History", "/history", "".join(body))


def settings_page(
    thresholds: dict, backtest: dict | None, status: dict, flash: str | None = None
) -> str:
    def field(name, label, value, step="0.01"):
        return (
            f'<div><label>{esc(label)}</label>'
            f'<input name="{esc(name)}" type="number" step="{step}" value="{esc(value)}"></div>'
        )

    backtest_html = ""
    if backtest:
        rows = "".join(
            f"<tr><td><span class='badge {esc(k)}'>{esc(k.replace('_',' '))}</span></td>"
            f"<td class='num'>{v['current']}</td><td class='num'>{v['candidate']}</td></tr>"
            for k, v in backtest["verdicts"].items()
        )
        backtest_html = f"""<div class="card">
        <h3>What these numbers would have done</h3>
        <div class="sub">Re-scored {backtest["listings"]} stored listings from the last
        {backtest["days"]} days. Nothing is saved until you apply.</div>
        <table><tr><th>Verdict</th><th class="num">Now</th><th class="num">Candidate</th></tr>
        {rows}</table></div>"""

    body = f"""<h2>Settings</h2>
    <div class="sub">Tune the scoring thresholds, and see the effect before committing.</div>

    <div class="card">
      <h3>Status</h3>
      <div class="meta">
        <span>Active listings <b>{status.get("active", 0)}</b></span>
        <span>Searches <b>{status.get("profiles", 0)}</b></span>
        <span>Alerts today <b>{status.get("alerts_today", 0)}</b></span>
        <span>Model stage <b>{"on" if status.get("ai_enabled") else "off"}</b></span>
        <span>Sending <b>{"dry run" if status.get("dry_run") else "live"}</b></span>
      </div>
    </div>

    <form method="post" action="/settings">
      <div class="card">
        <h3>Scoring thresholds</h3>
        <div class="grid">
          {field("buy_now_discount", "Buy now, discount below market", thresholds["buy_now_discount"])}
          {field("good_deal_discount", "Good deal, discount below market", thresholds["good_deal_discount"])}
          {field("watch_discount", "Watch, auction discount", thresholds["watch_discount"])}
          {field("scam_floor", "Implausibly cheap floor", thresholds["scam_floor"])}
          {field("watch_horizon_hours", "Watch horizon (hours)", thresholds["watch_horizon_hours"], "1")}
        </div>
        <div class="actions">
          <button name="action" value="preview">Preview effect</button>
          <button name="action" value="apply" class="primary">Apply</button>
        </div>
      </div>
    </form>
    {backtest_html}"""
    return page("Settings", "/settings", body, flash)
