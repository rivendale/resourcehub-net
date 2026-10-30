#!/usr/bin/env python3
"""Build resourcehub.net: a public index of research papers, people to follow, repo reviews and demos.
Python standard library only.

    build.py [--entries entries] [--schemas schemas] [--out site]

entries/<section>/<id>.json is the source of truth, one reviewed entry per file. schemas/common.schema.json
holds the fields every entry carries; schemas/<section>.schema.json the rest. Output: one HTML page per
section, entries/<section>/<id>.json, index.json, llms.txt, llms-full.txt, feed.xml, and the merged
per-section schemas under schemas/. The build writes a temp dir and swaps it in only when everything
succeeded, so a bad entry leaves the previous site untouched. RH_NOW (ISO UTC) pins the clock for tests.

What the build refuses (tests/FAILURES.md): anything off-schema, unknown fields, characters XML forbids, a URL
that is not a plain https URL (credentials, odd hosts), a redirect or tracker host (and its subdomains), a query key not on the allow-list, and an --out
directory it did not make. feed.xml carries the newest 50 entries; every other output carries all of
them. Truth is the review gate's job, not the build's.
"""
import argparse
import datetime
import email.utils
import errno
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
# Query keys a published link may carry. An allow-list, because nine review rounds showed a blocklist of
# identifying keys never converges (authToken, PHPSESSID, ck_subscriber_id, a custom-domain substack's r...).
# Any other key is refused: link the clean URL, or add the key here after checking it cannot identify a reader.
ALLOWED_QUERY_KEYS = {"id", "p", "page", "q", "lang", "hl", "tab"}
# Keys that are harmless only on particular hosts (and their subdomains): YouTube's t is a timestamp, but X's t
# is a share-tracking token; Scholar's user names the profile's subject, not the reader.
HOST_QUERY_KEYS = {"youtube.com": {"v", "t", "list", "index"}, "youtu.be": {"t", "list", "index"},
                   "scholar.google.com": {"user"}}
MARKER = ".resourcehub-build"   # only a directory carrying this is ever replaced
# JSON allows these, XML 1.0 does not (and entries are single-line text): controls incl. newline and tab,
# the non-characters U+FFFE/U+FFFF, and lone surrogates.
FORBIDDEN = re.compile("[\x00-\x1f\x7f\ufffe\uffff\ud800-\udfff]")
# A plain https URL: an ASCII host of dot-separated labels, an optional port, then path/query/fragment
# with no whitespace or backslash. Anything else (credentials, a percent-encoded or missing host, a
# backslash a browser would rewrite) is refused rather than parsed charitably.
LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
URL_RE = re.compile(rf"^https://(?P<host>{LABEL}(?:\.{LABEL})+)(?::(?P<port>[0-9]{{1,5}}))?(?P<rest>[/?#][^\s\\\"'<>]*)?$")
# Only scheme:// is a URL ("BERT: ..." is not). It ends at an ASCII space, so a non-breaking space inside it
# stays part of the candidate and URL_RE refuses the whole thing.
URL_IN_TEXT = re.compile(r"[a-z][a-z0-9+.-]*://[^ ]+", re.I)
TRAILING = ".,;:!?)]}\"'>"   # sentence punctuation and quotes/brackets around a URL, stripped from its end only
_writes = 0


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
        if FORBIDDEN.search(value):
            errs.append(f"{where}: contains a forbidden character (XML 1.0)")
        if "pattern" in schema and not re.search(schema["pattern"], value, re.ASCII):   # \d means 0-9 only
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


def url_problems(s, path):
    if not s.lower().startswith("https://"):
        return [f"{path}: {s!r} is not https"]
    if "@" in re.split(r"[/?#]", s[len("https://"):], maxsplit=1)[0]:
        return [f"{path}: the URL embeds credentials (user@ or user:pass@); never publish them"]
    m = URL_RE.match(s)
    if not m:
        return [f"{path}: {s!r} is not a plain https URL with an ASCII host"]
    errs = []
    if m.group("port") and not 1 <= int(m.group("port")) <= 65535:
        errs.append(f"{path}: port {m.group('port')} is outside 1..65535")
    host = m.group("host").lower()
    u = urllib.parse.urlsplit(s)
    # The host, and any host embedded later in the URL (two links glued together, or an archive.org wrapper).
    hosts = [host] + [h.lower() for h in re.findall(r"://([A-Za-z0-9.-]+)", s[len("https://"):])]
    for h in hosts:
        if any(h == t or h.endswith("." + t) for t in TRACKER_HOSTS):
            errs.append(f"{path}: {h} is a redirect/tracker; link the primary source")
    dpath, dfrag = urllib.parse.unquote(u.path), urllib.parse.unquote(u.fragment)   # %64 is d, %3B is ;
    if host.endswith("substack.com") and "/redirect/" in dpath:
        errs.append(f"{path}: a substack /redirect/ link embeds a subscriber token; link the post itself")
    if re.search(r";[^/]*=", dpath):
        errs.append(f"{path}: a path parameter (;name=value, e.g. ;jsessionid=) can carry a session; drop it")
    if any(c in dfrag for c in "=?&"):
        errs.append(f"{path}: the fragment {u.fragment!r} carries data, not just an anchor; drop it")
    allowed = ALLOWED_QUERY_KEYS.union(*(keys for h, keys in HOST_QUERY_KEYS.items()
                                         if host == h or host.endswith("." + h)))
    for seg in re.split(r"[&;]", u.query):
        k = urllib.parse.unquote_plus(seg.split("=", 1)[0])
        if seg and k not in allowed:
            errs.append(f"{path}: query key '{k}' is not allow-listed (ALLOWED_QUERY_KEYS); link the clean URL")
    return errs


def link_problems(entry, where):
    """Every URL in every string, whole-field or inside prose, meets the same rules."""
    errs = []
    for path, text in strings(entry, where):
        if re.match(r"^[a-z][a-z0-9+.-]*://[^ ]*$", text, re.I):
            # A URL field (no ASCII space) is checked whole, never a prefix of it; URL_RE then refuses any
            # other whitespace, so a non-breaking space cannot hide the rest of the URL.
            errs += url_problems(text, path)
            continue
        for m in URL_IN_TEXT.finditer(text):
            url = m.group(0)
            if (m.start(), m.end()) != (0, len(text)):
                url = url.rstrip(TRAILING)   # a sentence's own punctuation is not part of the link
            errs += url_problems(url, path)
    return errs


DATE_FIELDS = ("added", "date", "pushed", "reviewed_on")


def date_problems(entry, where):
    """The schemas check a date's shape; this checks it is a real calendar date (YYYY, YYYY-MM or YYYY-MM-DD)."""
    errs = []
    for k in DATE_FIELDS:
        v = entry.get(k)
        if not isinstance(v, str) or not re.fullmatch(r"\d{4}(-\d{2}){0,2}", v, re.ASCII):
            continue
        try:
            parts = [int(x) for x in v.split("-")]
            datetime.date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
        except ValueError:
            errs.append(f"{where}.{k}: {v!r} is not a real date")
    return errs


# ---- load ----------------------------------------------------------------------------------------

def load_entries(entries_dir, schemas):
    entries, seen, errs = {s: [] for s, _, _ in SECTIONS}, {}, []
    known = {s for s, _, _ in SECTIONS}
    for name in sorted(os.listdir(entries_dir)):   # nothing outside the four sections is silently skipped
        if name not in known and name != ".gitkeep":
            errs.append(f"{name}: unexpected entry path; entries live in {', '.join(sorted(known))}/")
    for sec in sorted(known - set(os.listdir(entries_dir))):
        errs.append(f"{sec}/: missing section directory (it holds a .gitkeep when empty)")
    for sec, _, _ in SECTIONS:
        d = os.path.join(entries_dir, sec)
        if not os.path.isdir(d):
            if os.path.exists(d):
                errs.append(f"{sec}: is not a directory")
            continue
        for name in sorted(os.listdir(d)):
            if name == ".gitkeep":
                continue
            if not name.endswith(".json") or not os.path.isfile(os.path.join(d, name)):
                errs.append(f"{os.path.join(sec, name)}: unexpected entry path; only <id>.json files belong here")
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
            problems = validate(e, schemas[sec], path) + link_problems(e, path) + date_problems(e, path)
            eid = e.get("id") if isinstance(e.get("id"), str) else None   # validate() already reported a bad type
            if eid is not None and eid != name[:-5]:
                problems.append(f"{path}: id {eid!r} differs from the file name")
            if isinstance(e.get("added"), str) and re.match(r"^\d{4}-\d{2}-\d{2}$", e["added"]):
                try:
                    # One day of slack: an author east of UTC writes today's date before UTC does.
                    if datetime.date.fromisoformat(e["added"]) > today() + datetime.timedelta(days=1):
                        problems.append(f"{path}: added {e['added']} is in the future")
                except ValueError:
                    problems.append(f"{path}: added {e['added']!r} is not a real date")
            if eid is not None and eid in seen:
                problems.append(f"{path}: id {eid!r} is also used by {seen[eid]}")
            if eid is not None:
                seen.setdefault(eid, path)
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


def fault(point):
    """Test seam: RH_FAULT=write|swap makes the build fail at that point (tests/run.py, F11c)."""
    if os.environ.get("RH_FAULT") == point:
        raise OSError(f"injected fault at {point}")


def write(path, text):
    global _writes
    _writes += 1
    if _writes == 3:
        fault("write")
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


def exchange(a, b):
    """Swap two paths in one atomic step (Linux renameat2 RENAME_EXCHANGE), so the live site is never
    missing. False where the kernel or filesystem cannot; the caller then falls back to two renames."""
    if os.environ.get("RH_FAULT", "").startswith("fallback"):   # test seam: exercise the two-rename path
        return False
    try:
        import ctypes
        fn = ctypes.CDLL(None, use_errno=True).renameat2
    except Exception:   # no ctypes, no libc symbol, or CDLL(None) unsupported (Windows): fall back
        return False
    if fn(-100, os.fsencode(a), -100, os.fsencode(b), 2) == 0:   # AT_FDCWD, RENAME_EXCHANGE
        return True
    err = ctypes.get_errno()
    if err in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
        return False
    raise OSError(err, os.strerror(err), b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--entries", default=os.path.join(ROOT, "entries"))
    ap.add_argument("--schemas", default=os.path.join(ROOT, "schemas"))
    ap.add_argument("--out", default=os.path.join(ROOT, "site"))
    a = ap.parse_args()
    out = os.path.abspath(a.out)
    parent = os.path.dirname(out)
    if os.path.islink(out):
        die(f"refusing to replace {out}: it is a symlink; point --out at the real directory")
    if not os.path.isdir(a.entries):
        die(f"entries directory {a.entries} does not exist; refusing to publish an empty site over the real one")
    schemas = load_schemas(a.schemas)
    entries = load_entries(a.entries, schemas)
    if os.path.exists(out) and not os.path.isfile(os.path.join(out, MARKER)):
        die(f"refusing to replace {out}: it is not a previous build (no {MARKER})")
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".site-", dir=parent)
    os.chmod(tmp, 0o755)   # mkdtemp makes it 0700
    try:
        render(entries, schemas, tmp)
        for r, ds, fs in os.walk(tmp):   # readable by a web server whatever the builder's umask
            for n in ds:
                os.chmod(os.path.join(r, n), 0o755)
            for n in fs:
                os.chmod(os.path.join(r, n), 0o644)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    try:
        fault("swap")
        swapped = os.path.exists(out) and exchange(tmp, out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if swapped:   # tmp now holds the previous site
        shutil.rmtree(tmp)
        print(f"built {sum(len(v) for v in entries.values())} entries into {out} (atomic swap)")
        return
    old = None
    if os.path.exists(out):
        old = tempfile.mkdtemp(prefix=".site-old-", dir=parent)
        try:
            fault("fallback-aside")
            os.rename(out, os.path.join(old, "site"))
        except BaseException:   # the old site never moved: drop the scaffolding and leave it be
            shutil.rmtree(old, ignore_errors=True)
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    try:
        fault("fallback-rename")
        os.rename(tmp, out)
    except BaseException:
        if old:   # put the previous site back before giving up
            os.rename(os.path.join(old, "site"), out)
            shutil.rmtree(old, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if old:
        shutil.rmtree(old)
    print(f"built {sum(len(v) for v in entries.values())} entries into {out}")


if __name__ == "__main__":
    main()
