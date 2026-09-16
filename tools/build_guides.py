"""
Generates standalone, crawlable static pages for each live guide on
The Elders Club, plus a sitemap.xml. Reads guide content straight out of
index.html (single source of truth) and per-guide extras (URL slug,
thumbnail) from guides_meta.json.

Usage:
    python3 tools/build_guides.py [--only SLUG]

Writes to ./guides/<urlSlug>/index.html and ./sitemap.xml, relative to the
project root (run from theeldersclub/). Never touches index.html itself and
never pushes/deploys anything -- purely a local build step to review before
publishing.
"""
import datetime as dt
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(ROOT, "index.html")
META_PATH = os.path.join(ROOT, "tools", "guides_meta.json")
OUT_ROOT = os.path.join(ROOT, "guides")
SITE_URL = "https://theeldersclub.com"

NAVY = "#1f3a4d"
GOLD = "#c9973a"
CREAM = "#fdf9f2"
CREAM_BG = "#fbf7f0"
TAN_BG = "#f6eddc"


def js_object_literal_to_py(text):
    """Converts a simple JS array-of-object-literals (bare keys, double-quoted
    string values, no nested functions) into valid JSON text, then parses it.
    This matches the exact shape used throughout this codebase's data arrays.

    Keys are only ever quoted when they sit right after `{` or `,` (the only
    structural positions a key can appear) -- anchoring on that, rather than
    matching "word:" anywhere, avoids corrupting string values that happen to
    contain a colon themselves (e.g. a title like "Google Pay: send money").
    """
    quoted = re.sub(
        r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)',
        r'\1"\2"\3',
        text,
    )
    return json.loads(quoted)


def extract_dc_script(html):
    start = html.find('<script type="__bundler/template">') + len('<script type="__bundler/template">')
    end = html.find("</script>", start)
    template_json_str = html[start:end]
    template_html = json.loads(template_json_str)
    dc_start = template_html.find('<script type="text/x-dc"')
    dc_start = template_html.find(">", dc_start) + 1
    dc_end = template_html.find("</script>", dc_start)
    return template_html[dc_start:dc_end]


def extract_array(dc, name):
    m = re.search(rf"const {name} = (\[.*?\n\]);", dc, re.S)
    if not m:
        raise ValueError(f"Could not find array {name} in index.html's script")
    return js_object_literal_to_py(m.group(1))


def extract_guide_content(dc):
    m = re.search(r"const GUIDE_CONTENT = (\{.*?\n\});", dc, re.S)
    if not m:
        raise ValueError("Could not find GUIDE_CONTENT in index.html's script")
    raw = m.group(1)
    # Replace bare STEPS/FAQS identifiers (e.g. `steps: STEPS2`) with a
    # resolvable placeholder string so this parses as JSON, then substitute
    # the real arrays back in afterward.
    # Longest-name-first avoids a prefix collision: naive replacement of the
    # bare token "FAQS" would also clobber the "FAQS" prefix inside "FAQS4",
    # "FAQS2", etc. (same for STEPS/STEPS2/STEPS3/STEPS4).
    array_refs = re.findall(r":\s*(STEPS\w*|FAQS\w*)\b", raw)
    for ref in sorted(set(array_refs), key=len, reverse=True):
        raw = re.sub(rf":\s*{re.escape(ref)}\b", f': "__REF_{ref}__"', raw)
    parsed = js_object_literal_to_py(raw)
    return parsed


def load_source_data():
    with open(INDEX_HTML) as f:
        html = f.read()
    dc = extract_dc_script(html)
    guides = extract_array(dc, "GUIDES")
    guide_content_raw = extract_guide_content(dc)

    array_names = set(re.findall(r"const (STEPS\w*|FAQS\w*) =", dc))
    arrays = {name: extract_array(dc, name) for name in array_names}

    guide_content = {}
    for slug, entry in guide_content_raw.items():
        resolved = dict(entry)
        for key in ("steps", "faqs"):
            val = resolved.get(key)
            if isinstance(val, str) and val.startswith("__REF_") and val.endswith("__"):
                ref_name = val[len("__REF_"):-2]
                resolved[key] = arrays[ref_name]
        guide_content[slug] = resolved

    return guides, guide_content


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_json_ld(guide, content, author_meta, last_updated_iso):
    total_min = guide["dur"].split()[0]
    minutes = 0
    seconds = 0
    if ":" in total_min:
        m, s = total_min.split(":")
        minutes, seconds = int(m), int(s)
    else:
        try:
            minutes = int(float(total_min))
        except ValueError:
            minutes = 0
    duration_iso = f"PT{minutes}M" + (f"{seconds}S" if seconds else "")

    person = {"@type": "Person", "name": author_meta["name"], "description": author_meta["bio"]}
    if author_meta.get("linkedin"):
        person["sameAs"] = [author_meta["linkedin"]]

    howto = {
        "@type": "HowTo",
        "name": guide["title"],
        "description": guide["blurb"],
        "totalTime": duration_iso,
        "author": person,
        "dateModified": last_updated_iso,
        "step": [
            {"@type": "HowToStep", "position": int(s["n"]), "name": s["title"], "text": s["body"]}
            for s in content.get("steps", [])
        ],
    }
    faqpage = {
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": f["q"], "acceptedAnswer": {"@type": "Answer", "text": f["a"]}}
            for f in content.get("faqs", [])
        ],
    }
    graph = {"@context": "https://schema.org", "@graph": [howto, faqpage]}
    return json.dumps(graph, indent=2, ensure_ascii=False)


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} | The Elders Club</title>
<meta name="description" content="{description}">
<meta property="og:title" content="{title} | The Elders Club">
<meta property="og:description" content="{description}">
<meta property="og:type" content="article">
<meta property="og:url" content="{canonical}">
{og_image}
<link rel="canonical" href="{canonical}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;700;800;900&display=swap" rel="stylesheet">
<script type="application/ld+json">
{json_ld}
</script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: "Archivo", system-ui, sans-serif; background: {cream_bg}; color: {navy}; line-height: 1.5; }}
  a {{ color: inherit; }}
  .wrap {{ max-width: 860px; margin: 0 auto; padding: 0 clamp(18px, 4vw, 32px); }}
  header.site {{ border-bottom: 2px solid {navy}; background: {cream}; padding: 20px 0; }}
  header.site .wrap {{ max-width: 1280px; display: flex; align-items: center; justify-content: space-between; }}
  .brand {{ font-weight: 900; font-size: 22px; text-decoration: none; letter-spacing: -0.01em; }}
  .home-link {{ font-weight: 700; text-decoration: none; border: 2px solid {navy}; padding: 10px 18px; }}
  .home-link:hover {{ background: {gold}; }}
  .crumb {{ padding: 20px 0 0; font-size: 15px; }}
  .crumb a {{ color: #8a5f1a; text-decoration: none; font-weight: 700; }}
  .hero {{ padding: 20px 0 32px; border-bottom: 2px solid {navy}; }}
  .eyebrow {{ font-weight: 700; font-size: 14px; letter-spacing: 0.12em; text-transform: uppercase; color: #8a5f1a; margin-bottom: 10px; }}
  h1 {{ font-size: clamp(28px, 4.6vw, 40px); font-weight: 900; letter-spacing: -0.02em; margin: 0 0 14px; line-height: 1.1; }}
  .meta {{ font-size: 15px; font-weight: 700; color: #3d5c70; }}
  .byline {{ font-size: 15px; color: #3d5c70; margin-top: 10px; }}
  .byline a {{ color: #8a5f1a; font-weight: 700; text-decoration: none; }}
  .byline a:hover {{ text-decoration: underline; }}
  .intro {{ padding: 28px 0; border-bottom: 2px solid {navy}; font-size: 19px; line-height: 1.6; color: #294a5e; }}
  .video {{ padding: 32px 0; border-bottom: 2px solid {navy}; }}
  .video-frame {{ position: relative; padding-bottom: 56.25%; height: 0; overflow: hidden; border: 2px solid {navy}; }}
  .video-frame iframe {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; border: 0; }}
  section.steps, section.faqs {{ padding: 40px 0; border-bottom: 2px solid {navy}; }}
  h2 {{ font-size: clamp(24px, 3.6vw, 32px); font-weight: 900; letter-spacing: -0.02em; margin: 0 0 24px; }}
  .step {{ display: flex; gap: 20px; padding: 20px 0; border-top: 1px solid rgba(31,58,77,0.15); }}
  .step:first-of-type {{ border-top: none; }}
  .step-n {{ flex: 0 0 auto; width: 40px; height: 40px; border-radius: 50%; background: {navy}; color: {cream}; display: flex; align-items: center; justify-content: center; font-weight: 800; }}
  .step-body h3 {{ font-size: 19px; font-weight: 800; margin: 0 0 6px; }}
  .step-body p {{ font-size: 16px; color: #294a5e; }}
  .step-shot {{ font-size: 14px; color: #6a7f8c; font-style: italic; margin-top: 6px; }}
  details {{ border-top: 1px solid rgba(31,58,77,0.15); padding: 16px 0; }}
  details:first-of-type {{ border-top: none; }}
  summary {{ font-weight: 800; font-size: 17px; cursor: pointer; }}
  details p {{ margin-top: 10px; font-size: 16px; color: #294a5e; }}
  section.recap {{ padding: 40px 0; border-bottom: 2px solid {navy}; }}
  .recap p.lead {{ font-size: 18px; color: #294a5e; margin-bottom: 20px; }}
  .recap ul {{ list-style: none; }}
  .recap li {{ padding: 10px 0 10px 32px; position: relative; font-size: 17px; font-weight: 700; }}
  .recap li:before {{ content: "\\2713"; position: absolute; left: 0; color: {gold}; font-weight: 900; }}
  .recap .closing {{ font-size: 17px; color: #294a5e; margin-top: 20px; }}
  .cta {{ padding: 40px 0; text-align: center; }}
  .cta a {{ display: inline-block; background: {navy}; color: {cream}; font-weight: 800; padding: 16px 28px; text-decoration: none; border: 2px solid {navy}; }}
  .cta a:hover {{ background: {gold}; color: {navy}; }}
  footer {{ background: {navy}; color: {cream}; padding: 32px 0; text-align: center; font-size: 15px; }}
  footer a {{ color: {cream}; text-decoration: underline; }}
</style>
</head>
<body>
<header class="site">
  <div class="wrap">
    <a class="brand" href="/">The Elders Club</a>
    <a class="home-link" href="/">All guides</a>
  </div>
</header>

<main class="wrap">
  <div class="crumb"><a href="/">All guides</a> / {app}</div>

  <div class="hero">
    <div class="eyebrow">{app} &middot; {level}</div>
    <h1>{title}</h1>
    <div class="meta">{dur}</div>
    <div class="byline">Written by {author_link}, {author_bio} &middot; Last updated {last_updated}</div>
  </div>

  <p class="intro">{intro}</p>

  <div class="video">
    <div class="video-frame">
      <iframe src="https://www.youtube.com/embed/{video_id}" title="{title}" loading="lazy" allowfullscreen></iframe>
    </div>
  </div>

  <section class="steps">
    <h2>The steps, written out</h2>
    {steps_html}
  </section>

  <section class="faqs">
    <h2>If something goes wrong</h2>
    {faqs_html}
  </section>

  <section class="recap">
    <h2>What you'll walk away knowing</h2>
    <p class="lead">By the end of this guide, made free and step-by-step by The Elders Club, you'll be able to:</p>
    <ul>
      {outcomes_html}
    </ul>
    {closing_html}
  </section>

  <div class="cta">
    <a href="/">See all free guides &rarr;</a>
  </div>
</main>

<footer>
  <div class="wrap">The Elders Club &mdash; free video guides, plain language, one screen at a time.<br>
  <a href="/">theeldersclub.com</a></div>
</footer>
</body>
</html>
"""


def render_guide_page(guide, content, meta, author_meta):
    steps_html = "\n".join(
        f'<div class="step"><div class="step-n">{esc(s["n"])}</div>'
        f'<div class="step-body"><h3>{esc(s["title"])}</h3><p>{esc(s["body"])}</p>'
        + (f'<p class="step-shot">On screen: {esc(s["shot"])}</p>' if s.get("shot") else "")
        + '</div></div>'
        for s in content.get("steps", [])
    )
    faqs_html = "\n".join(
        f'<details><summary>{esc(f["q"])}</summary><p>{esc(f["a"])}</p></details>'
        for f in content.get("faqs", [])
    )
    outcomes_html = "\n".join(
        f"<li>{esc(o)}</li>" for o in content.get("outcomes", [])
    )
    canonical = f'{SITE_URL}/guides/{meta["urlSlug"]}/'
    description = guide["blurb"]
    og_image = ""
    if meta.get("thumbnail"):
        og_image = f'<meta property="og:image" content="{SITE_URL}/assets/images/guides/{meta["thumbnail"]}">'

    last_updated_iso = meta.get("lastUpdated") or dt.date.today().isoformat()
    last_updated_display = dt.date.fromisoformat(last_updated_iso).strftime("%-d %B %Y")

    author_name = esc(author_meta["name"])
    if author_meta.get("linkedin"):
        author_link = f'<a href="{esc(author_meta["linkedin"])}" target="_blank" rel="noopener">{author_name}</a>'
    else:
        author_link = author_name
    author_bio = esc(author_meta["bio"])

    intro = meta.get("intro") or ""
    closing_html = f'<p class="closing">{esc(meta["closing"])}</p>' if meta.get("closing") else ""

    return PAGE_TEMPLATE.format(
        title=esc(guide["title"]),
        description=esc(description),
        canonical=canonical,
        og_image=og_image,
        json_ld=render_json_ld(guide, content, author_meta, last_updated_iso),
        navy=NAVY, gold=GOLD, cream=CREAM, cream_bg=CREAM_BG,
        app=esc(guide["app"]),
        level=esc(guide["level"]),
        dur=esc(guide["dur"]),
        video_id=content["videoId"],
        steps_html=steps_html,
        faqs_html=faqs_html,
        outcomes_html=outcomes_html,
        closing_html=closing_html,
        author_link=author_link,
        author_bio=author_bio,
        last_updated=last_updated_display,
        intro=esc(intro),
    )


def main():
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]

    with open(META_PATH) as f:
        meta_map = json.load(f)
    author_meta = meta_map["_author"]

    guides, guide_content = load_source_data()
    guides_by_slug = {g["slug"]: g for g in guides}

    live_slugs = [s for s in guide_content if s in meta_map]
    if only:
        live_slugs = [s for s in live_slugs if s == only]

    written = []
    for slug in live_slugs:
        guide = guides_by_slug[slug]
        content = guide_content[slug]
        meta = meta_map[slug]
        html = render_guide_page(guide, content, meta, author_meta)
        out_dir = os.path.join(OUT_ROOT, meta["urlSlug"])
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "index.html")
        with open(out_path, "w") as f:
            f.write(html)
        written.append((slug, out_path, f'{SITE_URL}/guides/{meta["urlSlug"]}/'))

    print(f"Wrote {len(written)} guide page(s):")
    for slug, path, url in written:
        print(f"  {slug:24s} -> {path}\n{'':26s}({url})")

    sitemap_urls = [SITE_URL + "/"] + [url for _, _, url in written]
    sitemap = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in sitemap_urls:
        sitemap.append(f"  <url><loc>{u}</loc></url>")
    sitemap.append("</urlset>")
    sitemap_path = os.path.join(ROOT, "sitemap.xml")
    with open(sitemap_path, "w") as f:
        f.write("\n".join(sitemap) + "\n")
    print(f"\nWrote sitemap.xml with {len(sitemap_urls)} URLs -> {sitemap_path}")


if __name__ == "__main__":
    main()
