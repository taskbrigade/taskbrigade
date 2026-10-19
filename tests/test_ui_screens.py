"""Screenshot and layout checks of the main pages at phone, tablet and desktop width (Playwright).

Each test seeds the same demo data, serves the app with uvicorn on a free local port and opens every page of `PAGES`
at one width of `VIEWPORTS`. Per page it checks the mobile rules of the UI redesign (no horizontal page scroll, tap
targets >= 44 px and inputs >= 16 px on the phone, labels, contrast >= 4.5:1, ARIA state on toggles, visible focus)
and compares a full-page screenshot with the baseline in `tests/screenshots/` (canvas pixel diff in the browser, so
no image library is needed).

    uv run playwright install --with-deps chromium     # once (CI does this in the test job)
    TBR_UPDATE_SCREENSHOTS=1 uv run pytest tests/test_ui_screens.py -m db   # write new baselines

`TBR_PLAYWRIGHT_WS=ws://127.0.0.1:<port>/` connects to a browser served by `playwright run-server` instead (e.g. the
Playwright Docker image with `--network host` on a host whose Chromium misses system libraries). Without a browser
the tests are skipped; a failing comparison writes `<name>.actual.png` and `<name>.diff.png` to
`TBR_SCREENSHOT_OUT` (default: a temporary directory, printed in the failure).
"""
import base64
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from tests.test_sprints_ui import mksprint
from tests.test_ui_cli import ui  # noqa: F401 (Fixture)
from tests.test_workflow import SHA, claim, env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)

pytestmark = pytest.mark.db

BASELINES = Path(__file__).parent / "screenshots"
UPDATE = os.environ.get("TBR_UPDATE_SCREENSHOTS") == "1"
# Share of pixels that may differ (anti-aliasing, caret); a layout change moves far more.
MAX_DIFF_RATIO = 0.002
MAX_HEIGHT = 2400  # Long pages are compared down to here.

VIEWPORTS = {"phone": (390, 844), "tablet": (768, 1024), "desktop": (1440, 900)}
# Narrowest phone: only the layout checks run (no screenshot).
NARROW = (360, 740)

# name → path; `None` paths are filled in by `_seed`. Login and error page are opened without a session.
PAGES = {
    "inbox": "/",
    "backlog": "/backlog",
    "kanban": "/kanban",
    "planning": "/planning",
    "projects": "/projects",
    "projects-cards": "/projects?view=cards",
    "project-detail": "/projects/KET",
    "task-detail": "/tasks/KET-0001",
    "task-new": "/tasks/new",
    "project-new": "/projects/new",
    "workflows": "/workflows",
    "profile": "/profile",
    "admin-users": "/admin/users?all_agents=1",  # Seeded agents have no owner (hidden by default).
    "admin-settings": "/admin/settings",
    "admin-database": "/admin/database",
    "error": "/tasks/KET-9999",
}
PUBLIC_PAGES = {"login": "/login"}

# Elements whose content changes from run to run (clock times, relative ages, versions) are masked.
MASK = ["time", "[data-screens-mask]", ".reltime", "#db-status code"]

# Full-page shots: the fixed bottom tab bar goes to the end of the page instead of over the middle of it.
SHOT_CSS = "html { position: relative; } .bottom-tabs { position: absolute !important; }"
# The viewport resize of a full-page shot moves a snap-scrolling board (kanban on phones) to a varying offset: shots
# show it scrolled back to its first column. test_ui_mobile_flows checks the real start column (first or `?col=`).
UNSCROLL_JS = """async () => { for (const el of document.querySelectorAll(".kanban-scroll")) {
  el.style.scrollSnapType = "none"; el.scrollLeft = 0; }
  await new Promise(ok => requestAnimationFrame(() => requestAnimationFrame(ok))); }"""

# Before a screenshot: dates, clock times and short relative ages become fixed text, so a baseline stays valid on
# another day (the seed runs at test time). The checks above run on the real text.
NORMALIZE_JS = r"""
() => {
  const rules = [[/\b\d{4}-\d{2}-\d{2}\b/g, "2000-01-01"], [/\b\d{1,2}\.\d{1,2}\.\d{2,4}\b/g, "01.01.2000"],
    [/\b\d{1,2}:\d{2}(:\d{2})?\b/g, "00:00"],
    [/\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2}\b/g, "Jan 1"],
    [/\b\d+ (s|sec|min|h|seconds?|minutes?|hours?|Sek\.?|Min\.?|Std\.?)( ago)?\b/g, "1 min ago"]];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const n = walker.currentNode;
    let t = n.textContent;
    for (const [re, to] of rules) t = t.replace(re, to);
    if (t !== n.textContent) n.textContent = t;
  }
}
"""

# --- Checks in the page ----------------------------------------------------------------------------------------

CHECKS_JS = r"""
({phone}) => {
  const problems = [];
  const vw = document.documentElement.clientWidth;
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (el.closest("details:not([open]) > :not(summary)")) return false;  // folded content keeps its box in Chromium
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && !el.closest("[hidden], [inert]");
  };
  const name = el => {
    let n = el.tagName.toLowerCase();
    if (el.id) n += "#" + el.id;
    const cls = [...el.classList].slice(0, 2).join(".");
    if (cls) n += "." + cls;
    const t = (el.getAttribute("aria-label") || el.textContent || el.getAttribute("name") || "").trim().replace(/\s+/g, " ");
    return n + (t ? ` "${t.slice(0, 30)}"` : "");
  };
  // Inside a container that scrolls or clips sideways (tables, kanban board): wide content is intended there.
  const clipped = el => {
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === "auto" || ox === "scroll" || ox === "hidden" || ox === "clip") return true;
    }
    return false;
  };

  // 1. viewport meta
  const meta = document.querySelector('meta[name=viewport]');
  if (!meta || !/width=device-width/.test(meta.content) || !/initial-scale=1/.test(meta.content))
    problems.push("viewport meta missing or wrong");

  // 2. horizontal page scroll
  if (document.documentElement.scrollWidth > vw + 1) {
    const wide = [...document.querySelectorAll("body *")].filter(el => visible(el) && !clipped(el)
      && el.getBoundingClientRect().right > vw + 1).slice(-3).map(name);
    problems.push(`horizontal scroll: page ${document.documentElement.scrollWidth}px > ${vw}px (${wide.join(", ")})`);
  }

  const interactive = [...document.querySelectorAll(
    'a[href], button, select, textarea, summary, input:not([type=hidden])')].filter(visible);

  // 3. labels: every form control needs an accessible name.
  for (const el of document.querySelectorAll("input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea")) {
    if (el.closest("[hidden]")) continue;
    const named = (el.labels && el.labels.length) || el.getAttribute("aria-label") || el.getAttribute("aria-labelledby")
      || el.getAttribute("title");
    if (!named) problems.push(`no label: ${name(el)}`);
  }

  // 4. ARIA state: disclosure buttons carry aria-expanded, in-page segment buttons aria-pressed.
  for (const el of document.querySelectorAll("button[aria-controls], [data-toggle]")) {
    if (!el.hasAttribute("aria-expanded") && !el.hasAttribute("aria-selected")) problems.push(`no aria-expanded: ${name(el)}`);
  }
  for (const el of document.querySelectorAll("[data-segmented] > button")) {
    if (!el.hasAttribute("aria-pressed")) problems.push(`no aria-pressed: ${name(el)}`);
  }

  if (phone) {
    // 5. tap targets. Controls (buttons, fields, segments, chips, switches, summaries) are >= 44×44 px. Links and
    // checkboxes may be smaller when their 44 px target area does not overlap another target (Lighthouse rule);
    // links inside running text are exempt (WCAG 2.5.8).
    const targetOf = el => el.closest("label.ui-chip, label.ui-switch") || (el.matches("input[type=checkbox], input[type=radio]") && el.closest("label")) || el;
    const targets = [...new Set(interactive.map(targetOf))];
    const rects = new Map(targets.map(t => [t, t.getBoundingClientRect()]));
    // Fixed bars (bottom tabs, sheets) lie on their own layer; covering content is check 8.
    const layer = el => { for (let p = el; p; p = p.parentElement) if (getComputedStyle(p).position === "fixed") return p;
      return null; };
    for (const t of targets) {
      const r = rects.get(t);
      if (r.height >= 43.5 && r.width >= 43.5) continue;
      const soft = t.tagName === "A" && !t.matches(".ui-btn, [role=button], .ui-seg > a, .ui-tabs > a")
        || t.matches("input[type=checkbox], input[type=radio], label:not(.ui-chip):not(.ui-switch)");
      if (soft && t.tagName === "A" && t.closest("p, small, .banner, .md, .prose, .ui-empty, .field-help")) continue;
      if (soft) {
        const w = Math.max(r.width, 44), h = Math.max(r.height, 44);
        const x = r.left + r.width / 2 - w / 2, y = r.top + r.height / 2 - h / 2;
        const hit = targets.find(o => o !== t && !o.contains(t) && !t.contains(o) && layer(o) === layer(t) && (() => {
          const q = rects.get(o);
          return q.left < x + w - .5 && q.right > x + .5 && q.top < y + h - .5 && q.bottom > y + .5; })());
        if (!hit) continue;
        problems.push(`tap target ${Math.round(r.width)}×${Math.round(r.height)} next to ${name(hit)}: ${name(t)}`);
      } else {
        problems.push(`tap target ${Math.round(r.width)}×${Math.round(r.height)}: ${name(t)}`);
      }
    }
    // 6. inputs >= 16 px, otherwise iOS zooms in on focus.
    for (const el of document.querySelectorAll("input:not([type=hidden]):not([type=checkbox]):not([type=radio]), select, textarea")) {
      if (!visible(el)) continue;
      const fs = parseFloat(getComputedStyle(el).fontSize);
      if (fs < 16) problems.push(`input font ${fs}px: ${name(el)}`);
    }
  }

  // 8. fixed bottom bar: at the end of the page the last content sits above it.
  const tabs = document.querySelector(".bottom-tabs");
  if (tabs && visible(tabs) && getComputedStyle(tabs).position === "fixed") {
    window.scrollTo(0, document.documentElement.scrollHeight);
    const top = tabs.getBoundingClientRect().top;
    const last = Math.max(...[...document.querySelectorAll("main *")].filter(visible)
      .map(el => el.getBoundingClientRect().bottom));
    if (last > top + 1) problems.push(`bottom tab bar covers ${Math.round(last - top)}px of content`);
    window.scrollTo(0, 0);
  }

  // 7. contrast of text >= 4.5:1 (3:1 for large text) against the nearest opaque background.
  const rgb = c => { const m = c.match(/[\d.]+/g); return m ? m.map(Number) : [0, 0, 0, 0]; };
  const lum = ([r, g, b]) => { const f = v => (v /= 255) <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;
    return .2126 * f(r) + .7152 * f(g) + .0722 * f(b); };
  const blend = (fg, bg) => { const a = fg[3] === undefined ? 1 : fg[3];
    return [0, 1, 2].map(i => fg[i] * a + bg[i] * (1 - a)); };
  const background = el => {
    const layers = [];
    for (let p = el; p; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (s.backgroundImage !== "none" && !s.backgroundImage.startsWith("url(")) return null;  // gradient: skip
      const c = rgb(s.backgroundColor);
      if ((c[3] === undefined ? 1 : c[3]) > 0) { layers.push(c); if ((c[3] === undefined ? 1 : c[3]) >= 1) break; }
    }
    let bg = [255, 255, 255];
    for (const c of layers.reverse()) bg = blend(c, bg);
    return bg;
  };
  const seen = new Set();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode, el = node.parentElement;
    if (!node.textContent.trim() || !el || seen.has(el) || !visible(el)) continue;
    seen.add(el);
    if (el.closest("[disabled], :disabled, [aria-disabled=true], option, .ui-visually-hidden, .brand, svg")) continue;
    let opacity = 1;
    for (let p = el; p; p = p.parentElement) opacity *= parseFloat(getComputedStyle(p).opacity);
    if (opacity < 1) continue;  // dimmed on purpose (closed sprints, inactive rows)
    const s = getComputedStyle(el), bg = background(el);
    if (!bg) continue;
    const fg = blend(rgb(s.color), bg);
    const [l1, l2] = [lum(fg), lum(bg)].sort((a, b) => b - a);
    const ratio = (l1 + .05) / (l2 + .05);
    const size = parseFloat(s.fontSize), bold = parseInt(s.fontWeight) >= 700;
    const need = size >= 24 || (bold && size >= 18.66) ? 3 : 4.5;
    if (ratio < need - .01) problems.push(`contrast ${ratio.toFixed(2)}: ${name(el)}`);
  }
  return [...new Set(problems)];
}
"""

# Visible focus: the focused element (or the label/chip it sits in) shows an outline or box shadow.
# Shared page canvas (UI 15): grey body, one centred container (max. 1400 px, 24 px padding, 16 px below 640 px),
# no grey wrapper box, and the page title starts at the container's content edge on every page.
CANVAS_JS = r"""
() => {
  const problems = [];
  const bg = getComputedStyle(document.body).backgroundColor;
  if (bg !== "rgb(244, 245, 247)") problems.push(`body background ${bg}, expected --ui-bg`);
  const main = document.querySelector("body > main.app-main");
  if (!main) return [...problems, "no main.app-main container"];
  const s = getComputedStyle(main), r = main.getBoundingClientRect();
  const pad = innerWidth < 640 ? 16 : 24;
  if (parseFloat(s.paddingLeft) !== pad || parseFloat(s.paddingRight) !== pad)
    problems.push(`main padding ${s.paddingLeft}/${s.paddingRight}, expected ${pad}px`);
  if (r.width > 1400.5) problems.push(`main is ${r.width}px wide, max. 1400px`);
  for (const el of main.querySelectorAll(".ui-page")) {
    const b = getComputedStyle(el).backgroundColor;
    if (b !== "rgba(0, 0, 0, 0)") problems.push(`wrapper box .${[...el.classList].join(".")} has background ${b}`);
  }
  const title = main.querySelector("h1, .ui-page-title");
  if (title && !title.closest(".ui-auth")) {
    const left = title.getBoundingClientRect().left, edge = r.left + pad;
    if (Math.abs(left - edge) > 1) problems.push(`page title starts at ${left}px, container content edge ${edge}px`);
  }
  return problems;
}
"""

FOCUS_JS = r"""
() => {
  const el = document.activeElement;
  // Chromium keeps a date input as activeElement while focus sits in its internal fields, where :focus does not match.
  if (!el || el === document.body || !el.matches(":focus")) return null;
  const has = e => { const s = getComputedStyle(e);
    return (s.outlineStyle !== "none" && parseFloat(s.outlineWidth) > 0) || s.boxShadow !== "none"; };
  const host = el.closest("label") || el.parentElement;
  const sibling = el.nextElementSibling;
  if (has(el) || (host && has(host)) || (sibling && has(sibling))) return null;
  return el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") + " " + (el.textContent || el.name || "").trim().slice(0, 30);
}
"""

# Pixel diff of two PNGs (data URLs) in a canvas; returns [differing pixels, total, diff PNG data URL].
DIFF_JS = r"""
async ([a, b]) => {
  const load = src => new Promise((ok, err) => { const i = new Image(); i.onload = () => ok(i); i.onerror = err; i.src = src; });
  const [ia, ib] = await Promise.all([load(a), load(b)]);
  const w = Math.max(ia.width, ib.width), h = Math.max(ia.height, ib.height);
  const pix = img => { const c = new OffscreenCanvas(w, h), x = c.getContext("2d"); x.drawImage(img, 0, 0);
    return x.getImageData(0, 0, w, h).data; };
  const pa = pix(ia), pb = pix(ib);
  const out = new OffscreenCanvas(w, h), ox = out.getContext("2d"), od = ox.createImageData(w, h);
  let diff = 0;
  for (let i = 0; i < pa.length; i += 4) {
    const d = Math.abs(pa[i] - pb[i]) + Math.abs(pa[i + 1] - pb[i + 1]) + Math.abs(pa[i + 2] - pb[i + 2]);
    if (d > 48) { diff++; od.data[i] = 220; od.data[i + 3] = 255; }
    else { od.data[i] = od.data[i + 1] = od.data[i + 2] = 255; od.data[i + 3] = 40; }
  }
  ox.putImageData(od, 0, 0);
  const blob = await out.convertToBlob();
  const url = await new Promise(ok => { const r = new FileReader(); r.onload = () => ok(r.result); r.readAsDataURL(blob); });
  return [diff, w * h, url, ia.width === ib.width && ia.height === ib.height];
}
"""


# --- Fixtures --------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    try:
        ws = os.environ.get("TBR_PLAYWRIGHT_WS")
        b = pw.chromium.connect(ws) if ws else pw.chromium.launch()
    except Exception as exc:  # no browser installed, missing system libraries …
        pw.stop()
        pytest.skip(f"no Playwright Chromium: {str(exc).splitlines()[0]}")
    yield b
    b.close()
    pw.stop()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(env):  # noqa: F811
    import uvicorn

    from app.main import app

    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)


def _seed(env, ui):  # noqa: F811
    """Neutral demo data: one project, a sprint, tasks in several states, a question and a review."""
    mkproject(env, description="Demo project for the screenshot tests")
    sprint = mksprint(ui, name="Sprint 1")
    nr = mktask(env, title="Build the login page", assigned_to="claude-code", prio=2)["task_nr"]  # KET-0001
    mktask(env, title="Write the API guide", assigned_to="hermes", prio=3)
    mktask(env, title="Fix the mobile header", assigned_to="codex", prio=1)
    mktask(env, title="Draft: dark mode", status="draft", prio=4)
    mktask(env, title="Release notes", prio=5)
    t = claim(env).json()
    env["c"].post(f"/api/v1/tasks/{nr}/ask", headers=env["claude"], json={"body": "Password or passkey?"})
    env["c"].post(f"/api/v1/tasks/{nr}/answer", headers=env["admin"], json={"body": "Password first."})
    claim(env, task_nr=nr)
    env["gitea"].push_branch("taskbrigade", "ket", t["branch_name"])
    env["c"].post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"],
                  json={"branch_name": t["branch_name"], "commit_sha": SHA, "summary": "Login page done"})
    claim(env, who="codex")
    env["c"].post("/api/v1/tasks/KET-0003/ask", headers=env["codex"], json={"body": "Which breakpoint?"})
    env["c"].patch("/api/v1/tasks/KET-0002", headers=env["admin"], json={"sprint": sprint})


def _context(browser, base, size, phone):
    w, h = size
    ctx = browser.new_context(viewport={"width": w, "height": h}, is_mobile=phone, has_touch=phone,
                              device_scale_factor=1, locale="en-US", timezone_id="UTC", reduced_motion="reduce")
    ctx.add_cookies([{"name": "tm_lang", "value": "en", "url": base}])  # English UI (the tests default to German)
    return ctx


def _login(page, base):
    from tests.conftest import BOOTSTRAP_PW

    page.goto(base + "/login")
    page.fill("input[name=username]", "admin")
    page.fill("input[name=password]", BOOTSTRAP_PW)
    page.click("form[action='/login'] button[type=submit]")
    page.wait_for_url(base + "/")


def _settle(page):
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready")
    page.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; "
                               "caret-color: transparent !important; }")


def _focus_problems(page, steps=25) -> list[str]:
    page.mouse.click(1, 1)
    out = []
    for _ in range(steps):
        page.keyboard.press("Tab")
        bad = page.evaluate(FOCUS_JS)
        if bad:
            out.append(f"focus not visible: {bad}")
    return sorted(set(out))


def _compare(page, png: bytes, name: str, out: Path) -> str | None:
    base = BASELINES / f"{name}.png"
    if UPDATE or not base.exists():
        BASELINES.mkdir(exist_ok=True)
        base.write_bytes(png)
        return None if UPDATE else f"{name}: new baseline written – check and commit {base.relative_to(Path.cwd())}"
    url = lambda b: "data:image/png;base64," + base64.b64encode(b).decode()  # noqa: E731
    diff, total, diff_png, same_size = page.evaluate(DIFF_JS, [url(base.read_bytes()), url(png)])
    if same_size and diff <= total * MAX_DIFF_RATIO:
        return None
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.actual.png").write_bytes(png)
    (out / f"{name}.diff.png").write_bytes(base64.b64decode(diff_png.split(",", 1)[1]))
    return f"{name}: {diff} of {total} pixels differ (size equal: {same_size}) – see {out}"


def _shoot(page) -> bytes:
    page.evaluate(UNSCROLL_JS)
    page.evaluate(NORMALIZE_JS)
    height = min(page.evaluate("document.documentElement.scrollHeight"), MAX_HEIGHT)
    width = page.viewport_size["width"]
    return page.screenshot(clip={"x": 0, "y": 0, "width": width, "height": height}, full_page=True,
                           animations="disabled", caret="hide", mask=[page.locator(s) for s in MASK],
                           mask_color="#C9CFD8", style=SHOT_CSS)


def _run(browser, base, size, phone, label, shots=True, focus=False) -> list[str]:
    out = Path(os.environ.get("TBR_SCREENSHOT_OUT") or tempfile.mkdtemp(prefix="tbr-screens-"))
    problems = []
    ctx = _context(browser, base, size, phone)
    try:
        page = ctx.new_page()
        for name, path in PUBLIC_PAGES.items():
            page.goto(base + path)
            _settle(page)
            problems += [f"{name} @{size[0]}: {p}" for p in page.evaluate(CHECKS_JS, {"phone": phone})]
            problems += [f"{name} @{size[0]}: {p}" for p in page.evaluate(CANVAS_JS)]
            if shots and (msg := _compare(page, _shoot(page), f"{name}-{label}", out)):
                problems.append(msg)
        _login(page, base)
        for name, path in PAGES.items():
            page.goto(base + path)
            _settle(page)
            problems += [f"{name} @{size[0]}: {p}" for p in page.evaluate(CHECKS_JS, {"phone": phone})]
            problems += [f"{name} @{size[0]}: {p}" for p in page.evaluate(CANVAS_JS)]
            if shots and (msg := _compare(page, _shoot(page), f"{name}-{label}", out)):
                problems.append(msg)
            if focus:
                problems += [f"{name} @{size[0]}: {p}" for p in _focus_problems(page)]
    finally:
        ctx.close()
    return problems


@pytest.mark.parametrize("label", list(VIEWPORTS))
def test_pages_at_viewport(browser, env, ui, server, label):  # noqa: F811
    _seed(env, ui)
    size = VIEWPORTS[label]
    problems = _run(browser, server, size, label == "phone", label, focus=label == "desktop")
    assert not problems, "\n".join(problems)


def test_pages_at_360px_have_no_horizontal_scroll(browser, env, ui, server):  # noqa: F811
    _seed(env, ui)
    problems = _run(browser, server, NARROW, True, "narrow", shots=False)
    assert not problems, "\n".join(problems)


TITLE_PAGES = {
    "inbox": "/", "backlog": "/backlog", "kanban": "/kanban", "planning": "/planning", "projects": "/projects",
    "project-detail": "/projects/KET", "workflows": "/workflows", "profile": "/profile",
    "admin-users": "/admin/users", "admin-settings": "/admin/settings", "admin-database": "/admin/database",
    "admin-ui-kit": "/admin/ui-kit",
}
ADMIN_TITLES = {"admin-users": "Admin: Users & agents", "admin-settings": "Admin: Settings",
                "admin-database": "Admin: Database", "admin-ui-kit": "Admin: UI kit"}
TITLE_STYLE_JS = """() => { const h = document.querySelectorAll("main h1");
  if (h.length !== 1 || !h[0].classList.contains("ui-page-title")) return null;
  const s = getComputedStyle(h[0]);
  return {size: s.fontSize, weight: s.fontWeight, line: s.lineHeight, text: h[0].innerText.trim()}; }"""


@pytest.mark.parametrize("width", [390, 1440])
def test_page_titles_share_one_style(browser, env, ui, server, width):  # noqa: F811
    """every main page has one h1.ui-page-title with the same computed size/weight/line height;
    admin pages say "Admin: …" in one line and in the browser tab."""
    _seed(env, ui)
    ctx = _context(browser, server, (width, 900), width == 390)
    try:
        page = ctx.new_page()
        _login(page, server)
        styles = {}
        for name, path in TITLE_PAGES.items():
            page.goto(server + path)
            style = page.evaluate(TITLE_STYLE_JS)
            assert style, f"{name}: no single h1.ui-page-title"
            if name in ADMIN_TITLES:
                assert style["text"] == ADMIN_TITLES[name]
                assert page.title() == f"{ADMIN_TITLES[name]} – TaskBrigade"
                assert not page.locator(".ui-settings-kicker").count()
            styles[name] = (style["size"], style["weight"], style["line"])
        expected = ("22px" if width == 390 else "26px", "700", "26.4px" if width == 390 else "31.2px")
        assert {n: s for n, s in styles.items() if s != expected} == {}
    finally:
        ctx.close()


@pytest.mark.parametrize("width", [390, 1440])
def test_language_default_links_restore_admin_default(browser, env, ui, server, width):  # noqa: F811
    ctx = _context(browser, server, (width, 900), width == 390)
    try:
        page = ctx.new_page()
        _login(page, server)
        page.goto(server + "/profile")
        assert page.locator("html").get_attribute("lang") == "en"
        page.locator('[aria-controls="account-menu"]').click()
        menu = page.locator("#account-menu")
        assert "lang=en&" in menu.locator('a[aria-current="true"]').get_attribute("href")
        default = menu.locator('a[href^="/language?lang=default&"]')
        assert default.inner_text() == "Default (DE)"
        assert default.bounding_box()["width"] >= 44
        assert default.bounding_box()["height"] >= 44
        default.click()
        page.wait_for_load_state("networkidle")
        assert page.locator("html").get_attribute("lang") == "de"
        assert not any(cookie["name"] == "tm_lang" for cookie in ctx.cookies())
        assert "lang=default&" in page.locator('.pf-language a[aria-current="true"]').get_attribute("href")
        page.locator('[aria-controls="account-menu"]').click()
        assert "lang=default&" in menu.locator('a[aria-current="true"]').get_attribute("href")
        page.locator('[aria-controls="account-menu"]').click()
        page.locator('.pf-language a[href^="/language?lang=en&"]').click()
        page.wait_for_load_state("networkidle")
        assert page.locator("html").get_attribute("lang") == "en"
        page.locator('.pf-language a[href^="/language?lang=default&"]').click()
        page.wait_for_load_state("networkidle")
        assert page.locator("html").get_attribute("lang") == "de"
        assert not any(cookie["name"] == "tm_lang" for cookie in ctx.cookies())
    finally:
        ctx.close()


def test_profile_display_switches_save_on_toggle(browser, env, ui, server):  # noqa: F811
    """a click on a display switch is stored right away (htmx) – no save button, survives a reload."""
    ctx = _context(browser, server, (1440, 900), False)
    try:
        page = ctx.new_page()
        _login(page, server)
        page.goto(server + "/profile")
        for name in ("show_all_projects", "show_all_agents"):
            switch = page.locator(f'#pf-display input[name="{name}"]')
            was = switch.is_checked()
            with page.expect_response(lambda r: r.url.endswith("/profile/display") and r.status == 200):
                switch.locator("xpath=..").click()  # The visible label/track, as a user clicks it.
            page.locator("#pf-display-status", has_text="Saved.").wait_for()
            page.reload()
            assert page.locator(f'#pf-display input[name="{name}"]').is_checked() is not was, name
    finally:
        ctx.close()


ROW_JS = """() => [...document.querySelectorAll(".agent-review-row")].map(r => ({
  align: getComputedStyle(r).textAlign, dot: getComputedStyle(r, "::before").content,
  justify: getComputedStyle(r.querySelector(".agent-review-top")).justifyContent,
  right: r.getBoundingClientRect().right, card: r.parentElement.getBoundingClientRect().right,
  title: r.querySelector(".agent-review-title").getBoundingClientRect().left - r.getBoundingClientRect().left}))"""


@pytest.mark.parametrize("width", [360, 390, 768, 1440])
def test_inbox_agent_reviews_section(browser, env, ui, server, width):  # noqa: F811
    """Running agent reviews: own section below "Needs your action", compact left-aligned rows, no stray dot,
    nothing wider than the viewport; full-page shots go to `TBR_SCREENSHOT_OUT` (no baseline)."""
    from tests.test_reviewers import _role

    _seed(env, ui)
    _role("hermes", "reviewer")
    nr = mktask(env, title="Show agent reviews in their own section", assigned_to="claude-code", prio=2,
                reviewer="hermes")["task_nr"]
    t = claim(env, task_nr=nr).json()
    env["gitea"].push_branch("taskbrigade", "ket", t["branch_name"])
    env["c"].post(f"/api/v1/tasks/{nr}/submit", headers=env["claude"],
                  json={"branch_name": t["branch_name"], "commit_sha": SHA, "summary": "Done"})
    env["c"].post("/api/v1/agents/me/claim", headers=env["hermes"], json={"reviews": True, "task_nr": nr})
    phone = width < 640
    ctx = _context(browser, server, (width, 900), phone)
    try:
        page = ctx.new_page()
        _login(page, server)
        page.goto(server + "/")
        _settle(page)
        problems = page.evaluate(CHECKS_JS, {"phone": phone})
        assert not problems, "\n".join(problems)
        rows = page.evaluate(ROW_JS)
        assert len(rows) == 1 and rows[0]["align"] in ("left", "start") and rows[0]["dot"] in ("none", "normal")
        assert rows[0]["justify"] in ("normal", "flex-start", "start") and rows[0]["title"] <= 17
        assert rows[0]["right"] <= rows[0]["card"] + 0.5 <= width
        section = page.locator(".inbox-agent-reviews")
        assert "Review running" in section.inner_text() and "Reviews by agents" in section.inner_text()
        cards = page.locator(".inbox-cards").bounding_box()
        assert section.bounding_box()["y"] > cards["y"] + cards["height"]  # Below "Needs your action".
        # The notices panel only points to the section: no agent-review details, table or explanation.
        notices = page.locator(".inbox-notices")
        assert notices.locator(".notice.agent-review").count() == 0 and nr not in notices.inner_text()
        pointer = notices.locator('a[href="#agent-reviews-title"]')
        assert pointer.text_content() == "1 review by agents ↓"  # Folded on phones.
        page.locator(f"#ar-{nr} .request-changes > summary").click()
        assert page.locator(f"#ar-{nr} textarea").is_visible()
        assert page.evaluate("document.documentElement.scrollWidth") <= width
        if out := os.environ.get("TBR_SCREENSHOT_OUT"):
            page.locator(f"#ar-{nr} .request-changes > summary").click()
            Path(out).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(out) / f"inbox-agent-reviews-{width}.png"), full_page=True)
    finally:
        ctx.close()
