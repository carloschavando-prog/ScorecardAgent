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
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

# Manual overrides — values we don't have a feed for yet.
# Set to None to render the card as "TBD" once automation lands.
MANUAL = {
    "google_reviews": 25,    # weekly count, entered by hand
    "employee_count": 32,    # active staff at end of week
    "food_cos_pct":   28.07, # food cost of sales (lower is better, target 30%)
    "bev_cos_pct":     7.57, # beverage cost of sales (lower is better, target 20%)
    "voids_comps_pct": 0.22, # voids & comps as % of net sales (lower is better, target 1%)
}

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
    """Total revenue buckets + per-day totals for a date range,
    merging gotab sales + tripleseat events."""
    rows = fetch_all(
        f"sales?report_date=gte.{start}&report_date=lte.{end}"
        f"&select=report_date,category,product,net_sales"
    )
    buckets = {"food": 0.0, "bev": 0.0, "ent": 0.0, "other": 0.0}
    daily = {}  # date -> total revenue across all buckets
    for r in rows:
        cat = r["category"]
        amt = float(r["net_sales"] or 0)
        d = r["report_date"]
        daily[d] = daily.get(d, 0.0) + amt
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
        daily[e["event_date"]] = daily.get(e["event_date"], 0.0) + (f + b + rest)

    buckets["total"] = buckets["food"] + buckets["bev"] + buckets["ent"] + buckets["other"]
    buckets["daily"] = daily
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


def attendance_for(start, end):
    """Pull staff attendance issues for the window via the existing
    AttendanceAgent core. Returns {late, no_show, called_off, sick, total, incidents}.

    `total` is the scorecard cell value: late + no_show + called_off.
    `incidents` is a short list of {date, name, kind, detail} dicts.
    Returns zeros on failure (e.g. 7Shifts env missing) so the page still renders.
    """
    try:
        # Lazy-import — only generate.py needs this; the deployed static page does not.
        env_path = os.path.expanduser("~/7shifts/.env")
        if os.path.exists(env_path):
            for line in open(env_path):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        sys.path.insert(0, os.path.expanduser("~/Documents/AttendanceAgent/api"))
        import tardiness_core as tc
        rows, _punches, attendance, _name_cat = tc.build(start, end)
        no_show    = sum(a.get("no_show", 0)    for a in attendance.values())
        called_off = sum(a.get("called_off", 0) for a in attendance.values())
        sick       = sum(a.get("sick", 0)       for a in attendance.values())
        late       = len(rows)
        incidents = []
        for r in rows:
            incidents.append({"date": r["date"], "name": r["name"], "kind": "Late",
                              "detail": f'{r["late"]} min · sched {r["sched"]} / in {r["actual"]}'})
        for name, a in attendance.items():
            for _ in range(a.get("no_show", 0)):
                incidents.append({"date": "—", "name": name, "kind": "No-show", "detail": ""})
            for _ in range(a.get("called_off", 0)):
                incidents.append({"date": "—", "name": name, "kind": "Called off", "detail": ""})
        return {"late": late, "no_show": no_show, "called_off": called_off, "sick": sick,
                "total": late + no_show + called_off, "incidents": incidents,
                "ok": True}
    except Exception as e:
        print(f"  ! attendance lookup failed: {e}")
        return {"late": 0, "no_show": 0, "called_off": 0, "sick": 0,
                "total": 0, "incidents": [], "ok": False}


def compute(start, end, with_attendance=False):
    s = sales_for(start, end)
    l = labor_for(start, end)
    total_sales = s["total"]
    food = s["food"]
    bev = s["bev"]
    ent = s["ent"]
    other = s["other"]
    non_food = total_sales - food
    out = {
        "total_sales": total_sales,
        "food_sales": food,
        "bev_sales": bev,
        "ent_sales": ent,
        "other_sales": other,
        "daily_sales": s["daily"],
        "total_labor_pct": (l["total_cost"] / total_sales * 100) if total_sales else 0,
        "kit_labor_pct": (l["kit_cost"] / food * 100) if food else 0,
        "foh_labor_pct": (l["foh_cost"] / non_food * 100) if non_food else 0,
        "total_labor_cost": l["total_cost"],
        "kit_labor_cost": l["kit_cost"],
        "foh_labor_cost": l["foh_cost"],
        "labor_days": l["days"],
    }
    if with_attendance:
        out["attendance"] = attendance_for(start, end)
    return out


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

    # ---- sales cards (YoY — green if up vs last year, red if down) ----
    def sales_card(title, this_val, last_val):
        d_str, direction, pct = delta(this_val, last_val, currency=True)
        arrow = "▲" if direction == "up" else ("▼" if direction == "down" else "■")
        chip_class = (
            "delta-up" if direction == "up"
            else "delta-down" if direction == "down"
            else "delta-flat"
        )
        status_class = (
            "is-hit"  if direction == "up"
            else "is-miss" if direction == "down"
            else ""
        )
        return f"""
        <div class="card sales-card {status_class}">
          <div class="card-title">{title}</div>
          <div class="card-value">{fmt_money(this_val)}</div>
          <div class="card-prior">vs {fmt_money(last_val)} ({last_year['_label']})</div>
          <div class="card-delta {chip_class}">{arrow} {d_str} &nbsp;·&nbsp; {pct:+.1f}%</div>
        </div>
        """

    # ---- labor cards (target-tracked — green if ≤ target, red if over) ----
    def labor_card(title, this_val, last_val, target):
        # better = lower (% of revenue)
        d_str, direction, _ = delta(this_val, last_val)
        # for labor, "up" (cost rose) is bad, "down" is good — flip arrow color
        chip_class = "delta-down" if direction == "up" else ("delta-up" if direction == "down" else "delta-flat")
        arrow = "▲" if direction == "up" else ("▼" if direction == "down" else "■")
        hit_target = this_val <= target
        target_class = "target-hit" if hit_target else "target-miss"
        target_label = "ON TARGET" if hit_target else "OVER TARGET"
        status_class = "is-hit" if hit_target else "is-miss"
        # bar fill: scale so target = 100% width
        bar_pct = min((this_val / target) * 100, 175) if target > 0 else 0
        bar_color = "linear-gradient(90deg, #1cd97b, #1cd97b)" if hit_target else "linear-gradient(90deg, #ff8a3d, #ff3b5c)"
        return f"""
        <div class="card labor-card {status_class}">
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
        sales_card("Total Sales",         this_year["total_sales"], last_year["total_sales"]),
        sales_card("Food Sales",          this_year["food_sales"],  last_year["food_sales"]),
        sales_card("Beverage Sales",      this_year["bev_sales"],   last_year["bev_sales"]),
        sales_card("Entertainment Sales", this_year["ent_sales"],   last_year["ent_sales"]),
    ])

    labor_html = "".join([
        labor_card("Total Labor %",   this_year["total_labor_pct"], last_year["total_labor_pct"], 15.0),
        labor_card("Kitchen Labor %", this_year["kit_labor_pct"],   last_year["kit_labor_pct"],   20.0),
        labor_card("FOH Labor %",     this_year["foh_labor_pct"],   last_year["foh_labor_pct"],   10.0),
    ])

    # Manually-entered cards — Employee Count = hit, Google Reviews = miss for P5W4
    manual_html = ""
    if MANUAL.get("employee_count") is not None:
        manual_html += f"""
        <div class="card manual-card is-hit">
          <div class="card-title">Employee Count</div>
          <div class="card-value">{MANUAL['employee_count']}</div>
          <div class="card-prior">active staff this week</div>
          <div class="card-delta">
            <span class="target-pill target-hit">ON TARGET</span>
            <span class="manual-inline">manual</span>
          </div>
        </div>
        """
    if MANUAL.get("google_reviews") is not None:
        manual_html += f"""
        <div class="card manual-card is-miss">
          <div class="card-title">Google Reviews</div>
          <div class="card-value">{MANUAL['google_reviews']}</div>
          <div class="card-prior">new reviews this week</div>
          <div class="card-delta">
            <span class="target-pill target-miss">BELOW TARGET</span>
            <span class="manual-inline">manual</span>
          </div>
        </div>
        """

    # ---- Cost-of-Sales cards (manual for now, lower = better, target-tracked) ----
    def cost_card(title, value, target):
        hit_target = value <= target
        target_class = "target-hit" if hit_target else "target-miss"
        target_label = "ON TARGET" if hit_target else "OVER TARGET"
        status_class = "is-hit" if hit_target else "is-miss"
        bar_pct = min((value / target) * 100, 175) if target > 0 else 0
        bar_color = ("linear-gradient(90deg, #1cd97b, #1cd97b)"
                     if hit_target
                     else "linear-gradient(90deg, #ff8a3d, #ff3b5c)")
        return f"""
        <div class="card cost-card {status_class}">
          <div class="card-title">{title}</div>
          <div class="card-value">{value:.2f}%</div>
          <div class="card-prior">target ≤ {target:.2f}%</div>
          <div class="labor-bar">
            <div class="labor-bar-fill" style="width: {bar_pct:.1f}%; background: {bar_color};"></div>
            <div class="labor-bar-target" style="left: 100%;"></div>
          </div>
          <div class="card-delta">
            <span class="target-pill {target_class}">{target_label}</span>
            <span class="manual-inline">manual</span>
          </div>
        </div>
        """

    cost_cards = []
    if MANUAL.get("food_cos_pct") is not None:
        cost_cards.append(cost_card("Food COS %",     MANUAL["food_cos_pct"],     30.0))
    if MANUAL.get("bev_cos_pct") is not None:
        cost_cards.append(cost_card("Beverage COS %", MANUAL["bev_cos_pct"],      20.0))
    if MANUAL.get("voids_comps_pct") is not None:
        cost_cards.append(cost_card("Voids & Comps",  MANUAL["voids_comps_pct"],   1.0))
    cost_html = "".join(cost_cards)

    # bottom: placeholders for the rows we don't yet have a data source for
    pending_rows = []
    pending_html = "".join([
        f"""
        <div class="card pending-card">
          <div class="card-title">{n}</div>
          <div class="card-value pending">TBD</div>
          <div class="card-prior">target {t}</div>
          <div class="pending-tag">awaiting data source</div>
        </div>
        """
        for n, t in pending_rows
    ])

    # Staff Attendance Issues — real data via AttendanceAgent core
    att = this_year.get("attendance") or {"total": 0, "late": 0, "no_show": 0,
                                          "called_off": 0, "incidents": [], "ok": False}
    att_total = att["total"]
    att_target_hit = att_total == 0
    att_target_class = "target-hit" if att_target_hit else "target-miss"
    att_target_label = "ON TARGET" if att_target_hit else "OVER TARGET"
    att_status_class = "is-hit" if att_target_hit else "is-miss"
    att_value_color = "var(--green)" if att_target_hit else "var(--red)"
    def _incident_li(i):
        kind_slug = i["kind"].lower().replace("-", "").replace(" ", "")
        detail_html = f'<span class="att-detail">{i["detail"]}</span>' if i["detail"] else ""
        return (f'<li><span class="att-kind att-{kind_slug}">{i["kind"]}</span>'
                f'<span class="att-name">{i["name"]}</span>'
                f'<span class="att-date">{i["date"]}</span>'
                f'{detail_html}</li>')
    att_incidents_html = "".join(_incident_li(i) for i in att["incidents"]) or \
        '<li class="att-empty">No incidents this week.</li>'
    attendance_card_html = f"""
        <div class="card attendance-card {att_status_class}">
          <div class="card-title">Staff Attendance Issues</div>
          <div class="card-value" style="color: {att_value_color};">{att_total}</div>
          <div class="card-prior">{att['late']} late · {att['no_show']} no-show · {att['called_off']} called off · target 0</div>
          <div class="card-delta">
            <span class="target-pill {att_target_class}">{att_target_label}</span>
          </div>
          <details class="att-details">
            <summary>{'View incidents' if att['incidents'] else 'No incidents'}</summary>
            <ul class="att-list">{att_incidents_html}</ul>
          </details>
        </div>
    """


    # Headline summary
    sales_delta_pct = ((this_year["total_sales"] - last_year["total_sales"]) / last_year["total_sales"] * 100) if last_year["total_sales"] else 0
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
    --bg-1: #07101c;
    --bg-2: #0a1626;
    --bg-3: #0f1d33;
    --fg: #f1f4f8;
    --fg-dim: #8ea0b8;
    --card-bg: rgba(255,255,255,0.045);
    --card-border: rgba(255,255,255,0.09);
    --green: #1cd97b;
    --green-soft: rgba(28, 217, 123, 0.18);
    --green-glow: rgba(28, 217, 123, 0.35);
    --red: #ff3b5c;
    --red-soft: rgba(255, 59, 92, 0.18);
    --red-glow: rgba(255, 59, 92, 0.35);
    --orange: #ff8a3d;
    --purple: #7c5cff;
    --neutral: #5c6b80;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    color: var(--fg);
    background: var(--bg-1);
    background-image:
      radial-gradient(circle at 12% -10%, rgba(28, 217, 123, 0.10), transparent 50%),
      radial-gradient(circle at 88% -10%, rgba(255, 59, 92, 0.10), transparent 50%),
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
    color: #fff;
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
    gap: 16px;
    padding: 16px 32px;
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 999px;
    font-size: 17px;
    font-weight: 500;
  }}
  .hero-headline .stat {{
    font-size: 22px;
    padding: 2px 4px;
  }}
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
    --accent: var(--neutral);
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
    background: linear-gradient(135deg, var(--accent) 0%, transparent 55%);
    opacity: 0.10;
    pointer-events: none;
  }}
  .card::after {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 5px;
    background: var(--accent);
    opacity: 0.95;
  }}
  .card:hover {{
    transform: translateY(-3px);
    border-color: rgba(255,255,255,0.20);
    box-shadow: 0 18px 40px -18px rgba(0,0,0,0.6);
  }}
  /* Strong status variants — applied via card class */
  .card.is-hit  {{
    --accent: var(--green);
    border-color: rgba(28, 217, 123, 0.30);
    box-shadow: 0 0 0 1px rgba(28, 217, 123, 0.12) inset;
  }}
  .card.is-miss {{
    --accent: var(--red);
    border-color: rgba(255, 59, 92, 0.30);
    box-shadow: 0 0 0 1px rgba(255, 59, 92, 0.12) inset;
  }}
  .card.is-neutral {{
    --accent: var(--neutral);
  }}
  .card.is-neutral::after {{ height: 3px; opacity: 0.55; }}
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
  .delta-up   {{ background: var(--green-soft); color: var(--green); }}
  .delta-down {{ background: var(--red-soft);   color: var(--red); }}
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
    display: inline-block;
    padding: 5px 12px;
    border-radius: 8px;
    font-size: 12px;
    font-weight: 800;
    letter-spacing: 0.10em;
    margin-right: 6px;
    text-transform: uppercase;
  }}
  .target-hit  {{
    background: var(--green-soft);
    color: var(--green);
    box-shadow: 0 0 0 1px rgba(28, 217, 123, 0.35) inset;
  }}
  .target-miss {{
    background: var(--red-soft);
    color: var(--red);
    box-shadow: 0 0 0 1px rgba(255, 59, 92, 0.35) inset;
  }}

  .pending-card {{
    opacity: 0.78;
  }}
  .pending-tag {{
    margin-top: 12px;
    font-size: 12px;
    color: var(--fg-dim);
    font-style: italic;
  }}

  /* Manual entry cards (Google Reviews, Employee Count) */
  .manual-tag {{
    margin-top: 12px;
    font-size: 11px;
    color: var(--fg-dim);
    font-style: italic;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }}
  .manual-inline {{
    font-size: 10px;
    color: var(--fg-dim);
    text-transform: uppercase;
    letter-spacing: 0.10em;
    margin-left: 8px;
    font-weight: 600;
    opacity: 0.7;
  }}

  /* Attendance card details */
  .attendance-card {{ grid-column: span 2; }}
  .att-details {{
    margin-top: 14px;
    border-top: 1px solid rgba(255,255,255,0.08);
    padding-top: 12px;
  }}
  .att-details summary {{
    cursor: pointer;
    font-size: 13px;
    color: var(--fg-dim);
    font-weight: 500;
    list-style: none;
    user-select: none;
    padding: 4px 0;
  }}
  .att-details summary::-webkit-details-marker {{ display: none; }}
  .att-details summary::before {{
    content: '▸ ';
    display: inline-block;
    transition: transform .15s ease;
  }}
  .att-details[open] summary::before {{ transform: rotate(90deg); }}
  .att-list {{
    list-style: none;
    padding: 8px 0 0;
    margin: 0;
  }}
  .att-list li {{
    display: grid;
    grid-template-columns: 80px 1fr auto;
    gap: 10px;
    align-items: center;
    padding: 8px 0;
    border-bottom: 1px dashed rgba(255,255,255,0.06);
    font-size: 13px;
  }}
  .att-list li:last-child {{ border-bottom: none; }}
  .att-kind {{
    font-family: 'Space Grotesk', sans-serif;
    font-size: 11px;
    font-weight: 700;
    padding: 3px 8px;
    border-radius: 6px;
    text-align: center;
  }}
  .att-late      {{ background: rgba(255,138,61,0.18); color: var(--orange); }}
  .att-noshow    {{ background: rgba(255,84,112,0.20); color: var(--red); }}
  .att-calledoff {{ background: rgba(255,84,112,0.14); color: var(--red); }}
  .att-name      {{ font-weight: 500; }}
  .att-date      {{ color: var(--fg-dim); font-size: 12px; }}
  .att-detail    {{ grid-column: 2 / -1; color: var(--fg-dim); font-size: 12px; }}
  .att-empty     {{ color: var(--fg-dim); font-style: italic; padding: 12px 0 !important; border: none !important; display: block !important; }}

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

  <h2 class="section-title">Cost of Sales <span class="badge">lower is better</span></h2>
  <div class="grid grid-3">{cost_html}</div>

  <h2 class="section-title">Team <span class="badge">attendance · headcount · reviews</span></h2>
  <div class="grid grid-3">
    {attendance_card_html}
    {manual_html}
  </div>

  {'<h2 class="section-title">Operational Metrics <span class="badge">data wiring in progress</span></h2><div class="grid grid-6">' + pending_html + '</div>' if pending_html else ''}

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
        # default: P5W4 2026 vs P5W4 2025
        # Note: FY2025 P5W4 = May 26 – Jun 1 (per ops calendar — FY2024 was a
        # 53-week year, so the prior-year fiscal alignment is one week later
        # than a naive 52-week subtraction would suggest).
        this_start, this_end = "2026-05-25", "2026-05-31"
        last_start, last_end = "2025-05-26", "2025-06-01"
        label = "P5W4"
    elif len(args) >= 5:
        this_start, this_end, last_start, last_end, label = args[:5]
    else:
        print("Usage: generate.py [this_start this_end last_start last_end label]")
        sys.exit(2)

    print(f"Fetching {label}: {this_start}→{this_end} vs {last_start}→{last_end}")
    this_year = compute(this_start, this_end, with_attendance=True)
    last_year = compute(last_start, last_end, with_attendance=False)
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
