"""Slide renderer: turns copy + design spec into editable HTML and 1080x1350 PNG slides."""

from __future__ import annotations

import html
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

W, H = 1080, 1350

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #777; font-family: '{body_font}', 'DejaVu Sans', sans-serif; }
.slide { width: {W}px; height: {H}px; position: relative; overflow: hidden; display: flex;
  flex-direction: column; justify-content: center; padding: 110px 96px; margin: 0 auto 40px; }
.dark { background: {primary}; color: {text_light}; }
.light { background: {light}; color: {text_dark}; }
.slide h1, .slide h2 { font-family: '{heading_font}', 'DejaVu Sans', sans-serif; letter-spacing: -0.02em; }
.slide h1 { font-size: 92px; line-height: 1.04; font-weight: 800; }
.slide h2 { font-size: 66px; line-height: 1.1; font-weight: 800; margin-bottom: 40px; }
.slide p { font-size: 38px; line-height: 1.42; font-weight: 400; opacity: .92; white-space: pre-line; }
.slide p + p { margin-top: 24px; }
.note { font-size: 28px !important; line-height: 1.35 !important; opacity: .8 !important; margin-top: 30px;
  padding-top: 18px; border-top: 2px solid rgba(127,127,127,.35); }
.slide ul { list-style: none; }
.slide li { font-size: 37px; line-height: 1.35; padding: 22px 0 22px 58px; position: relative;
  border-bottom: 2px solid rgba(127,127,127,.25); }
.slide li::before { content: ''; position: absolute; left: 0; top: 36px; width: 26px; height: 26px;
  border-radius: 50%; background: {accent}; }
.hl { color: {accent}; }
.stat-figure { font-family: '{heading_font}', sans-serif; font-size: 170px; font-weight: 800; color: {accent};
  line-height: 1; margin-bottom: 36px; word-break: break-word; }
.bar { position: absolute; left: 96px; top: 84px; width: 120px; height: 10px; background: {accent}; }
.counter { position: absolute; right: 96px; top: 70px; font-size: 28px; opacity: .6; font-weight: 600; }
.brand { position: absolute; left: 96px; bottom: 70px; font-size: 28px; font-weight: 700; letter-spacing: .08em;
  text-transform: uppercase; opacity: .75; }
.swipe { position: absolute; right: 96px; bottom: 70px; font-size: 28px; font-weight: 600; color: {accent}; }
.layout-cover h1 { margin-top: 40px; }
.cta-box { margin-top: 50px; background: {accent}; color: {primary}; padding: 34px 44px; border-radius: 18px;
  font-size: 38px; font-weight: 700; line-height: 1.3; }
"""


def _hl(text: str, highlight: str) -> str:
    safe = html.escape(text)
    if highlight and highlight in text:
        h = html.escape(highlight)
        return safe.replace(h, f'<span class="hl">{h}</span>', 1)
    return safe


NOTE_SPLIT = re.compile(r"(?:^|\s)\* ?(?=[A-Z])")


def _split_notes(text: str) -> tuple[str, list[str]]:
    """Separates footnotes (lines/segments starting with '*') from the main text."""
    main, notes = [], []
    for line in text.split("\n"):
        parts = NOTE_SPLIT.split(line)
        if line.lstrip().startswith("*") and len(parts) > 1 and not parts[0].strip():
            parts = parts[1:]
            notes += [p.strip() for p in parts if p.strip()]
            continue
        main.append(parts[0].rstrip())
        notes += [p.strip() for p in parts[1:] if p.strip()]
    return "\n".join(main).strip(), notes


def _notes_html(notes: list[str]) -> str:
    return "".join(f'<p class="note">*{html.escape(n)}</p>' for n in notes)


def _slide_html(slide: dict, design: dict, total: int, theme: str, brand_name: str, cta: str) -> str:
    n, layout = slide["n"], design.get("layout", "statement")
    hl = design.get("highlight", "")
    head = slide["headline"]
    body, notes = _split_notes(slide["body"].strip())
    cls = theme if theme in ("dark", "light") else "dark"   # the Designer's theme applies to every slide
    chrome = (f'<div class="bar"></div><div class="counter">{n}/{total}</div>'
              f'<div class="brand">{html.escape(brand_name)}</div>')
    if n < total:
        chrome += '<div class="swipe">Swipe &rarr;</div>'

    if layout == "cover":
        inner = f'<h1>{_hl(head, hl)}</h1><p style="margin-top:40px">{_hl(body, hl)}</p>{_notes_html(notes)}'
    elif layout == "list":
        raw = [b.strip() for b in (design.get("bullets") or "").split("|") if b.strip()]
        if not raw:
            raw = [s.strip(" •-") for s in body.split("\n") if s.strip()] or [body]
        intro = [b for b in raw if b.endswith(":")]
        items = [b for b in raw if not b.endswith(":") and not b.startswith("*")]
        notes += [b.lstrip("*").strip() for b in raw if b.startswith("*") and b.lstrip("*").strip() not in notes]
        inner = (f'<h2>{_hl(head, hl)}</h2>' + "".join(f"<p>{html.escape(i)}</p>" for i in intro)
                 + "<ul>" + "".join(f"<li>{_hl(i, hl)}</li>" for i in items) + "</ul>" + _notes_html(notes))
    elif layout == "stat" and hl and (hl in head or hl in body):
        # The figure is followed immediately by its qualifying note (e.g. date / "hypothetical").
        inner = (f'<div class="stat-figure">{html.escape(hl)}</div>{_notes_html(notes)}'
                 f'<h2>{html.escape(head)}</h2><p>{html.escape(body)}</p>')
    elif layout == "cta":
        show_box = cta and cta not in body and cta not in head
        inner = (f'<h2>{_hl(head, hl)}</h2><p>{_hl(body, hl)}</p>{_notes_html(notes)}'
                 + (f'<div class="cta-box">{html.escape(cta)}</div>' if show_box else ""))
    else:
        inner = f'<h2>{_hl(head, hl)}</h2><p>{_hl(body, hl)}</p>{_notes_html(notes)}'
    return f'<section class="slide {cls} layout-{layout}" id="slide-{n}">{chrome}<div>{inner}</div></section>'


def render_carousel(carousel: dict, design: dict, brand: dict, brand_name: str, out_dir: Path) -> list[Path]:
    """Writes slides.html (editable) and slide-XX.png files. Returns PNG paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    colors, fonts = brand["colors"], brand["fonts"]
    css = CSS
    for k, v in {**colors, "heading_font": fonts["heading"], "body_font": fonts["body"], "W": W, "H": H}.items():
        css = css.replace("{" + k + "}", str(v))
    by_n = {d["n"]: d for d in design.get("slides", [])}
    slides = sorted(carousel["slides"], key=lambda s: s["n"])
    total = len(slides)
    theme = design.get("theme", "dark")
    sections = [_slide_html(s, by_n.get(s["n"], {}), total, theme, brand_name, carousel.get("cta", "")) for s in slides]
    doc = (f'<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(carousel["title"])}</title>'
           f"<style>{css}</style></head><body>{''.join(sections)}</body></html>")
    html_path = out_dir / "slides.html"
    html_path.write_text(doc)

    pngs: list[Path] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H})
        page.goto(html_path.resolve().as_uri())
        page.wait_for_load_state("networkidle")
        for s in slides:
            path = out_dir / f"slide-{s['n']:02d}.png"
            page.locator(f"#slide-{s['n']}").screenshot(path=str(path))
            pngs.append(path)
        browser.close()
    return pngs


def check_overflow(html_path: Path) -> list[int]:
    """Returns slide numbers whose content overflows the 1080x1350 frame."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H})
        page.goto(html_path.resolve().as_uri())
        bad = page.evaluate(
            """() => [...document.querySelectorAll('.slide')].filter(s => {
                   const inner = s.lastElementChild;
                   return inner.scrollHeight > s.clientHeight - 220 || inner.scrollWidth > s.clientWidth - 192;
               }).map(s => parseInt(s.id.split('-')[1]))"""
        )
        browser.close()
    return bad
