"""Stitch-faithful markup for the explorer. Numbers come from callers — never invented."""

from __future__ import annotations

from html import escape
from urllib.parse import urlencode

from app.data import pretty_label, source_display_name

NAV = (
    ("overview", "dashboard", "Overview"),
    ("map", "map", "Opportunity Map"),
    ("evidence", "database", "Evidence Explorer"),
    ("segments", "groups", "Segments"),
    ("ajio", "verified", "AJIO Corroboration"),
    ("ask", "psychology", "Ask the Engine"),
)


def qp_href(current: dict[str, str], **updates) -> str:
    params = {key: value for key, value in current.items() if value not in (None, "")}
    for key, value in updates.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = str(value)
    return "/?" + urlencode(params) if params else "/"


CHROME_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@500&family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@24,400,0,0&display=swap');

:root {
  --bg: #f8f9fa;
  --surface: #ffffff;
  --on: #191c1d;
  --secondary: #5f5e5e;
  --primary: #9e0000;
  --primary-container: #cc0000;
  --on-primary: #ffffff;
  --outline: #e8bdb6;
  --ghost: #e5e7eb;
  --container: #f3f4f5;
  --container-high: #e7e8e9;
  --tertiary: #444c62;
  --tertiary-container: #5c647b;
  --error: #ba1a1a;
  --error-container: #ffdad6;
  --sidebar: 260px;
  --top: 64px;
}

[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
#MainMenu, footer, .stDeployButton,
.stAppDeployButton { display: none !important; }

header[data-testid="stHeader"] {
  background: transparent !important;
  border: none !important;
}
[data-testid="stToolbar"] {
  background: transparent !important;
  border: none !important;
}
header[data-testid="stHeader"]:has([data-testid="stExpandSidebarButton"]) {
  min-height: 3.5rem !important;
}

[data-testid="stExpandSidebarButton"] {
  display: inline-flex !important;
  visibility: visible !important;
  opacity: 1 !important;
  z-index: 1000000 !important;
}

.stApp, [data-testid="stAppViewContainer"], [data-testid="stAppViewContainer"] > .main {
  background: var(--bg) !important;
  color: var(--on);
}
.block-container {
  padding-top: 1.5rem !important;
  padding-bottom: 3rem !important;
  max-width: 1480px !important;
}
[data-testid="stSidebar"] {
  background: #ffffff !important;
  border-right: 1px solid #e5e7eb;
}
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h1 {
  font-size: 24px; font-weight: 700; color: #9e0000; letter-spacing: -0.01em; margin: 0;
}
[data-testid="stSidebar"] button {
  justify-content: flex-start !important;
  font-family: Inter, system-ui, sans-serif !important;
  border-radius: 0 8px 8px 0 !important;
}
[data-testid="stSidebar"] button[kind="primary"] {
  background: #f3f4f5 !important;
  color: #9e0000 !important;
  border-left: 4px solid #9e0000 !important;
  font-weight: 700 !important;
}
[data-testid="stVerticalBlock"] { gap: 0.4rem !important; }

.ad-nav, .ad-top {
  font-family: Inter, system-ui, sans-serif;
}
.ad-nav {
  position: fixed; left: 0; top: 0; bottom: 0; width: var(--sidebar);
  background: var(--surface); border-right: 1px solid var(--ghost);
  z-index: 50; display: flex; flex-direction: column;
}
.ad-brand { padding: 28px 24px 16px; }
.ad-brand h1 { margin: 0; font-size: 24px; line-height: 32px; font-weight: 700; color: var(--primary); letter-spacing: -0.01em; }
.ad-brand p { margin: 4px 0 0; font-family: "JetBrains Mono", monospace; font-size: 12px; letter-spacing: 0.05em; text-transform: uppercase; color: var(--secondary); }
.ad-nav-ask {
  margin: 8px 16px 16px; display: flex; align-items: center; justify-content: center; gap: 8px;
  background: var(--primary); color: #fff; padding: 10px 14px; border-radius: 9999px;
  font-size: 14px; font-weight: 700; text-decoration: none;
}
.ad-nav-ask:hover { background: #930000; }
.ad-links { flex: 1; overflow: auto; padding: 8px 8px 0; }
.ad-link {
  display: flex; align-items: center; gap: 12px; padding: 12px 16px; margin-bottom: 2px;
  color: var(--secondary); text-decoration: none; border-radius: 0 8px 8px 0; font-size: 14px;
}
.ad-link:hover { background: #edeeef; color: var(--on); }
.ad-link.active {
  color: var(--primary); font-weight: 700; background: var(--container);
  border-left: 4px solid var(--primary); padding-left: 12px;
}
.ad-foot { padding: 16px 24px 24px; border-top: 1px solid var(--ghost); font-family: "JetBrains Mono", monospace; font-size: 12px; letter-spacing: 0.05em; text-transform: uppercase; color: var(--secondary); }
.ad-foot div { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
.material-symbols-outlined { font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24; font-size: 20px; vertical-align: middle; }
.ad-link.active .material-symbols-outlined { font-variation-settings: 'FILL' 1; }

.ad-top {
  position: fixed; top: 0; left: var(--sidebar); right: 0; height: var(--top);
  background: var(--surface); border-bottom: 1px solid var(--ghost);
  z-index: 40; display: flex; align-items: center; justify-content: space-between; padding: 0 32px;
}
.ad-seg { display: flex; background: var(--container); border: 1px solid var(--ghost); border-radius: 9999px; padding: 4px; gap: 2px; }
.ad-seg a {
  padding: 6px 16px; border-radius: 9999px; text-decoration: none; font-size: 14px; color: var(--secondary);
}
.ad-seg a.on { background: #fff; color: var(--primary); font-weight: 700; box-shadow: 0 1px 2px rgba(0,0,0,.06); }
.ad-top-right { display: flex; align-items: center; gap: 16px; }
.ad-pill {
  font-family: "JetBrains Mono", monospace; font-size: 12px; letter-spacing: 0.05em; text-transform: uppercase;
  color: var(--secondary); text-decoration: none; border: 1px solid var(--ghost); padding: 6px 12px; border-radius: 6px;
}
.ad-pill.on, .ad-pill:hover { border-color: var(--primary); color: var(--primary); }
.ad-ask-btn {
  background: var(--primary); color: #fff; text-decoration: none; padding: 8px 16px; border-radius: 6px;
  font-family: "JetBrains Mono", monospace; font-size: 12px; letter-spacing: 0.05em; text-transform: uppercase;
}

.ad-page { font-family: Inter, system-ui, sans-serif; color: var(--on); }
.ad-kicker { font-family: "JetBrains Mono", monospace; font-size: 12px; letter-spacing: 0.05em; text-transform: uppercase; color: var(--primary); }
.ad-display { font-size: 48px; line-height: 56px; font-weight: 700; letter-spacing: -0.02em; margin: 0; }
.ad-sub { font-size: 18px; color: var(--secondary); margin: 8px 0 0; }
.ad-banner {
  background: var(--container); border: 1px solid var(--ghost); border-radius: 8px;
  padding: 24px; display: flex; justify-content: space-between; align-items: center; margin: 24px 0;
}
.ad-banner h3 { margin: 0 0 4px; font-family: "JetBrains Mono", monospace; font-size: 12px; letter-spacing: 0.05em; text-transform: uppercase; color: var(--primary); }
.ad-banner p { margin: 0; font-size: 24px; font-weight: 600; }
.ad-icon-round { width: 48px; height: 48px; border-radius: 9999px; background: var(--primary-container); color: #ffdad4; display: flex; align-items: center; justify-content: center; }

.ad-kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 24px; margin-bottom: 24px; }
.ad-card { background: var(--surface); border: 1px solid var(--ghost); border-radius: 8px; padding: 16px 20px; }
.ad-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,.05); }
.ad-card .lbl { font-family: "JetBrains Mono", monospace; font-size: 12px; letter-spacing: 0.05em; text-transform: uppercase; color: var(--secondary); margin-bottom: 8px; }
.ad-card .num { font-size: 32px; line-height: 40px; font-weight: 700; letter-spacing: -0.02em; }
.ad-card .num.accent { color: var(--primary); }
.ad-card.tint { background: var(--container); }

.ad-grid-2 { display: grid; grid-template-columns: 2fr 1fr; gap: 24px; }
.ad-grid-half { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-top: 24px; }
.ad-grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 24px; margin: 24px 0; }
.ad-pad { padding: 24px; }
.ad-h { font-size: 24px; font-weight: 600; margin: 0 0 16px; }

.ad-funnel { position: relative; display: flex; justify-content: space-between; align-items: center; margin: 48px 0 16px; }
.ad-funnel::before { content: ""; position: absolute; left: 0; right: 0; top: 16px; height: 2px; background: var(--ghost); }
.ad-step { position: relative; background: var(--surface); padding: 0 8px; text-align: center; z-index: 1; }
.ad-dot { width: 32px; height: 32px; border-radius: 9999px; border: 1px solid var(--ghost); background: var(--container-high); margin: 0 auto 8px; display: flex; align-items: center; justify-content: center; font-family: "JetBrains Mono", monospace; font-size: 11px; color: var(--secondary); }
.ad-step.gap .ad-dot { width: 40px; height: 40px; background: var(--error); color: #fff; border: 2px solid var(--error-container); font-weight: 700; }
.ad-step.gap .nm { color: var(--error); font-weight: 700; font-size: 18px; }
.ad-flag { position: absolute; top: -36px; left: 50%; transform: translateX(-50%); background: var(--error-container); color: #93000a; font-family: "JetBrains Mono", monospace; font-size: 11px; letter-spacing: 0.04em; text-transform: uppercase; padding: 4px 8px; border-radius: 4px; white-space: nowrap; }

.ad-list { list-style: none; padding: 0; margin: 0; }
.ad-list li { display: flex; gap: 16px; padding: 12px; border-bottom: 1px solid var(--ghost); }
.ad-list li.lead { background: var(--container); border-left: 4px solid var(--primary); border-radius: 4px; }
.ad-list a { color: inherit; text-decoration: none; }
.ad-rank { width: 28px; font-weight: 700; color: var(--secondary); }
.ad-muted { color: var(--secondary); font-size: 14px; }
.ad-quote { font-size: 16px; font-style: italic; border-left: 2px solid var(--primary-container); padding-left: 16px; margin: 12px 0; }

.ad-bar-track { height: 8px; background: #e1e3e4; border-radius: 9999px; overflow: hidden; }
.ad-bar-fill { height: 8px; background: var(--tertiary); border-radius: 9999px; }

.ad-table { width: 100%; border-collapse: collapse; font-size: 14px; }
.ad-table th { text-align: left; font-family: "JetBrains Mono", monospace; font-size: 12px; letter-spacing: 0.05em; text-transform: uppercase; color: var(--secondary); padding: 16px 24px; border-bottom: 1px solid var(--ghost); background: #fff; }
.ad-table td { padding: 16px 24px; border-bottom: 1px solid var(--ghost); }
.ad-table tr:hover td { background: #fafafa; }
.ad-table a { color: inherit; text-decoration: none; font-weight: 600; }
.ad-table a:hover { color: var(--primary); }
.ad-dot-pri { display: inline-block; width: 8px; height: 8px; border-radius: 9999px; margin-right: 8px; }

.ad-scatter { background: #fff; border: 1px solid var(--ghost); border-radius: 8px; padding: 24px; }
.ad-scatter-frame { position: relative; height: 420px; background: var(--bg); border-radius: 8px; overflow: hidden; }
.ad-q { position: absolute; font-family: "JetBrains Mono", monospace; font-size: 11px; letter-spacing: 0.04em; text-transform: uppercase; padding: 4px 8px; border-radius: 4px; }
.ad-q.ne { top: 12px; right: 12px; color: var(--primary); background: rgba(158,0,0,.08); }
.ad-q.nw { top: 12px; left: 12px; color: var(--secondary); background: var(--container); }
.ad-q.se { bottom: 40px; right: 12px; color: var(--secondary); background: var(--container); }
.ad-q.sw { bottom: 40px; left: 12px; color: #926e69; background: var(--container); }

.ad-back { color: var(--primary); text-decoration: none; font-family: "JetBrains Mono", monospace; font-size: 12px; letter-spacing: 0.05em; text-transform: uppercase; display: inline-flex; align-items: center; gap: 6px; }
.ad-score { font-size: 32px; font-weight: 700; color: var(--primary); letter-spacing: -0.02em; }
.ad-warn { background: #fff8f6; border: 1px solid var(--outline); border-radius: 8px; padding: 16px 20px; font-size: 14px; color: #5e3f3a; margin-bottom: 24px; }

.ad-feed { display: flex; gap: 24px; align-items: flex-start; }
.ad-filters { width: 256px; flex-shrink: 0; background: #fff; border: 1px solid var(--ghost); border-radius: 8px; padding: 24px; }
.ad-filters h4 { font-family: "JetBrains Mono", monospace; font-size: 12px; letter-spacing: 0.05em; text-transform: uppercase; color: var(--secondary); margin: 0 0 10px; }
.ad-chip {
  display: inline-block; padding: 4px 12px; border-radius: 9999px; border: 1px solid var(--ghost);
  font-family: "JetBrains Mono", monospace; font-size: 11px; letter-spacing: 0.04em; text-transform: uppercase;
  color: var(--secondary); text-decoration: none; margin: 0 6px 6px 0;
}
.ad-chip.on { border-color: var(--primary); color: var(--primary); background: #ffdad4; font-weight: 700; }
.ad-ev { background: #fff; border: 1px solid var(--ghost); border-radius: 8px; padding: 24px; margin-bottom: 16px; position: relative; overflow: hidden; }
.ad-ev::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; background: var(--primary); }
.ad-ev p { font-size: 18px; font-style: italic; line-height: 1.5; margin: 0 0 16px; }
.ad-meta { font-family: "JetBrains Mono", monospace; font-size: 11px; letter-spacing: 0.04em; text-transform: uppercase; color: var(--secondary); }

.ad-ask-hero { text-align: center; margin: 24px 0 8px; }
.ad-badge { display: inline-flex; align-items: center; gap: 6px; color: var(--primary); background: rgba(204,0,0,.08); border: 1px solid rgba(204,0,0,.2); padding: 4px 12px; border-radius: 9999px; font-family: "JetBrains Mono", monospace; font-size: 12px; letter-spacing: 0.04em; text-transform: uppercase; }
.ad-chips { display: flex; flex-wrap: wrap; justify-content: center; gap: 10px; margin: 8px 0 24px; }
.ad-chips a {
  padding: 8px 16px; border-radius: 9999px; border: 1px solid var(--ghost); background: #fff;
  color: var(--secondary); text-decoration: none; font-size: 14px;
}
.ad-chips a:hover { border-color: var(--primary); color: var(--on); }
.ad-answer { background: #fff; border: 1px solid var(--ghost); border-radius: 16px; padding: 32px; position: relative; overflow: hidden; }
.ad-answer::before { content: ""; position: absolute; left: 0; right: 0; top: 0; height: 4px; background: linear-gradient(90deg, #9e0000, #ffb4a8); }

@media (max-width: 1100px) {
  .ad-kpis, .ad-grid-2, .ad-grid-half, .ad-grid-4 { grid-template-columns: 1fr; }
  .ad-feed { flex-direction: column; }
  .ad-filters { width: 100%; }
}
[data-testid="stTextInput"] input, [data-testid="stTextArea"] textarea {
  border-radius: 12px !important; border: 1px solid var(--ghost) !important;
  font-family: Inter, system-ui, sans-serif !important;
}
[data-testid="stFormSubmitButton"] button, .stButton > button {
  background: var(--primary) !important; color: #fff !important; border: none !important;
  border-radius: 12px !important; font-weight: 600 !important;
}
</style>
"""


def sidenav(page: str, href, *, corpus_ok: bool) -> str:
    links = []
    for key, icon, label in NAV:
        cls = "ad-link active" if page == key or (page == "detail" and key == "map") else "ad-link"
        links.append(
            f'<a class="{cls}" href="{escape(href(page=key, theme=None, q=None), quote=True)}">'
            f'<span class="material-symbols-outlined">{icon}</span>{escape(label)}</a>'
        )
    corpus = "Corpus loaded" if corpus_ok else "Corpus missing"
    return f"""
<nav class="ad-nav">
  <div class="ad-brand">
    <h1>AJIO Discovery</h1>
    <p>Discovery Engine</p>
  </div>
  <a class="ad-nav-ask" href="{escape(href(page="ask"), quote=True)}">
    <span class="material-symbols-outlined">psychology</span> Ask the Engine
  </a>
  <div class="ad-links">{"".join(links)}</div>
  <div class="ad-foot">
    <div><span class="material-symbols-outlined">info</span> v1.0</div>
    <div><span class="material-symbols-outlined">verified_user</span> Evidence-backed</div>
    <div><span class="material-symbols-outlined">storage</span> {escape(corpus)}</div>
  </div>
</nav>
"""


def topbar(*, page: str, genuine: bool, href, source_pills: str = "") -> str:
    full_cls = "" if genuine else "on"
    gen_cls = "on" if genuine else ""
    return f"""
<header class="ad-top">
  <div class="ad-seg">
    <a class="{full_cls}" href="{escape(href(view="full"), quote=True)}">Full Corpus</a>
    <a class="{gen_cls}" href="{escape(href(view="genuine"), quote=True)}">Genuine Intent</a>
  </div>
  <div class="ad-top-right">
    {source_pills}
    <a class="ad-ask-btn" href="{escape(href(page="ask"), quote=True)}">Ask Engine</a>
  </div>
</header>
"""


def source_pills(sources: list[str], active: list[str] | None, href) -> str:
    if not sources:
        return ""
    selected = list(active) if active else list(sources)
    all_on = set(selected) >= set(sources)
    chips = []
    for source in sources:
        is_on = (not all_on) and source in selected
        nxt = None if is_on else source
        cls = "ad-pill on" if is_on else "ad-pill"
        chips.append(
            f'<a class="{cls}" href="{escape(href(sources=nxt), quote=True)}">{escape(source_display_name(source))}</a>'
        )
    return '<span class="ad-muted" style="font-size:12px;margin-right:4px">Source</span>' + "".join(chips)


def overview_html(
    *,
    tagged: int,
    analyzable: int,
    n_sources: int,
    n_areas: int,
    genuine_n: int,
    top_label: str,
    top_href: str,
    top_rows: list[dict],
    quotes: list[str],
    source_mix: list[tuple[str, int, float]],
) -> str:
    kpis = [
        ("TAGGED SAMPLE", f"{tagged:,}", False),
        ("ANALYZABLE", f"{analyzable:,}", False),
        ("OPPORTUNITY AREAS", str(n_areas), False),
        ("GENUINE-INTENT", f"{genuine_n:,}", True),
        ("TOP OPPORTUNITY", pretty_label(top_label) or "—", False),
    ]
    kpi_html = []
    for i, (lbl, val, accent) in enumerate(kpis):
        extra = " tint" if i == 4 else ""
        numcls = "num accent" if accent else "num"
        if i == 4 and top_href:
            val_html = f'<a href="{escape(top_href, quote=True)}" class="{numcls}" style="font-size:18px;text-decoration:none;color:inherit">{escape(val)}</a>'
        else:
            val_html = f'<div class="{numcls}">{escape(val)}</div>'
        kpi_html.append(
            f'<div class="ad-card{extra}"><div class="lbl">{escape(lbl)}</div>{val_html}</div>'
        )
    items = []
    for i, row in enumerate(top_rows[:5], start=1):
        lead = " lead" if i == 1 else ""
        blurb = escape(row.get("blurb") or "")
        extra = f'<p class="ad-muted" style="margin:4px 0 0">{blurb}</p>' if blurb else ""
        items.append(
            f'<li class="{lead}"><span class="ad-rank">{i:02d}</span>'
            f'<div><a href="{escape(row["href"], quote=True)}"><h4 style="margin:0;font-size:18px">{escape(row["title"])}</h4></a>'
            f"{extra}</div></li>"
        )
    quote_html = "".join(f'<p class="ad-quote">{escape(q)}</p>' for q in quotes[:3]) or (
        '<p class="ad-muted">No unflagged quotes in the appendix yet.</p>'
    )
    mix = []
    for name, _n, share in source_mix:
        mix.append(
            f'<div style="margin-bottom:16px"><div style="display:flex;justify-content:space-between;font-size:14px;margin-bottom:4px">'
            f'<span>{escape(name)}</span><span class="ad-muted">{share:.0%}</span></div>'
            f'<div class="ad-bar-track"><div class="ad-bar-fill" style="width:{100*share:.1f}%"></div></div></div>'
        )
    steps = [
        ("1", "Discover", False),
        ("2", "Like", False),
        ("3", "Wishlist", False),
        ("4", "Evaluate", True),
        ("5", "Decide", False),
        ("6", "Purchase", False),
    ]
    funnel = []
    for num, name, gap in steps:
        flag = '<div class="ad-flag">Friction gap</div>' if gap else ""
        funnel.append(
            f'<div class="ad-step{" gap" if gap else ""}">{flag}<div class="ad-dot">{num}</div>'
            f'<div class="nm ad-muted">{escape(name)}</div></div>'
        )
    return f"""
<div class="ad-page">
  <h2 class="ad-display">AJIO Discovery Engine</h2>
  <p class="ad-sub">Discover what prevents wishlist intent from becoming purchase intent.</p>
  <div class="ad-banner">
    <div>
      <h3>Business objective</h3>
      <p>Increase 30-day wishlist-to-purchase conversion.</p>
    </div>
    <div class="ad-icon-round"><span class="material-symbols-outlined" style="font-size:28px">trending_up</span></div>
  </div>
  <p class="ad-muted" style="margin:-8px 0 20px">Read-only over frozen Stage 4/5 outputs. Prevalence is over the {tagged:,}-document tagged sample, not the {analyzable:,} analyzable corpus. Placeholder mock numbers from the design files are not shown.</p>
  <div class="ad-kpis">{"".join(kpi_html)}</div>
  <div class="ad-grid-2">
    <div class="ad-card ad-pad">
      <h3 class="ad-h">Discovery funnel</h3>
      <div class="ad-funnel">{"".join(funnel)}</div>
      <p class="ad-muted">The engine scores friction at evaluate — size, returns, delivery, quality — not the earlier browse steps.</p>
    </div>
    <div class="ad-card ad-pad">
      <h3 class="ad-h">Top opportunity areas</h3>
      <ul class="ad-list">{"".join(items)}</ul>
    </div>
  </div>
  <div class="ad-grid-half">
    <div class="ad-card ad-pad" style="background:#e1e3e4">
      <div class="ad-kicker" style="margin-bottom:12px">What the engine is telling us</div>
      {quote_html}
    </div>
    <div class="ad-card ad-pad">
      <h3 class="ad-h">Source mix</h3>
      <p class="ad-muted" style="margin-top:-8px;margin-bottom:16px">Share of the tagged sample, not a placeholder mix.</p>
      {"".join(mix) or '<p class="ad-muted">No tagged documents loaded.</p>'}
    </div>
  </div>
</div>
"""


def scatter_svg(points: list[dict]) -> str:
    """x = evidence volume (prevalence 0–1), y = genuine-intent share (0–1)."""
    w, h = 840, 400
    pl, pr, pt, pb = 56, 16, 16, 44
    iw, ih = w - pl - pr, h - pt - pb

    def xy(x, y):
        px = pl + max(0.0, min(1.0, x)) * iw
        py = pt + (1.0 - max(0.0, min(1.0, y))) * ih
        return px, py

    circles = []
    for pt_ in points:
        px, py = xy(pt_["x"], pt_["y"])
        r = pt_["r"]
        title = escape(pt_["title"])
        href = escape(pt_["href"], quote=True)
        circles.append(
            f'<a href="{href}"><circle cx="{px:.1f}" cy="{py:.1f}" r="{r:.1f}" '
            f'fill="rgba(158,0,0,0.55)" stroke="#9e0000" stroke-width="1">'
            f'<title>{title}</title></circle></a>'
        )
    x0, y0 = xy(0, 0)
    x1, y1 = xy(1, 1)
    xm, ym = xy(0.5, 0.5)
    return f"""
<svg viewBox="0 0 {w} {h}" width="100%" height="400" role="img" aria-label="Opportunity scatter">
  <line x1="{xm:.1f}" y1="{y1:.1f}" x2="{xm:.1f}" y2="{y0:.1f}" stroke="#c8c6c5" stroke-dasharray="5 5"/>
  <line x1="{x0:.1f}" y1="{ym:.1f}" x2="{x1:.1f}" y2="{ym:.1f}" stroke="#c8c6c5" stroke-dasharray="5 5"/>
  {"".join(circles)}
  <text x="{w/2}" y="{h-8}" text-anchor="middle" fill="#5f5e5e" font-size="12" font-family="Inter">Evidence volume (prevalence)</text>
  <text x="14" y="{h/2}" fill="#5f5e5e" font-size="12" font-family="Inter" transform="rotate(-90 14 {h/2})">Genuine-intent share</text>
</svg>
"""


def map_html(*, table_rows: list[dict], points: list[dict]) -> str:
    body = []
    for row in table_rows:
        color = {"Critical": "#9e0000", "High": "#5c647b", "Medium": "#926e69", "Watch": "#c8c6c5"}.get(
            row["priority"], "#c8c6c5"
        )
        body.append(
            f'<tr><td><a href="{escape(row["href"], quote=True)}">{escape(row["title"])}</a>'
            f'<div class="ad-muted">{escape(row["dimension"])}</div></td>'
            f'<td><span style="color:var(--primary);font-weight:700">{escape(row["score"])}</span></td>'
            f'<td class="ad-muted">{escape(row["evidence"])}</td>'
            f'<td>{escape(row["genuine"])}</td>'
            f'<td class="ad-muted">{escape(row["sources"])}</td>'
            f'<td><span class="ad-dot-pri" style="background:{color}"></span>{escape(row["priority"])}</td></tr>'
        )
    return f"""
<div class="ad-page">
  <h2 class="ad-display">Opportunity Map</h2>
  <p class="ad-sub">Where user friction is strongest — ranked from Stage 4, not a mock index.</p>
  <div class="ad-scatter" style="margin:24px 0">
    <div style="display:flex;justify-content:space-between;border-bottom:1px solid var(--ghost);padding-bottom:12px;margin-bottom:12px">
      <span class="ad-kicker" style="color:var(--tertiary-container)">Friction vs genuine intent</span>
      <span class="material-symbols-outlined" style="color:var(--primary)">scatter_plot</span>
    </div>
    <div class="ad-scatter-frame">
      <div class="ad-q ne">Prioritize</div>
      <div class="ad-q nw">High intent</div>
      <div class="ad-q se">Broad friction</div>
      <div class="ad-q sw">Lower priority</div>
      {scatter_svg(points)}
    </div>
  </div>
  <div class="ad-card" style="padding:0;overflow:hidden">
    <div style="padding:20px 24px;border-bottom:1px solid var(--ghost)" class="ad-kicker">Ranked opportunities</div>
    <table class="ad-table">
      <thead><tr><th>Opportunity</th><th>Score</th><th>Evidence</th><th>Genuine intent</th><th>Sources</th><th>Priority</th></tr></thead>
      <tbody>{"".join(body)}</tbody>
    </table>
  </div>
</div>
"""


def detail_html(ctx: dict) -> str:
    cards = "".join(
        f'<div class="ad-card ad-pad"><span class="lbl">{escape(c["lbl"])}</span>'
        f'<div class="num{" accent" if c.get("accent") else ""}">{escape(c["val"])}</div>'
        f'<div class="ad-muted" style="margin-top:8px">{escape(c["hint"])}</div></div>'
        for c in ctx["cards"]
    )
    quotes = "".join(
        f'<div class="ad-ev"><div class="ad-meta" style="margin-bottom:8px">{escape(q["meta"])}</div>'
        f'<p>“{escape(q["text"])}”</p></div>'
        for q in ctx["quotes"]
    ) or '<p class="ad-muted">No unflagged quote for this theme.</p>'
    return f"""
<div class="ad-page">
  <a class="ad-back" href="{escape(ctx["back"], quote=True)}"><span class="material-symbols-outlined">arrow_back</span> Back to map</a>
  <div style="display:flex;justify-content:space-between;align-items:flex-end;margin-top:12px;gap:24px">
    <h2 class="ad-display">{escape(ctx["title"])}</h2>
    <div style="text-align:right">
      <div class="ad-kicker" style="color:var(--secondary)">Opportunity score</div>
      <div class="ad-score">{escape(ctx["score"])}</div>
    </div>
  </div>
  <div class="ad-card ad-pad" style="margin:24px 0">
    <div class="ad-kicker" style="margin-bottom:12px">Executive summary</div>
    <p style="margin:0;font-size:18px;color:#5e3f3a;max-width:52rem">{escape(ctx["summary"])}</p>
  </div>
  <div class="ad-grid-4">{cards}</div>
  <h3 class="ad-h">Evidence</h3>
  {quotes}
</div>
"""


def evidence_html(
    *,
    chips_source: str,
    chips_intent: str,
    chips_dim: str,
    cards: list[dict],
    n_shown: int,
    n_total: int,
) -> str:
    feed = []
    for card in cards:
        feed.append(
            f'<div class="ad-ev"><div style="display:flex;justify-content:space-between;margin-bottom:12px">'
            f'<div><span class="ad-chip on">{escape(card["tag"])}</span> '
            f'<span class="ad-meta">{escape(card["source"])} · {escape(card["doc_id"])}</span></div>'
            f'<span class="ad-meta">{escape(card.get("when") or "")}</span></div>'
            f'<p>“{escape(card["text"])}”</p>'
            f'<div class="ad-meta">{escape(card["intent"])} · {escape(card["theme"])}</div></div>'
        )
    empty = '<p class="ad-muted">No documents match these filters.</p>' if not feed else ""
    return f"""
<div class="ad-page">
  <h2 class="ad-display">Evidence Explorer</h2>
  <p class="ad-sub">Trace every opportunity back to conversations. Bodies are PII-redacted.</p>
  <p class="ad-muted">{n_shown} of {n_total} tagged documents in this filter.</p>
  <div class="ad-feed" style="margin-top:24px">
    <aside class="ad-filters">
      <div class="ad-kicker" style="margin-bottom:16px">Filters</div>
      <h4>Source</h4><div style="margin-bottom:20px">{chips_source}</div>
      <h4>Intent</h4><div style="margin-bottom:20px">{chips_intent}</div>
      <h4>Dimension</h4><div>{chips_dim}</div>
    </aside>
    <div style="flex:1">{"".join(feed) or empty}</div>
  </div>
</div>
"""


def segments_html(rows: list[dict]) -> str:
    body = "".join(
        f'<tr><td>{escape(r["segment"])}</td><td>{escape(r["blocker"])}</td>'
        f'<td>{escape(r["n"])}</td><td>{escape(r["lift"])}</td></tr>'
        for r in rows
    )
    note = (
        "Lift is versus the tagged-set base rate for that blocker. "
        "n_docs = 1 is directional — not a segment claim."
    )
    return f"""
<div class="ad-page">
  <h2 class="ad-display">Segments</h2>
  <p class="ad-sub">Segment × blocker cells that cleared lift ≥ 2.</p>
  <div class="ad-card" style="margin-top:24px;padding:0;overflow:hidden">
    <table class="ad-table">
      <thead><tr><th>Segment</th><th>Blocker</th><th>Documents</th><th>Lift</th></tr></thead>
      <tbody>{body or '<tr><td colspan="4" class="ad-muted">No segment cells in this snapshot.</td></tr>'}</tbody>
    </table>
  </div>
  <p class="ad-muted" style="margin-top:16px">{escape(note)}</p>
</div>
"""


def ajio_html(ctx: dict, rows: list[dict]) -> str:
    body = "".join(
        f'<tr><td>{escape(r["label"])}</td><td>{escape(r["kind"])}</td>'
        f'<td>{"Yes" if r["corroborates"] else "No"}</td>'
        f'<td class="ad-muted">{escape(r["detail"])}</td></tr>'
        for r in rows
    )
    return f"""
<div class="ad-page">
  <h2 class="ad-display">AJIO Corroboration</h2>
  <p class="ad-sub">On-site aggregates beside the text themes. They are not documents.</p>
  <div class="ad-warn">{escape(ctx["provenance"])}</div>
  <div class="ad-grid-4">
    <div class="ad-card ad-pad"><div class="lbl">Products</div><div class="num">{escape(str(ctx["products"]))}</div></div>
    <div class="ad-card ad-pad"><div class="lbl">Mean misfit %</div><div class="num">{escape(ctx["misfit"])}</div></div>
    <div class="ad-card ad-pad"><div class="lbl">Bad + Very Bad %</div><div class="num">{escape(ctx["quality"])}</div></div>
    <div class="ad-card ad-pad"><div class="lbl">Mean rating</div><div class="num">{escape(ctx["rating"])}</div></div>
  </div>
  <p class="ad-muted">{escape(ctx["caption"])}</p>
  <div class="ad-card" style="margin-top:24px;padding:0;overflow:hidden">
    <div style="padding:20px 24px" class="ad-kicker">Cross-reference against text themes</div>
    <table class="ad-table">
      <thead><tr><th>Theme</th><th>Kind</th><th>Corroborates</th><th>Detail</th></tr></thead>
      <tbody>{body}</tbody>
    </table>
  </div>
</div>
"""


def ask_intro_html(chips: list[tuple[str, str]]) -> str:
    chip_html = "".join(
        f'<a href="{escape(url, quote=True)}"><span class="material-symbols-outlined" style="font-size:14px;color:var(--primary)">auto_awesome</span> {escape(label)}</a>'
        for label, url in chips
    )
    return f"""
<div class="ad-page">
  <div class="ad-ask-hero">
    <div class="ad-badge"><span class="material-symbols-outlined" style="font-size:14px">psychology</span> Grounded in evidence corpus</div>
    <h2 class="ad-display" style="margin-top:16px">Ask the Discovery Engine.</h2>
    <p class="ad-sub">One Groq call over opportunity_scores.csv and the appendix quotes. Not a re-run.</p>
  </div>
  <div class="ad-chips">{chip_html}</div>
</div>
"""


def ask_answer_html(title: str, body_html: str, quotes: list[dict], related: list[dict]) -> str:
    ev = "".join(
        f'<div class="ad-ev"><div class="ad-meta" style="margin-bottom:8px">{escape(q["meta"])}</div>'
        f'<p style="font-size:14px;font-style:italic">“{escape(q["text"])}”</p></div>'
        for q in quotes
    )
    rel = "".join(
        f'<a href="{escape(r["href"], quote=True)}" style="display:block;padding:12px 0;border-top:1px solid var(--ghost);color:inherit;text-decoration:none">'
        f'<strong>{escape(r["title"])}</strong><div class="ad-muted">{escape(r["hint"])}</div></a>'
        for r in related
    )
    return f"""
<div class="ad-answer" style="margin-top:16px">
  <h3 style="margin:0 0 12px;font-size:18px">{escape(title)}</h3>
  <div class="ad-muted" style="color:var(--on);font-size:16px;line-height:1.6">{body_html}</div>
</div>
<div class="ad-grid-2" style="margin-top:24px">
  <div>
    <div class="ad-kicker" style="margin-bottom:12px">Evidence supporting this answer</div>
    {ev or '<p class="ad-muted">Appendix quotes for the top themes.</p>'}
  </div>
  <div class="ad-card ad-pad">
    <div class="ad-kicker" style="margin-bottom:12px">Related opportunities</div>
    {rel}
  </div>
</div>
"""


def missing_html(processed: str, outputs: str) -> str:
    return f"""
<div class="ad-page">
  <h2 class="ad-display">No scores on disk</h2>
  <p class="ad-sub">This explorer does not quantify. Run the pipeline, then reload.</p>
  <pre style="background:#fff;border:1px solid var(--ghost);padding:16px;border-radius:8px">.venv\\Scripts\\python.exe -m src.quantify.run_quantification
.venv\\Scripts\\python.exe -m src.synthesize.run_synthesis --force</pre>
  <p class="ad-muted">Looked in {escape(processed)} and {escape(outputs)}.</p>
</div>
"""
