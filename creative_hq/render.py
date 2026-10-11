"""Slide renderer: turns copy + design spec into editable HTML and 1080x1350 PNG slides."""

from __future__ import annotations

import html
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
.slide p { font-size: 38px; line-height: 1.42; font-weight: 400; opacity: .92; }
.slide ul { list-style: none; }
.slide li { font-size: 37px; line-height: 1.35; padding: 22px 0 22px 58px; position: relative;
  border-bottom: 2px solid rgba(127,127,127,.25); }
.slide li::before { content: ''; position: absolute; left: 0; top: 36px; width: 26px; height: 26px;
  border-radius: 50%; background: {accent}; }
.hl { color: {accent}; }
.stat { font-family: '{heading_font}', sans-serif; font-size: 170px; font-weight: 800; color: {accent};
  line-height: 1; margin-bottom: 36px; word-break: break-word; }
.bar { position: absolute; left: 96px; top: 84px; width: 120px; height: 10px; background: {accent}; }
.counter { position: absolute; right: 96px; top: 70px; font-size: 28px; opacity: .6; font-weight: 600; }
.brand { position: absolute; left: 96px; bottom: 70px; font-size: 28px; font-weight: 700; letter-spacing: .08em;
  text-transform: uppercase; opacity: .75; }
.swipe { position: absolute; right: 96px; bottom: 70px; font-size: 28px; font-weight: 600; color: {accent}; }
.cover h1 { margin-top: 40px; }
.cta-box { margin-top: 50px; background: {accent}; color: {primary}; padding: 34px 44px; border-radius: 18px;
  font-size: 38px; font-weight: 700; line-height: 1.3; }
"""


def _hl(text: str, highlight: str) -> str:
    safe = html.escape(text)
    if highlight and highlight in text:
        h = html.escape(highlight)
        return safe.replace(h, f'<span class="hl">{h}</span>', 1)
    return safe


def _slide_html(slide: dict, design: dict, total: int, theme: str, brand_name: str, cta: str) -> str:
    n, layout = slide["n"], design.get("layout", "statement")
    hl = design.get("highlight", "")
    head, body = slide["headline"], slide["body"]
    # Alternate themes for rhythm: cover/cta use the carousel theme, inner slides alternate.
    cls = theme if layout in ("cover", "cta") or n % 2 == 1 else ("light" if theme == "dark" else "dark")
    chrome = (f'<div class="bar"></div><div class="counter">{n}/{total}</div>'
              f'<div class="brand">{html.escape(brand_name)}</div>')
    if n < total:
        chrome += '<div class="swipe">Swipe &rarr;</div>'

    if layout == "cover":
        inner = f'<h1>{_hl(head, hl)}</h1><p style="margin-top:40px">{_hl(body, hl)}</p>'
    elif layout == "list":
        items = [b.strip() for b in (design.get("bullets") or "").split("|") if b.strip()]
        if not items:
            items = [s.strip() for s in body.replace("•", "\n").split("\n") if s.strip()] or [body]
        inner = f'<h2>{_hl(head, hl)}</h2><ul>' + "".join(f"<li>{_hl(i, hl)}</li>" for i in items) + "</ul>"
    elif layout == "stat" and hl and (hl in head or hl in body):
        inner = f'<div class="stat">{html.escape(hl)}</div><h2>{html.escape(head)}</h2><p>{html.escape(body)}</p>'
    elif layout == "cta":
        inner = f'<h2>{_hl(head, hl)}</h2><p>{_hl(body, hl)}</p><div class="cta-box">{html.escape(cta)}</div>'
    else:
        inner = f'<h2>{_hl(head, hl)}</h2><p>{_hl(body, hl)}</p>'
    return f'<section class="slide {cls} {layout}" id="slide-{n}">{chrome}<div>{inner}</div></section>'


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
