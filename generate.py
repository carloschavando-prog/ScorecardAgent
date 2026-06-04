#!/usr/bin/env python3
"""
ScorecardAgent — generate the weekly EOS Pulse scorecard HTML.

Pulls Sales (sales + ts_events) and Labor (labor_daily) from Supabase
for a given fiscal week and the prior-year fiscal week, computes the
scorecard lines we have data for, and writes index.html.

Usage:
    python generate.py              # defaults to P5W4 (May 25-31, 2026)
    python generate.py 2026-05-25 2026-05-31 2025-05-19 2025-05-25 P5W4
"""
import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

SUPA = "https://jrzfczhsqshejnrxgmuq.supabase.co"
KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImpyemZjemhzcXNoZWpucnhnbXVxIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3OTIxNzgwNywiZXhwIjoyMDk0NzkzODA3fQ."
    "dXTVEBfxBaU4_3dLiBqqvaOX-XWGbs46uTb-dhmINFc"
)

# Category mappings (mirrors daily_revenue logic used by SalesVarianceAgent)
FOOD_CATS = {
    "Chicken.", "Extra Sauces and Cheese Dips", "Fry Platters",
    "Half Pound Burgers", "Mozzarella Sticks", "Pizza and Flatbreads",
    "Pretzels", "Tater Kegs", "Wraps", "Dessert", "Event Food",
    "Burgers", "Tacos", "Legacy menu Items",
}
BEV_CATS = {"Beverage", "Soda Pop", "Wine", "Bottle Service"}
ENT_CATS = {"Entertainment", "Karaoke"}  # gotab entertainment & karaoke


def get(url):
    req = urllib.request.Request(
        url, headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"}
    )
    return json.loads(urllib.request.urlopen(req).read())


def fetch_all(path):
    """Page through PostgREST results to bypass the 1000-row cap."""
    out, off = [], 0
    while True:
        sep = "&" if "?" in path else "?"
        rows = get(f"{SUPA}/rest/v1/{path}{sep}limit=1000&offset={off}")
        out.extend(rows)
        if len(rows) < 1000:
            break
        off += 1000
    return out


def sales_for(start, end):
    """Total revenue buckets for a date range, merging gotab sales + tripleseat events."""
    rows = fetch_all(
        f"sales?report_date=gte.{start}&report_date=lte.{end}"
        f"&select=report_date,category,product,net_sales"
    )
    buckets = {"food": 0.0, "bev": 0.0, "ent": 0.0, "other": 0.0}
    for r in rows:
        cat = r["category"]
        amt = float(r["net_sales"] or 0)
        if cat in FOOD_CATS:
            buckets["food"] += amt
        elif cat in BEV_CATS:
            buckets["bev"] += amt
        elif cat in ENT_CATS:
            buckets["ent"] += amt
        else:
            buckets["other"] += amt

    # Add Tripleseat events — only DEFINITE/CLOSED count toward revenue.
    events = fetch_all(
        f"ts_events?event_date=gte.{start}&event_date=lte.{end}"
        f"&deleted_at=is.null"
        f"&select=event_date,status,food_amount,beverage_amount,actual_amount"
    )
    for e in events:
        if e["status"] not in ("DEFINITE", "CLOSED", "TENTATIVE"):
            continue
        f = float(e["food_amount"] or 0)
        b = float(e["beverage_amount"] or 0)
        a = float(e["actual_amount"] or 0)
        rest = max(a - f - b, 0)  # booking fees + entertainment add-ons
        buckets["food"] += f
        buckets["bev"] += b
        buckets["ent"] += rest

    buckets["total"] = sum(buckets.values()) - buckets["total"] if "total" in buckets else \
        buckets["food"] + buckets["bev"] + buckets["ent"] + buckets["other"]
    return buckets


def labor_for(start, end):
    rows = fetch_all(
        f"labor_daily?date=gte.{start}&date=lte.{end}"
        f"&select=date,kit_hours,kit_cost,foh_hours,foh_cost,total_hours,total_cost"
    )
    sums = {"kit_hours": 0.0, "kit_cost": 0.0, "foh_hours": 0.0, "foh_cost": 0.0,
            "total_hours": 0.0, "total_cost": 0.0, "days": len(rows)}
    for r in rows:
        for k in ("kit_hours", "kit_cost", "foh_hours", "foh_cost",
                  "total_hours", "total_cost"):
            sums[k] += float(r[k] or 0)
    return sums


def compute(start, end):
    s = sales_for(start, end)
    l = labor_for(start, end)
    total_sales = s["total"]
    food = s["food"]
    bev = s["bev"]
    ent = s["ent"]
    other = s["other"]
    non_food = total_sales - food
    return {
        "total_sales": total_sales,
        "food_sales": food,
        "bev_sales": bev,
        "ent_sales": ent,
        "other_sales": other,
        "total_labor_pct": (l["total_cost"] / total_sales * 100) if total_sales else 0,
        "kit_labor_pct": (l["kit_cost"] / food * 100) if food else 0,
        "foh_labor_pct": (l["foh_cost"] / non_food * 100) if non_food else 0,
        "total_labor_cost": l["total_cost"],
        "kit_labor_cost": l["kit_cost"],
        "foh_labor_cost": l["foh_cost"],
        "labor_days": l["days"],
    }


def fmt_money(v):
    return f"${v:,.0f}"


def fmt_pct(v):
    return f"{v:.2f}%"


def delta(curr, prior, currency=False):
    """Return (delta_value_str, direction, pct_change)."""
    if prior == 0:
        return ("—", "flat", 0.0)
    diff = curr - prior
    pct = (diff / prior) * 100
    if currency:
        v = f"{'+' if diff >= 0 else '−'}${abs(diff):,.0f}"
    else:
        v = f"{'+' if diff >= 0 else '−'}{abs(diff):.2f}pp"
    direction = "up" if diff > 0 else ("down" if diff < 0 else "flat")
    return (v, direction, pct)


def build_html(label, this_start, this_end, this_year, last_start, last_end, last_year):
    """label: e.g. 'P5W4'. *_start/*_end are ISO dates. *_year is a computed dict."""

    # ---- sales cards ----
    def sales_card(title, emoji, this_val, last_val, accent):
        d_str, direction, pct = delta(this_val, last_val, currency=True)
        arrow = "▲" if direction == "up" else ("▼" if direction == "down" else "■")
        chip_class = (
            "delta-up" if direction == "up"
            else "delta-down" if direction == "down"
            else "delta-flat"
        )
        return f"""
        <div class="card sales-card" style="--accent: {accent}">
          <div class="card-emoji">{emoji}</div>
          <div class="card-title">{title}</div>
          <div class="card-value">{fmt_money(this_val)}</div>
          <div class="card-prior">vs {fmt_money(last_val)} ({last_year['_label']})</div>
          <div class="card-delta {chip_class}">{arrow} {d_str} &nbsp;·&nbsp; {pct:+.1f}%</div>
        </div>
        """

    # ---- labor cards (with target bars) ----
    def labor_card(title, emoji, this_val, last_val, target, accent):
        # better = lower (% of revenue)
        d_str, direction, _ = delta(this_val, last_val)
        # for labor, "up" (cost rose) is bad, "down" is good — flip arrow color
        chip_class = "delta-down" if direction == "up" else ("delta-up" if direction == "down" else "delta-flat")
        arrow = "▲" if direction == "up" else ("▼" if direction == "down" else "■")
        hit_target = this_val <= target
        target_class = "target-hit" if hit_target else "target-miss"
        target_label = "ON TARGET 🎯" if hit_target else "OVER TARGET"
        # bar fill: scale so target = 100% width
        bar_pct = min((this_val / target) * 100, 175) if target > 0 else 0
        bar_color = "linear-gradient(90deg, #16d39a, #16d39a)" if hit_target else "linear-gradient(90deg, #ff8a3d, #ff5470)"
        return f"""
        <div class="card labor-card" style="--accent: {accent}">
          <div class="card-emoji">{emoji}</div>
          <div class="card-title">{title}</div>
          <div class="card-value">{fmt_pct(this_val)}</div>
          <div class="card-prior">vs {fmt_pct(last_val)} ({last_year['_label']}) · target ≤ {fmt_pct(target)}</div>
          <div class="labor-bar">
            <div class="labor-bar-fill" style="width: {bar_pct:.1f}%; background: {bar_color};"></div>
            <div class="labor-bar-target" style="left: 100%;"></div>
          </div>
          <div class="card-delta {chip_class}">
            <span class="target-pill {target_class}">{target_label}</span>
            &nbsp; {arrow} {d_str} YoY
          </div>
        </div>
        """

    sales_html = "".join([
        sales_card("Total Sales",        "💰", this_year["total_sales"], last_year["total_sales"], "#7c5cff"),
        sales_card("Food Sales",         "🍔", this_year["food_sales"],  last_year["food_sales"],  "#ff8a3d"),
        sales_card("Beverage Sales",     "🍹", this_year["bev_sales"],   last_year["bev_sales"],   "#16d39a"),
        sales_card("Entertainment Sales","🎯", this_year["ent_sales"],   last_year["ent_sales"],   "#ff5470"),
    ])

    labor_html = "".join([
        labor_card("Total Labor %",  "👥", this_year["total_labor_pct"], last_year["total_labor_pct"], 15.0, "#7c5cff"),
        labor_card("Kitchen Labor %","👨‍🍳", this_year["kit_labor_pct"],   last_year["kit_labor_pct"],   20.0, "#ff8a3d"),
        labor_card("FOH Labor %",    "🍽️", this_year["foh_labor_pct"],   last_year["foh_labor_pct"],   10.0, "#16d39a"),
    ])

    # bottom: placeholders for the rows we don't yet have a data source for
    pending_rows = [
        ("Food COS %",             "🥩", "≤ 30%"),
        ("Beverage COS %",         "🍷", "≤ 20%"),
        ("Employee Count",         "🧑‍🤝‍🧑", "—"),
        ("Google Review Count",    "⭐", "—"),
        ("Voids & Comps",          "🚫", "≤ 1%"),
        ("Staff Attendance Issues","📋", "0"),
    ]
    pending_html = "".join([
        f"""
        <div class="card pending-card">
          <div class="card-emoji">{e}</div>
          <div class="card-title">{n}</div>
          <div class="card-value pending">TBD</div>
          <div class="card-prior">target {t}</div>
          <div class="pending-tag">📡 awaiting data source</div>
        </div>
        """
        for n, e, t in pending_rows
    ])

    # Headline summary
    sales_delta_pct = ((this_year["total_sales"] - last_year["total_sales"]) / last_year["total_sales"] * 100) if last_year["total_sales"] else 0
    headline_emoji = "🚀" if sales_delta_pct > 5 else ("📈" if sales_delta_pct > 0 else ("📉" if sales_delta_pct < -5 else "➡️"))
    labor_hit = sum([
        this_year["total_labor_pct"] <= 15,
        this_year["kit_labor_pct"] <= 20,
        this_year["foh_labor_pct"] <= 10,
    ])

    # Format dates for display
    def pretty(d):
        y, m, dd = d.split("-")
        months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        return f"{months[int(m)-1]} {int(dd)}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>On Par · {label} Scorecard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg-1: #0b0820;
    --bg-2: #1a0f3a;
    --bg-3: #2d1b5e;
    --fg: #f3f0ff;
    --fg-dim: #b8a8e0;
    --card-bg: rgba(255,255,255,0.06);
    --card-border: rgba(255,255,255,0.10);
    --green: #16d39a;
    --red: #ff5470;
    --orange: #ff8a3d;
    --purple: #7c5cff;
    --pink: #ff5470;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    color: var(--fg);
    background: var(--bg-1);
    background-image:
      radial-gradient(circle at 10% 0%, rgba(124, 92, 255, 0.35), transparent 40%),
      radial-gradient(circle at 90% 0%, rgba(255, 84, 112, 0.25), transparent 40%),
      radial-gradient(circle at 50% 100%, rgba(22, 211, 154, 0.20), transparent 50%),
      linear-gradient(180deg, var(--bg-1), var(--bg-2));
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }}
  .container {{
    max-width: 1280px;
    margin: 0 auto;
    padding: 48px 24px 80px;
  }}
  .hero {{
    text-align: center;
    margin-bottom: 56px;
  }}
  .hero-eyebrow {{
    display: inline-block;
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 600;
    font-size: 13px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--fg-dim);
    padding: 8px 16px;
    border: 1px solid rgba(255,255,255,0.15);
    border-radius: 999px;
    background: rgba(255,255,255,0.03);
    backdrop-filter: blur(8px);
  }}
  .hero h1 {{
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 700;
    font-size: clamp(40px, 6vw, 72px);
    margin: 18px 0 12px;
    letter-spacing: -0.02em;
    background: linear-gradient(135deg, #fff 0%, #c0a8ff 50%, #ff8fb0 100%);
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    line-height: 1.05;
  }}
  .hero-sub {{
    font-size: 18px;
    color: var(--fg-dim);
    margin: 0;
  }}
  .hero-headline {{
    margin-top: 28px;
    display: inline-flex;
    align-items: center;
    gap: 14px;
    padding: 14px 28px;
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 999px;
    font-size: 16px;
    font-weight: 500;
  }}
  .hero-headline .big-emoji {{ font-size: 28px; }}
  .hero-headline .stat {{
    font-weight: 700;
    font-family: 'Space Grotesk', sans-serif;
  }}
  .hero-headline .stat.up {{ color: var(--green); }}
  .hero-headline .stat.down {{ color: var(--red); }}

  .section-title {{
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 700;
    font-size: 28px;
    margin: 56px 0 24px;
    display: flex;
    align-items: center;
    gap: 12px;
  }}
  .section-title .badge {{
    font-size: 12px;
    padding: 4px 10px;
    border-radius: 999px;
    background: rgba(255,255,255,0.08);
    color: var(--fg-dim);
    font-weight: 500;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }}

  .grid {{
    display: grid;
    gap: 20px;
  }}
  .grid-4 {{ grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }}
  .grid-3 {{ grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }}
  .grid-6 {{ grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }}

  .card {{
    --accent: var(--purple);
    position: relative;
    padding: 24px;
    border-radius: 20px;
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    backdrop-filter: blur(12px);
    overflow: hidden;
    transition: transform .22s ease, box-shadow .22s ease, border-color .22s ease;
    opacity: 0;
    transform: translateY(16px);
    animation: fadeup .55s cubic-bezier(.2,.7,.2,1) forwards;
  }}
  .card::before {{
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(135deg, var(--accent) 0%, transparent 60%);
    opacity: 0.08;
    pointer-events: none;
  }}
  .card::after {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
    background: var(--accent);
    opacity: 0.85;
  }}
  .card:hover {{
    transform: translateY(-3px);
    border-color: rgba(255,255,255,0.20);
    box-shadow: 0 18px 40px -18px rgba(0,0,0,0.6);
  }}
  .card-emoji {{
    font-size: 32px;
    margin-bottom: 8px;
  }}
  .card-title {{
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 600;
    font-size: 14px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-dim);
    margin-bottom: 8px;
  }}
  .card-value {{
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 700;
    font-size: 38px;
    letter-spacing: -0.02em;
    line-height: 1.1;
  }}
  .card-value.pending {{
    color: var(--fg-dim);
    font-size: 28px;
    opacity: 0.6;
  }}
  .card-prior {{
    margin-top: 6px;
    font-size: 13px;
    color: var(--fg-dim);
  }}
  .card-delta {{
    margin-top: 14px;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 13px;
    font-weight: 600;
    padding: 6px 10px;
    border-radius: 8px;
    font-family: 'Space Grotesk', sans-serif;
  }}
  .delta-up   {{ background: rgba(22,211,154,0.14); color: var(--green); }}
  .delta-down {{ background: rgba(255,84,112,0.14); color: var(--red); }}
  .delta-flat {{ background: rgba(255,255,255,0.08); color: var(--fg-dim); }}

  .labor-bar {{
    margin: 14px 0 6px;
    position: relative;
    width: 100%;
    height: 8px;
    background: rgba(255,255,255,0.06);
    border-radius: 999px;
    overflow: visible;
  }}
  .labor-bar-fill {{
    position: absolute;
    top: 0; left: 0;
    height: 100%;
    border-radius: 999px;
    transition: width 1.2s cubic-bezier(.2,.7,.2,1);
    max-width: 100%;
  }}
  .labor-bar-target {{
    position: absolute;
    top: -4px;
    width: 2px;
    height: 16px;
    background: rgba(255,255,255,0.45);
    transform: translateX(-1px);
  }}
  .target-pill {{
    padding: 3px 8px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.06em;
    margin-right: 4px;
  }}
  .target-hit  {{ background: rgba(22,211,154,0.18); color: var(--green); }}
  .target-miss {{ background: rgba(255,84,112,0.18); color: var(--red); }}

  .pending-card {{
    opacity: 0.78;
  }}
  .pending-tag {{
    margin-top: 12px;
    font-size: 12px;
    color: var(--fg-dim);
    font-style: italic;
  }}

  footer {{
    text-align: center;
    margin-top: 64px;
    color: var(--fg-dim);
    font-size: 13px;
  }}
  footer .small {{
    margin-top: 6px;
    font-size: 11px;
    opacity: 0.6;
  }}

  @keyframes fadeup {{
    to {{ opacity: 1; transform: translateY(0); }}
  }}
  /* Stagger card animations */
  .grid .card:nth-child(1) {{ animation-delay: .05s; }}
  .grid .card:nth-child(2) {{ animation-delay: .12s; }}
  .grid .card:nth-child(3) {{ animation-delay: .19s; }}
  .grid .card:nth-child(4) {{ animation-delay: .26s; }}
  .grid .card:nth-child(5) {{ animation-delay: .33s; }}
  .grid .card:nth-child(6) {{ animation-delay: .40s; }}

  @media (max-width: 640px) {{
    .container {{ padding: 32px 16px 56px; }}
    .card-value {{ font-size: 30px; }}
  }}
</style>
</head>
<body>
<div class="container">

  <header class="hero">
    <span class="hero-eyebrow">EOS Pulse Scorecard · On Par Entertainment</span>
    <h1>{label}<br/><span style="font-size: 0.55em; opacity: 0.8;">{pretty(this_start)} – {pretty(this_end)}, {this_start[:4]}</span></h1>
    <p class="hero-sub">Same fiscal week last year: {pretty(last_start)} – {pretty(last_end)}, {last_start[:4]}</p>
    <div class="hero-headline">
      <span class="big-emoji">{headline_emoji}</span>
      <span>Total Sales</span>
      <span class="stat {'up' if sales_delta_pct > 0 else 'down' if sales_delta_pct < 0 else 'flat'}">{sales_delta_pct:+.1f}%</span>
      <span>YoY</span>
      <span style="opacity: .4;">·</span>
      <span class="stat" style="color: {'var(--green)' if labor_hit == 3 else 'var(--orange)' if labor_hit >= 1 else 'var(--red)'};">{labor_hit}/3</span>
      <span>labor targets hit</span>
    </div>
  </header>

  <h2 class="section-title">Sales <span class="badge">vs same week last year</span></h2>
  <div class="grid grid-4">{sales_html}</div>

  <h2 class="section-title">Labor <span class="badge">% of revenue · lower is better</span></h2>
  <div class="grid grid-3">{labor_html}</div>

  <h2 class="section-title">Operational Metrics <span class="badge">data wiring in progress</span></h2>
  <div class="grid grid-6">{pending_html}</div>

  <footer>
    Generated by <strong>ScorecardAgent</strong> · Source: Supabase <code>labor_daily</code>, <code>sales</code>, <code>ts_events</code>
    <div class="small">{label} · {this_start} → {this_end}  ·  YoY: {last_start} → {last_end}</div>
  </footer>

</div>
</body>
</html>"""


def main():
    args = sys.argv[1:]
    if len(args) == 0:
        # default: P5W4 2026
        this_start, this_end = "2026-05-25", "2026-05-31"
        last_start, last_end = "2025-05-19", "2025-05-25"
        label = "P5W4"
    elif len(args) >= 5:
        this_start, this_end, last_start, last_end, label = args[:5]
    else:
        print("Usage: generate.py [this_start this_end last_start last_end label]")
        sys.exit(2)

    print(f"Fetching {label}: {this_start}→{this_end} vs {last_start}→{last_end}")
    this_year = compute(this_start, this_end)
    last_year = compute(last_start, last_end)
    this_year["_label"] = this_start[:4]
    last_year["_label"] = last_start[:4]

    print("\nThis year:")
    for k, v in this_year.items():
        print(f"  {k:20s} {v}")
    print("\nLast year:")
    for k, v in last_year.items():
        print(f"  {k:20s} {v}")

    html = build_html(label, this_start, this_end, this_year, last_start, last_end, last_year)
    out = Path(__file__).parent / "index.html"
    out.write_text(html)
    print(f"\nWrote {out}  ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
