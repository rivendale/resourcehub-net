#!/usr/bin/env python3
"""Build resourcehub.net: a public index of research papers, people to follow, repo reviews and demos.
Python standard library only.

    build.py [--entries entries] [--schemas schemas] [--out site]

entries/<section>/<id>.json is the source of truth, one reviewed entry per file. schemas/common.schema.json
holds the fields every entry carries; schemas/<section>.schema.json the rest. Output: one HTML page per
section, entries/<section>/<id>.json, index.json, llms.txt, llms-full.txt, feed.xml, and the merged
per-section schemas under schemas/. The build writes a temp dir and swaps it in only when everything
succeeded, so a bad entry leaves the previous site untouched. RH_NOW (ISO UTC) pins the clock for tests.

What the build refuses (tests/FAILURES.md): anything off-schema, unknown fields, control characters, a URL
that is not https, a redirect or tracker host, a query key that can identify a reader, and an --out
directory it did not make. feed.xml carries the newest 50 entries; every other output carries all of
them. Truth is the review gate's job, not the build's.
"""
import argparse
import datetime
import email.utils
import html
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE = "https://resourcehub.net"
SECTIONS = [
    ("papers", "Papers", "Research papers worth reading, linked to the primary source, across every field."),
    ("people", "People", "People producing consistently useful public work, and where to start with each."),
    ("repos", "Repo reviews", "Open-source repositories, each marked Reviewed with a focus level against a stated need."),
    ("demos", "Demos", "Dated demonstrations of what AI systems can do, as a record of progress."),
]
TRACKER_HOSTS = {"t.co", "bit.ly", "share.google", "lnkd.in", "buff.ly", "ow.ly", "tinyurl.com", "goo.gl"}
TOKEN_KEYS = {"token", "key", "sig", "signature", "auth", "email", "subscriber", "access_token"}
MARKER = ".resourcehub-build"   # only a directory carrying this is ever replaced
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")   # XML 1.0 forbids these; JSON allows them


def die(msg):
    print(f"build.py: {msg}", file=sys.stderr)
    sys.exit(1)


def esc(s):
    return html.escape(str(s), quote=True)


def today():
    now = os.environ.get("RH_NOW")
    t = datetime.datetime.fromisoformat(now.replace("Z", "+00:00")) if now else datetime.datetime.now(datetime.timezone.utc)
    return t.date()


# ---- schemas -------------------------------------------------------------------------------------

def load_schemas(schema_dir):
    common = json.load(open(os.path.join(schema_dir, "common.schema.json")))
    merged = {}
    for sec, _, _ in SECTIONS:
        s = json.load(open(os.path.join(schema_dir, f"{sec}.schema.json")))
        m = dict(s)
        m["properties"] = {**common["properties"], **s["properties"]}
        m["required"] = common["required"] + [r for r in s["required"] if r not in common["required"]]
        m["additionalProperties"] = False
        merged[sec] = m
    return merged


TYPES = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict, "null": type(None)}


def validate(value, schema, where):
    """The JSON Schema subset the schemas use. Returns a list of 'path: problem' strings."""
    errs = []
    # Typed: in Python 1 == True, but the JSON schema means the boolean.
    if "const" in schema and not (value == schema["const"] and type(value) is type(schema["const"])):
        return [f"{where}: must be {json.dumps(schema['const'])}"]
    types = schema.get("type")
    if types:
        types = types if isinstance(types, list) else [types]
        ok = any(isinstance(value, TYPES[t]) and not (t == "integer" and isinstance(value, bool)) for t in types)
        if not ok:
            return [f"{where}: expected {'/'.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{where}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, str):
        if CONTROL.search(value):
            errs.append(f"{where}: contains a control character")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errs.append(f"{where}: {value!r} does not match {schema['pattern']}")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 10 ** 9):
            errs.append(f"{where}: length {len(value)} outside {schema.get('minLength', 0)}..{schema.get('maxLength')}")
    if isinstance(value, int) and not isinstance(value, bool) and value < schema.get("minimum", value):
        errs.append(f"{where}: below minimum {schema['minimum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10 ** 9):
            errs.append(f"{where}: {len(value)} items outside {schema.get('minItems', 0)}..{schema.get('maxItems')}")
        for i, item in enumerate(value):
            errs += validate(item, schema.get("items", {}), f"{where}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for r in schema.get("required", []):
            if r not in value:
                errs.append(f"{where}: missing required field '{r}'")
        if schema.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    errs.append(f"{where}: unknown field '{k}' (schemas forbid extra fields)")
        if len(value) < schema.get("minProperties", 0):
            errs.append(f"{where}: needs at least {schema['minProperties']} field(s)")
        for k, v in value.items():
            if k in props:
                errs += validate(v, props[k], f"{where}.{k}")
    return errs


# ---- link safety ---------------------------------------------------------------------------------

def strings(value, where):
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from strings(v, f"{where}[{i}]")
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from strings(v, f"{where}.{k}")


def link_problems(entry, where):
    errs = []
    for path, s in strings(entry, where):
        if not re.match(r"^[a-z]+://", s, re.I):
            continue
        u = urllib.parse.urlsplit(s)
        host = (u.hostname or "").lower().rstrip(".")   # t.co. is t.co
        if u.scheme != "https":
            errs.append(f"{path}: {s!r} is not https")
        if host in TRACKER_HOSTS:
            errs.append(f"{path}: {host} is a redirect/tracker; link the primary source")
        if host.endswith("substack.com") and "/redirect/" in u.path:
            errs.append(f"{path}: a substack /redirect/ link embeds a subscriber token; link the post itself")
        for k, _ in urllib.parse.parse_qsl(u.query, keep_blank_values=True):
            if k.lower() in TOKEN_KEYS or k.lower().startswith("utm_") or (host.endswith("substack.com") and k == "r"):
                errs.append(f"{path}: query key '{k}' can carry a reader's identity; drop it")
    return errs


# ---- load ----------------------------------------------------------------------------------------

def load_entries(entries_dir, schemas):
    entries, seen, errs = {s: [] for s, _, _ in SECTIONS}, {}, []
    for sec, _, _ in SECTIONS:
        d = os.path.join(entries_dir, sec)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(sec, name)
            try:
                e = json.load(open(os.path.join(d, name), encoding="utf-8"))
            except (ValueError, UnicodeDecodeError) as x:
                errs.append(f"{path}: not valid JSON ({x})")
                continue
            if not isinstance(e, dict):
                errs.append(f"{path}: not a JSON object")
                continue
            if e.get("section") != sec:
                errs.append(f"{path}: section is {e.get('section')!r} but the file is under {sec}/")
                continue
            problems = validate(e, schemas[sec], path) + link_problems(e, path)
            if e.get("id") != name[:-5]:
                problems.append(f"{path}: id {e.get('id')!r} differs from the file name")
            if isinstance(e.get("added"), str) and re.match(r"^\d{4}-\d{2}-\d{2}$", e["added"]):
                try:
                    if datetime.date.fromisoformat(e["added"]) > today():
                        problems.append(f"{path}: added {e['added']} is in the future")
                except ValueError:
                    problems.append(f"{path}: added {e['added']!r} is not a real date")
            if e.get("id") in seen:
                problems.append(f"{path}: id {e['id']!r} is also used by {seen[e['id']]}")
            seen.setdefault(e.get("id"), path)
            if problems:
                errs += problems
            else:
                entries[sec].append(e)
    if errs:
        die("refusing to build:\n  " + "\n  ".join(errs))
    for sec in entries:
        entries[sec].sort(key=lambda e: (e["added"], e["id"]), reverse=True)
    return entries


# ---- render --------------------------------------------------------------------------------------

CSS = """:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#5d5b55;--line:#e3e0d8;--card:#ffffff;--accent:#2b5c8a;
--v:#2f6b3a;--u:#8a6a12;--h:#9a3a2a;--o:#55507a}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--muted:#a3a198;--line:#2d2c29;--card:#1c1b19;
--accent:#86b4e0;--v:#7fbf88;--u:#d9b55a;--h:#e08a78;--o:#a9a3d6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:24px 16px 64px}
header{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:baseline;border-bottom:1px solid var(--line);padding-bottom:12px}
header a.home{font-weight:700;font-size:1.15rem;color:var(--fg);text-decoration:none}
nav a{margin-right:14px;color:var(--muted);text-decoration:none}nav a[aria-current]{color:var(--fg);font-weight:600}
a{color:var(--accent)}h1{font-size:1.6rem;margin:28px 0 6px}p.lede{color:var(--muted);margin:0 0 20px}
article{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;margin:14px 0}
article h2{font-size:1.1rem;margin:0 0 4px}article p{margin:6px 0}.meta{color:var(--muted);font-size:.88rem}
.tag{display:inline-block;font-size:.78rem;border:1px solid var(--line);border-radius:999px;padding:1px 8px;margin:2px 4px 2px 0}
.claim-verified{color:var(--v)}.claim-unverified{color:var(--u)}.claim-hype{color:var(--h)}.claim-opinion{color:var(--o)}
.sections{display:grid;gap:12px}.sections article{margin:0}footer{margin-top:40px;color:var(--muted);font-size:.85rem;border-top:1px solid var(--line);padding-top:12px}
dt{font-weight:600;margin-top:6px}dd{margin:0 0 4px}
"""


def page(title, body, current=None, desc=""):
    here = ' aria-current="page"'
    nav = "".join(f'<a href="/{s}/"{here if s == current else ""}>{esc(t)}</a>' for s, t, _ in SECTIONS)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title><meta name="description" content="{esc(desc)}">
<link rel="stylesheet" href="/style.css"><link rel="alternate" type="application/rss+xml" title="resourcehub.net" href="/feed.xml">
</head><body><div class="wrap"><header><a class="home" href="/">resourcehub.net</a><nav>{nav}</nav></header>
<main>{body}</main>
<footer>Every entry is also JSON (<a href="/index.json">index.json</a>, <a href="/llms.txt">llms.txt</a>,
<a href="/feed.xml">RSS</a>). Source and review history: <a href="https://github.com/rivendale/resourcehub-net">github.com/rivendale/resourcehub-net</a>.
Entries say what a thing is and what it found; a link is a reference, not an endorsement.</footer>
</div></body></html>
"""


def claim(c):
    return f'<span class="tag claim-{esc(c)}">{esc(c)}</span>'


def link(u, text=None):
    return f'<a href="{esc(u)}" rel="noopener">{esc(text or u)}</a>'


def card(e):
    s, j = e["section"], f'/entries/{e["section"]}/{e["id"]}.json'
    meta = f'<p class="meta">added {esc(e["added"])} · reviewed by {esc(e["reviewed_by"])}{" · agent-written" if e["agent_written"] else ""} · <a href="{j}">json</a></p>'
    if s == "papers":
        tags = "".join(f'<span class="tag">{esc(t)}</span>' for t in e["tags"])
        code = f' · {link(e["code"], "code")}' if e["code"] else ""
        return (f'<article id="{esc(e["id"])}"><h2>{link(e["link"], e["title"])}</h2>'
                f'<p class="meta">{esc(", ".join(e["authors"]))} · {esc(e["date"])} · {esc(e["publication"])}{code}</p>'
                f'<p>{claim(e["claim"])} {esc(e["takeaway"])}</p><dl><dt>Key result</dt><dd>{esc(e["key_result"])}</dd>'
                f'<dt>Why it matters</dt><dd>{esc(e["why_it_matters"])}</dd></dl><p>{tags}</p>{meta}</article>')
    if s == "people":
        links = " · ".join(link(u, k) for k, u in sorted(e["links"].items()))
        start = "".join(f"<li>{link(u)}</li>" for u in e["start_here"])
        return (f'<article id="{esc(e["id"])}"><h2>{esc(e["name"])}</h2><p class="meta">{esc(e["affiliation"])} · {esc(e["focus"])}</p>'
                f'<p>{esc(e["why"])}</p><p>{links}</p><dl><dt>Start here</dt><dd><ul>{start}</ul></dd></dl>{meta}</article>')
    if s == "repos":
        return (f'<article id="{esc(e["id"])}"><h2>{link(e["url"], e["repo"])}</h2>'
                f'<p class="meta">{esc(e["language"])} · {esc(e["license"])} · {e["stars_at_review"]} stars at review · pushed {esc(e["pushed"])}</p>'
                f'<p><span class="tag">Reviewed {esc(e["reviewed_on"])}</span><span class="tag">focus: {esc(e["focus"])}</span></p><p>{esc(e["what_it_is"])}</p>'
                f'<dl><dt>Why this focus</dt><dd>{esc(e["focus_reason"])}</dd><dt>Measured against</dt><dd>{esc(e["measured_against"])}</dd></dl>{meta}</article>')
    return (f'<article id="{esc(e["id"])}"><h2>{link(e["link"], e["title"])}</h2>'
            f'<p class="meta">{esc(e["date"])} · {esc(e["model_or_system"])} · <span class="tag">{esc(e["capability_tag"])}</span></p>'
            f'<p>{claim(e["claim"])} {esc(e["what_it_shows"])}</p>{meta}</article>')


def summary_line(e):
    title = e.get("title") or e.get("name") or e.get("repo")
    gist = e.get("takeaway") or e.get("why") or e.get("what_it_is") or e.get("what_it_shows")
    return title, gist


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def render(entries, schemas, out):
    every = sorted((e for es in entries.values() for e in es), key=lambda e: (e["added"], e["id"]), reverse=True)
    stamp = every[0]["added"] if every else today().isoformat()
    write(f"{out}/{MARKER}", "written by build.py; marks this directory as safe to replace\n")
    write(f"{out}/style.css", CSS)
    write(f"{out}/.nojekyll", "")
    write(f"{out}/robots.txt", "User-agent: *\nAllow: /\n")
    blocks = "".join(f'<article><h2><a href="/{s}/">{esc(t)}</a></h2><p>{esc(d)}</p><p class="meta">{len(entries[s])} entr{"y" if len(entries[s]) == 1 else "ies"}</p></article>'
                     for s, t, d in SECTIONS)
    write(f"{out}/index.html", page("resourcehub.net", f'<h1>Research worth reading</h1><p class="lede">A public index of papers, '
          f'people, open-source repositories and demos, each entry reviewed before it is published and available as JSON.</p>'
          f'<div class="sections">{blocks}</div>', desc="A public, reviewed index of research papers, people, repos and demos."))
    for s, t, d in SECTIONS:
        body = "".join(card(e) for e in entries[s]) or '<p class="lede">No entries yet.</p>'
        write(f"{out}/{s}/index.html", page(f"{t} · resourcehub.net", f"<h1>{esc(t)}</h1><p class=\"lede\">{esc(d)}</p>{body}", s, d))
        for e in entries[s]:
            write(f"{out}/entries/{s}/{e['id']}.json", json.dumps(e, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        write(f"{out}/schemas/{s}.schema.json", json.dumps(schemas[s], indent=2, sort_keys=True) + "\n")
    write(f"{out}/index.json", json.dumps({"site": SITE, "updated": stamp, "count": len(every),
          "entries": [{"id": e["id"], "section": e["section"], "added": e["added"], "title": summary_line(e)[0],
                       "json": f"{SITE}/entries/{e['section']}/{e['id']}.json"} for e in every]},
          indent=2, ensure_ascii=False) + "\n")
    llms = [f"# resourcehub.net\n\n> A public, reviewed index of research papers, people to follow, open-source repo reviews "
            f"and AI demos. Every entry is JSON; schemas at {SITE}/schemas/<section>.schema.json.\n"]
    full = list(llms)
    for s, t, d in SECTIONS:
        llms.append(f"\n## {t}\n\n")
        full.append(f"\n## {t}\n\n{d}\n\n")
        for e in entries[s]:
            title, gist = summary_line(e)
            llms.append(f"- [{title}]({SITE}/entries/{s}/{e['id']}.json): {gist}\n")
            full.append(f"### {title}\n\n```json\n{json.dumps(e, indent=2, ensure_ascii=False, sort_keys=True)}\n```\n\n")
    write(f"{out}/llms.txt", "".join(llms))
    write(f"{out}/llms-full.txt", "".join(full))
    items = []
    for e in every[:50]:
        title, gist = summary_line(e)
        when = email.utils.format_datetime(datetime.datetime.fromisoformat(e["added"]).replace(tzinfo=datetime.timezone.utc))
        url = f"{SITE}/{e['section']}/#{e['id']}"
        items.append(f"<item><title>{esc(title)}</title><link>{esc(url)}</link><guid isPermaLink=\"false\">{esc(SITE)}/entries/"
                     f"{esc(e['section'])}/{esc(e['id'])}.json</guid><pubDate>{when}</pubDate><description>{esc(gist)}</description></item>")
    write(f"{out}/feed.xml", '<?xml version="1.0" encoding="utf-8"?>\n<rss version="2.0"><channel><title>resourcehub.net</title>'
          f"<link>{SITE}/</link><description>Reviewed research papers, people, repos and demos.</description>"
          + "".join(items) + "</channel></rss>\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--entries", default=os.path.join(ROOT, "entries"))
    ap.add_argument("--schemas", default=os.path.join(ROOT, "schemas"))
    ap.add_argument("--out", default=os.path.join(ROOT, "site"))
    a = ap.parse_args()
    schemas = load_schemas(a.schemas)
    entries = load_entries(a.entries, schemas)
    out = os.path.abspath(a.out)
    if os.path.exists(out) and not os.path.isfile(os.path.join(out, MARKER)):
        die(f"refusing to replace {out}: it is not a previous build (no {MARKER})")
    parent = os.path.dirname(out)
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".site-", dir=parent)
    os.chmod(tmp, 0o755)   # mkdtemp makes it 0700
    try:
        render(entries, schemas, tmp)
        old = None
        if os.path.exists(out):
            old = tempfile.mkdtemp(prefix=".site-old-", dir=parent)
            os.rename(out, os.path.join(old, "site"))
        os.rename(tmp, out)
        if old:
            shutil.rmtree(old)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    print(f"built {sum(len(v) for v in entries.values())} entries into {out}")


if __name__ == "__main__":
    main()
