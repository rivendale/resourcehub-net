#!/usr/bin/env python3
"""End-to-end tests for build.py, one or more per row of tests/FAILURES.md. Written before build.py.

Each case writes an entries tree into a temp dir, runs the real build as a subprocess with RH_NOW
pinned, and checks the exit status and the files it wrote. Run: python3 tests/run.py
"""
import atexit
import copy
import ctypes
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = "2026-10-29T12:00:00Z"

BASE = {
    "papers": {"id": "ord-2026-intelligence-explosions", "section": "papers", "added": "2026-10-29",
               "reviewed_by": "laptop", "sources": ["https://arxiv.org/abs/2608.14426"], "agent_written": True,
               "title": "The Dynamics of Intelligence Explosions", "authors": ["Toby Ord"], "date": "2026-08-14",
               "link": "https://arxiv.org/abs/2608.14426", "takeaway": "A runaway takeoff is harder to reach than assumed.",
               "why_it_matters": "Generation time emerges as a key variable.", "key_result": "Singular growth needs strict conditions.",
               "publication": "preprint", "claim": "verified", "code": None, "tags": ["ai-forecasting"]},
    "people": {"id": "ethan-mollick", "section": "people", "added": "2026-10-28", "reviewed_by": "laptop",
               "sources": ["https://www.oneusefulthing.org/"], "agent_written": True, "name": "Ethan Mollick",
               "affiliation": "Wharton", "focus": "AI at work and in education",
               "links": {"newsletter": "https://www.oneusefulthing.org/"},
               "start_here": ["https://www.oneusefulthing.org/"], "why": "Practical, tested use of AI tools."},
    "repos": {"id": "example-tool", "section": "repos", "added": "2026-10-27", "reviewed_by": "laptop",
              "sources": ["https://github.com/example/tool"], "agent_written": True, "repo": "example/tool",
              "url": "https://github.com/example/tool", "license": "MIT", "language": "Python", "stars_at_review": 120,
              "pushed": "2026-10-01", "what_it_is": "A small CLI.", "reviewed": True, "focus": "low",
              "focus_reason": "Duplicates a tool already in use.", "measured_against": "A stated need for X.",
              "reviewed_on": "2026-10-27", "last_commit": "2026-09-30", "releases_last_year": 3,
              "latest_release": "v1.2.0 (2026-09-01)", "open_issues": 4, "tests_ci": "tests/ directory; CI runs them on each push.",
              "does_not_do": "Does not store credentials or call a hosted service."},
    "sources": {"id": "fred-api", "section": "sources", "added": "2026-10-29", "reviewed_by": "laptop",
                "sources": ["https://fred.stlouisfed.org/docs/api/fred/"], "agent_written": True, "name": "FRED API",
                "url": "https://fred.stlouisfed.org/docs/api/fred/", "subject": "macro-and-markets", "kind": "api",
                "provides": "Economic time series from the St. Louis Fed.", "access": "free-with-key",
                "last_checked": "2026-10-29", "link_check": "ok", "claim": "verified"},
    "demos": {"id": "demo-one", "section": "demos", "added": "2026-10-26", "reviewed_by": "laptop",
              "sources": ["https://www.youtube.com/watch?v=abc123"], "agent_written": False, "date": "2026-10-20",
              "title": "A demo", "link": "https://www.youtube.com/watch?v=abc123", "what_it_shows": "A capability.",
              "model_or_system": "Some model", "capability_tag": "coding", "claim": "opinion"},
}

results = []
# Every temp tree lives under one root, removed when the suite exits, however it exits.
TMP = tempfile.mkdtemp(prefix="rh-tests-")
atexit.register(shutil.rmtree, TMP, True)


def exchange_supported():
    """Whether renameat2(RENAME_EXCHANGE) works where the tests run (not on NFS or 9p, for example)."""
    try:
        fn = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError):
        return False
    a, b = tempfile.mkdtemp(dir=TMP), tempfile.mkdtemp(dir=TMP)
    return fn(-100, os.fsencode(a), -100, os.fsencode(b), 2) == 0


SECTIONS = ("sources", "papers", "people", "repos", "demos")


def write_tree(entries, raw=None, sections=SECTIONS):
    d = tempfile.mkdtemp(prefix="rh-entries-", dir=TMP)
    for s_ in sections:   # the real tree always has every section (each with .gitkeep)
        os.makedirs(os.path.join(d, s_), exist_ok=True)
    for e in entries:
        os.makedirs(os.path.join(d, e["section"]), exist_ok=True)
        with open(os.path.join(d, e["section"], e["id"] + ".json"), "w") as f:
            json.dump(e, f)
    for path, text in (raw or {}).items():
        os.makedirs(os.path.dirname(os.path.join(d, path)), exist_ok=True)
        with open(os.path.join(d, path), "w") as f:
            f.write(text)
    return d


def build(entries_dir, out=None, fault=None, umask=None):
    out = out or tempfile.mkdtemp(prefix="rh-out-", dir=TMP) + "/site"
    env = {**os.environ, "RH_NOW": NOW}
    if fault:
        env["RH_FAULT"] = fault
    p = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "build.py"), "--entries", entries_dir, "--out", out],
                       capture_output=True, text=True, env=env,
                       preexec_fn=(lambda: os.umask(umask)) if umask is not None else None)
    return p, out


def read(path):
    """A missing output reads as empty, so the check fails instead of crashing the run."""
    try:
        return open(path, encoding="utf-8").read()
    except OSError:
        return ""


def snap(o):
    return {os.path.relpath(os.path.join(r, f), o): open(os.path.join(r, f), "rb").read() for r, _, fs in os.walk(o) for f in fs}


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def all_valid():
    return [copy.deepcopy(v) for v in BASE.values()]


def refused(p, needle):
    """The build refused on purpose (its own message, no traceback), naming the needle."""
    return p.returncode != 0 and "refusing" in p.stderr and "Traceback" not in p.stderr and needle in p.stderr


def expect_fail(name, entries, needle, raw=None):
    p, _ = build(write_tree(entries, raw))
    check(name, refused(p, needle), f"rc={p.returncode} err={p.stderr.strip()[:160]}")


def mutate(section, **changes):
    es = all_valid()
    for e in es:
        if e["section"] == section:
            for k, v in changes.items():
                if v is KeyError:
                    e.pop(k)
                else:
                    e[k] = v
    return es


# F1
expect_fail("bad-json", all_valid(), "not valid JSON", raw={"papers/broken.json": "{not json"})
# F2
expect_fail("missing-field", mutate("papers", takeaway=KeyError), "takeaway")
# F3
expect_fail("bad-enum", mutate("papers", claim="likely"), "claim")
expect_fail("bad-type", mutate("repos", stars_at_review="120"), "stars_at_review")
# F4
expect_fail("extra-field", mutate("people", email="someone@example.com"), "email")
# F5
es = all_valid(); es[0]["id"] = "other-id"
d = write_tree([e for e in es if e["section"] != "papers"]); os.makedirs(f"{d}/papers", exist_ok=True)
json.dump(es[0], open(f"{d}/papers/ord-2026-intelligence-explosions.json", "w"))
p, _ = build(d); check("id-mismatch", refused(p, "other-id"), p.stderr.strip()[:160])
dup = copy.deepcopy(BASE["demos"]); dup["id"] = BASE["papers"]["id"]   # valid as a demo; only the id clashes
d = write_tree(all_valid(), raw={"demos/ord-2026-intelligence-explosions.json": json.dumps(dup)})
p, _ = build(d); check("dup-id", refused(p, "also used by"), p.stderr.strip()[:160])
# F6
es = all_valid(); wrong = copy.deepcopy(BASE["demos"]); wrong["id"] = "demo-two"
d = write_tree(es, raw={"people/demo-two.json": json.dumps(wrong)})
p, _ = build(d); check("wrong-section", refused(p, "but the file is under"), p.stderr.strip()[:160])
# F7, F8
expect_fail("tracker-url", mutate("papers", sources=["https://t.co/abc"]), "t.co")
expect_fail("substack-redirect", mutate("papers", link="https://example.substack.com/redirect/abc?j=xyz"), "redirect")
expect_fail("http-url", mutate("papers", link="http://arxiv.org/abs/2608.14426"), "link")
expect_fail("token-url", mutate("people", start_here=["https://example.com/post?token=abc"]), "token")
expect_fail("utm-url", mutate("demos", link="https://example.com/v?utm_source=x"), "utm_source")
# F12
expect_fail("trailing-dot-tracker", mutate("papers", sources=["https://t.co./abc"]), "t.co")
expect_fail("substack-share-id", mutate("papers", link="https://x.substack.com/p/post?r=1abcd"), "'r'")
expect_fail("const-is-typed", mutate("repos", reviewed=1), "reviewed")
expect_fail("control-char", mutate("papers", takeaway="a\u0001b"), "forbidden character")
expect_fail("userinfo-url", mutate("papers", link="https://user:secret@example.com/p"), "credentials")
expect_fail("backslash-url", mutate("papers", link="https://bit.ly\\abc"), "link")
expect_fail("pct-host", mutate("papers", link="https://t%2Eco/abc"), "link")
expect_fail("hostless-url", mutate("papers", link="https://?x"), "link")
expect_fail("tracker-subdomain", mutate("papers", sources=["https://maps.app.goo.gl/abc"]), "goo.gl")
expect_fail("tracker-www", mutate("papers", sources=["https://www.tinyurl.com/abc"]), "tinyurl.com")
expect_fail("trailing-newline", mutate("demos", capability_tag="coding\n"), "forbidden character")
expect_fail("xml-nonchar", mutate("papers", takeaway="x\uffff"), "forbidden character")
lone = json.dumps(BASE["papers"]).replace('"A runaway takeoff', '"\\ud800 A runaway takeoff')
expect_fail("lone-surrogate", [e for e in all_valid() if e["section"] != "papers"], "forbidden character",
            raw={f"papers/{BASE['papers']['id']}.json": lone})
p, _ = build(write_tree(mutate("papers", title="BERT: Pre-training of Deep Bidirectional Transformers vs Qwen2:7B",
                                 takeaway="Caveat: see https://arxiv.org/abs/1810.04805 or the mirror at https://arxiv.org.")))
check("colon-and-prose-url-ok", p.returncode == 0, p.stderr.strip()[:200])   # a word with a colon is not a URL
expect_fail("prose-url-token", mutate("papers", takeaway="Read https://example.com/post?token=abc first"), "token")
expect_fail("prose-url-tracker", mutate("people", why="Mostly posts via https://t.co/xyz links"), "t.co")
expect_fail("substack-R-upper", mutate("papers", link="https://x.substack.com/p/post?R=1abcd"), "'R'")
expect_fail("bad-port", mutate("papers", link="https://example.com:99999/p"), "port")
expect_fail("click-id-fbclid", mutate("demos", link="https://example.com/v?fbclid=abc"), "fbclid")
expect_fail("click-id-mc_eid", mutate("demos", link="https://example.com/v?mc_eid=abc"), "mc_eid")
expect_fail("share-id-si", mutate("demos", link="https://www.youtube.com/watch?v=abc123&si=XyZ"), "'si'")
expect_fail("quote-in-url-field", mutate("papers", link='https://example.com/"?token=secret'), "link")
expect_fail("quote-userinfo", mutate("papers", link='https://example.com"@t.co/x'), "link")
expect_fail("misplaced-dir", all_valid(), "unexpected", raw={"paper/foo.json": json.dumps(BASE["papers"])})
expect_fail("impossible-date", mutate("repos", pushed="2026-02-30"), "pushed")
expect_fail("fullwidth-date", mutate("repos", pushed="\uff12\uff10\uff12\uff16-\uff11\uff13-\uff14\uff15"), "pushed")
for key in ("api_key", "password", "auth_token", "session", "client_secret"):
    expect_fail(f"credential-key-{key}", mutate("papers", link=f"https://example.com/doc?{key}=abc"), key)
p, _ = build(write_tree(mutate("demos", link="https://www.youtube.com/watch?v=abc123&t=42#section-3")))
check("allowed-keys-ok", p.returncode == 0, p.stderr.strip()[:200])   # allow-listed keys and a plain anchor pass
for key in ("authToken", "clientSecret", "PHPSESSID", "author"):   # not allow-listed: refused, whatever the spelling
    expect_fail(f"unlisted-key-{key}", mutate("papers", link=f"https://example.com/doc?{key}=abc"), key)
expect_fail("custom-domain-substack-r", mutate("people", start_here=["https://www.oneusefulthing.org/p/post?r=1abcd"]), "'r'")
expect_fail("path-param-session", mutate("papers", link="https://example.com/doc;jsessionid=abc"), "path parameter")
expect_fail("fragment-key-value", mutate("papers", link="https://example.com/#state=xyz"), "fragment")
expect_fail("hash-route-token", mutate("papers", link="https://app.example.com/#/reset?token=abc"), "token")
expect_fail("nbsp-in-url-field", mutate("papers", link="https://example.com/p\u00a0?token=abc"), "link")
expect_fail("semicolon-token", mutate("papers", link="https://example.com/?x=1;token=private"), "token")
expect_fail("fullwidth-port", mutate("papers", link="https://example.com:\uff14\uff14\uff13/p"), "link")
p, _ = build(write_tree(mutate("papers", link="https://docs.python.org/3/library/hashlib.html#key-derivation",
                                 sources=["https://example.com/guide#auth", "https://example.com/rfc#session-management"])))
check("anchors-ok", p.returncode == 0, p.stderr.strip()[:200])   # an anchor is not a key=value pair
expect_fail("subscriber-id", mutate("papers", link="https://example.com/post?ck_subscriber_id=12345"), "ck_subscriber_id")
expect_fail("user-email", mutate("papers", link="https://example.com/post?user_email=a"), "user_email")
expect_fail("prose-nbsp-url", mutate("papers", takeaway="Read https://example.com/post\u00a0?token=abc first"), "takeaway")
expect_fail("pct-redirect", mutate("papers", link="https://x.substack.com/re%64irect/token"), "redirect")
expect_fail("pct-path-param", mutate("papers", link="https://example.com/doc%3Bjsessionid=secret"), "path parameter")
expect_fail("prose-quote-token", mutate("papers", takeaway='Read https://example.com/path"?access_token=secret first'), "takeaway")
p, _ = build(write_tree(mutate("papers", takeaway='The paper ("https://arxiv.org/abs/2608.14426") and <https://arxiv.org/abs/1810.04805>.')))
check("quoted-prose-url-ok", p.returncode == 0, p.stderr.strip()[:200])   # quotes and brackets around a URL are punctuation
p, _ = build(write_tree(mutate("people", links={"scholar": "https://scholar.google.com/citations?user=abc123&hl=en"})))
check("scholar-user-ok", p.returncode == 0, p.stderr.strip()[:200])   # user= is allowed on Google Scholar
expect_fail("x-share-t", mutate("people", links={"x": "https://x.com/someone/status/1?t=AbCdEfGh"}), "'t'")
expect_fail("user-off-scholar", mutate("papers", link="https://example.com/p?user=reader42"), "'user'")
bad_id = copy.deepcopy(BASE["papers"]); bad_id["id"] = ["x"]
expect_fail("unhashable-id", [e for e in all_valid() if e["section"] != "papers"], "id",
            raw={"papers/x.json": json.dumps(bad_id)})
p, _ = build(write_tree(mutate("demos", link="https://youtu.be/abc123?list=PLx&index=2&t=30")))
check("youtu-be-list-ok", p.returncode == 0, p.stderr.strip()[:200])
expect_fail("glued-tracker", mutate("papers", takeaway="See https://a.com/x,https://t.co/y for more"), "t.co")
expect_fail("glued-credentials", mutate("papers", link="https://example.com/a,https://user:secret@other.com/b"), "credentials")
expect_fail("glued-http", mutate("papers", takeaway="See https://a.com/x,http://b.com/y here"), "not https")
expect_fail("glued-trailing-dot-tracker", mutate("papers", takeaway="See https://a.com/x,https://t.co./y here"), "t.co")
p, _ = build(write_tree(mutate("papers", sources=["https://web.archive.org/web/2020/https://example.com/x"])))
check("archive-url-ok", p.returncode == 0, p.stderr.strip()[:200])   # an archived URL inside a URL is legitimate
expect_fail("hash-route-click-id", mutate("demos", link="https://example.com/#/watch?fbclid=abc"), "fbclid")
expect_fail("hash-query-token", mutate("papers", link="https://app.example.com/#?access_token=abc"), "access_token")
expect_fail("impossible-month", mutate("papers", date="2026-13"), "date")
expect_fail("fragment-token", mutate("papers", link="https://app.example.com/#access_token=abc"), "access_token")
p, _ = build(write_tree(mutate("papers", takeaway="https://arxiv.org/abs/2608.14426 argues takeoff is slower.")))
check("prose-starting-with-url-ok", p.returncode == 0, p.stderr.strip()[:200])
d = write_tree([e for e in all_valid() if e["section"] != "papers"], sections=("sources", "people", "repos", "demos"))
open(os.path.join(d, "papers"), "w").write("not a directory")
p, _ = build(d); check("section-is-a-file", refused(p, "is not a directory"), p.stderr.strip()[:160])
p, out = build(write_tree(all_valid()), umask=0o077)
modes = {(os.stat(os.path.join(r, n)).st_mode & 0o777) for r, ds, fs in os.walk(out) for n in ds + fs} if p.returncode == 0 else {0}
check("readable-under-umask-077", p.returncode == 0 and modes <= {0o755, 0o644}, str(sorted(oct(m) for m in modes)))
expect_fail("stray-file", all_valid(), "unexpected", raw={"papers/notes.txt": "x"})
p, out = build(write_tree(all_valid()))
before = snap(out) if p.returncode == 0 else None
p2, _ = build(write_tree([], sections=()), out)
check("no-section-dirs", refused(p2, "missing section directory") and snap(out) == before, p2.stderr.strip()[:160])
p, out = build(write_tree(all_valid()))
before = snap(out) if p.returncode == 0 else None
p2, _ = build("/nonexistent/entries-dir", out)
check("missing-entries-dir", refused(p2, "does not exist") and snap(out) == before, p2.stderr.strip()[:160])
p, _ = build(write_tree(mutate("demos", added="2026-10-30")))
check("added-tomorrow-ok", p.returncode == 0, p.stderr.strip()[:160])   # one day of slack for authors east of UTC
expect_fail("future-date", mutate("demos", added="2026-11-30"), "future")
expect_fail("bad-date", mutate("demos", added="2026/10/26"), "added")

# F9, F13, F14, F16: a good build
es = all_valid(); es[0]["takeaway"] = "<script>alert(1)</script> & more"
p, out = build(write_tree(es))
check("good-build", p.returncode == 0, p.stderr.strip()[:200])
try:
    page = open(f"{out}/papers/index.html").read()
    check("html-escaped", "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page)
    idx = json.load(open(f"{out}/index.json"))
    ids = {e["id"] for e in idx["entries"]}
    check("all-outputs:index", ids == {e["id"] for e in es}, str(ids))
    llms, full = open(f"{out}/llms.txt").read(), open(f"{out}/llms-full.txt").read()
    feed = ET.parse(f"{out}/feed.xml").getroot()
    items = feed.findall("./channel/item")
    for e in es:
        check(f"all-outputs:{e['id']}",
              os.path.isfile(f"{out}/entries/{e['section']}/{e['id']}.json")
              and e["id"] in open(f"{out}/{e['section']}/index.html").read()
              and f"/entries/{e['section']}/{e['id']}.json" in llms and e["id"] in full
              and any(e["id"] in (i.findtext("guid") or "") for i in items))
    dates = [i.findtext("pubDate") for i in items]
    check("feed-newest-first", [i.findtext("guid") for i in items][0].endswith("ord-2026-intelligence-explosions.json"), str(dates))
    check("no-cname", not os.path.exists(f"{out}/CNAME"))
    import re as _re
    hrefs = [h for r, _, fs in os.walk(out) for f in fs if f.endswith(".html")
             for h in _re.findall(r'(?:href|src)="([^"]*)"', open(os.path.join(r, f)).read())]
    bad = [h for h in hrefs if not (h.startswith("/") or h.startswith("https://") or h.startswith("#"))]
    check("root-relative", hrefs and not bad and any(h.startswith("/") for h in hrefs), str(bad[:5]))
    for s in SECTIONS:
        sch = json.load(open(f"{out}/schemas/{s}.schema.json"))
        check(f"merged-schema:{s}", "id" in sch["properties"] and "added" in sch["required"])
    # F52: the data-only disclaimer is on every page and in every machine-readable index
    disclaimer = "Data only"
    pages = [os.path.join(r, f) for r, _, fs in os.walk(out) for f in fs if f.endswith(".html")]
    check("disclaimer-every-page", pages and all(disclaimer in read(p_) for p_ in pages),
          str([p_ for p_ in pages if disclaimer not in read(p_)][:3]))
    check("disclaimer-index-json", disclaimer in idx.get("disclaimer", ""), str(idx.get("disclaimer"))[:80])
    check("disclaimer-llms", disclaimer in llms and disclaimer in full and disclaimer in read(f"{out}/feed.xml"))
    # F53: the sources page filters by subject and access with a same-origin script only
    src_page = read(f"{out}/sources/index.html")
    scripts = _re.findall(r"<script\b[^>]*>", src_page)
    check("filter-controls", 'name="subject"' in src_page and 'name="access"' in src_page and 'type="search"' in src_page
          and 'value="macro-and-markets"' in src_page and 'value="free-with-key"' in src_page
          and 'data-subject="macro-and-markets"' in src_page and 'data-access="free-with-key"' in src_page, src_page[:200])
    css = read(f"{out}/style.css")   # an author display rule beats the UA's [hidden]; the stylesheet must restore it
    check("filter-hidden-without-js", '<form id="filter" class="filter" role="search" hidden>' in src_page
          and "form.filter[hidden]{display:none}" in css, css[-200:])
    check("no-external-script", scripts and all(_re.search(r'src="/[^/]', s_) for s_ in scripts)
          and os.path.isfile(f"{out}/filter.js") and not _re.search(r"\son[a-z]+=", src_page), str(scripts))
except Exception as x:  # a missing output must read as a failure, not crash the run
    check("good-build:outputs", False, repr(x)[:200])

# F46-F51: sources and repo-review rules
expect_fail("source-bad-access", mutate("sources", access="freemium"), "access")
expect_fail("source-bad-subject", mutate("sources", subject="stocks"), "subject")
expect_fail("source-bad-kind", mutate("sources", kind="website"), "kind")
expect_fail("source-missing-last-checked", mutate("sources", last_checked=KeyError), "last_checked")
expect_fail("source-impossible-last-checked", mutate("sources", last_checked="2026-02-30"), "last_checked")
expect_fail("source-future-last-checked", mutate("sources", last_checked="2026-11-30"), "future")
expect_fail("source-stars-undated", mutate("sources", stars=184), "counted_on")
expect_fail("source-likes-undated", mutate("sources", likes=12), "counted_on")
expect_fail("source-blocked-unexplained", mutate("sources", link_check="bot-blocked"), "link_note")
expect_fail("source-blocked-verified", mutate("sources", link_check="timeout", link_note="Timed out."), "claim")
p, _ = build(write_tree(mutate("sources", stars=184, counted_on="2026-10-29", license="Apache-2.0",
                               link_check="bot-blocked", link_note="Returns 403 to automated checks.", claim="unverified",
                               access_note="Unofficial; read the provider's terms.")))
check("source-optional-fields-ok", p.returncode == 0, p.stderr.strip()[:200])
# F55: sources whose main use is searching people by name are refused; official designation lists pass
for host_url in ("https://opencorporates.com/", "https://www.opensanctions.org/search", "https://adviserinfo.sec.gov/"):
    expect_fail(f"people-search-{host_url.split('/')[2]}", mutate("sources", url=host_url, sources=[host_url]), "people by name")
p, _ = build(write_tree(mutate("sources", url="https://ofac.treasury.gov/sanctions-list-service", subject="sanctions-and-kyc",
                               sources=["https://ofac.treasury.gov/sanctions-list-service"])))
check("designation-list-ok", p.returncode == 0, p.stderr.strip()[:200])
# F55b: the people guard reads every URL in every section, not only a source's url and sources
expect_fail("people-search-paper-link", mutate("papers", link="https://opencorporates.com/officers/x"), "people by name")
expect_fail("people-search-paper-code", mutate("papers", code="https://www.opensanctions.org/datasets/"), "people by name")
expect_fail("people-search-people-link", mutate("people", links={"site": "https://adviserinfo.sec.gov/"}), "people by name")
expect_fail("people-search-prose-url", mutate("repos", focus_reason="Compare https://nansen.ai/ labels."), "people by name")
for i, prose in enumerate(("See https://nansen.ai, which labels wallets.", "Compare (https://nansen.ai).", "Read https://opencorporates.com; then stop.")):
    expect_fail(f"people-search-punctuated-{i}", mutate("repos", focus_reason=prose), "people by name")
# F50b: every form of "recommend" is a recommending word; a word that only contains it is not
expect_fail("ranking-word-recommend", mutate("sources", provides="We recommend this feed."), "ranking")
expect_fail("ranking-word-recommends", mutate("repos", focus_reason="The team recommends it."), "ranking")
expect_fail("ranking-word-recommendation", mutate("sources", access_note="Our recommendation: a key."), "ranking")
expect_fail("ranking-word-recommending", mutate("repos", does_not_do="Recommending tickers."), "ranking")
p, _ = build(write_tree(mutate("sources", provides="Training data for a recommender system.")))
check("ranking-word-recommender-ok", p.returncode == 0, p.stderr.strip()[:200])   # a longer word that contains it passes
expect_fail("ranking-word-source", mutate("sources", provides="The best source for rates."), "ranking")
expect_fail("ranking-word-repo", mutate("repos", focus_reason="A leading library for this."), "ranking")
expect_fail("ranking-word-top", mutate("sources", access_note="Top tier needs a key."), "ranking")
p, _ = build(write_tree(mutate("sources", provides="Laptop-friendly bulk files; bestowed by statute.")))
check("ranking-word-substring-ok", p.returncode == 0, p.stderr.strip()[:200])
expect_fail("repo-missing-does-not-do", mutate("repos", does_not_do=KeyError), "does_not_do")
expect_fail("repo-impossible-last-commit", mutate("repos", last_commit="2026-02-30"), "last_commit")
expect_fail("repo-negative-releases", mutate("repos", releases_last_year=-1), "releases_last_year")
expect_fail("repo-future-last-commit", mutate("repos", last_commit="2026-11-30"), "future")
expect_fail("repo-releases-without-latest", mutate("repos", latest_release=None), "latest_release")
p, out = build(write_tree(mutate("repos", releases_last_year=0, latest_release=None, open_issues=1)))
check("repo-no-releases-ok", p.returncode == 0 and "1 open issue ·" in read(f"{out}/repos/index.html"), p.stderr.strip()[:200])

# F54: sources list alphabetically within each subject (never by date, which would read as a ranking)
srcs = []
for i, (subj, name) in enumerate([("tax", "b-tax"), ("macro-and-markets", "Zeta"), ("macro-and-markets", "alpha"), ("tax", "A-tax")]):
    e = copy.deepcopy(BASE["sources"]); e.update(id=f"src-{i}", subject=subj, name=name, added=f"2026-10-{20 + i}")
    srcs.append(e)
p, out = build(write_tree(srcs))
p1, out1 = build(write_tree(all_valid()))
check("one-source-singular", p1.returncode == 0 and ">1 source<" in read(f"{out1}/sources/index.html"), p1.stderr.strip()[:160])
order = re.findall(r'<article id="(src-\d)"', read(f"{out}/sources/index.html"))
check("sources-alphabetical", p.returncode == 0 and order == ["src-2", "src-1", "src-3", "src-0"], str(order))

# F10
p, out = build(write_tree([copy.deepcopy(BASE["papers"])]))
check("empty-section", p.returncode == 0 and "No entries yet" in read(f"{out}/demos/index.html"), p.stderr.strip()[:160])

# F11
d_good, d_bad = write_tree(all_valid()), write_tree(mutate("papers", claim="likely"))
p1, out = build(d_good)
before = snap(out) if p1.returncode == 0 else None
p2, _ = build(d_bad, out)
after = snap(out) if os.path.isdir(out) else None
check("no-partial", p1.returncode == 0 and p2.returncode != 0 and before == after)

# F11b: a rebuild into an existing site drops an entry that was removed (no stale files)
d1 = write_tree(all_valid())
d2 = write_tree([e for e in all_valid() if e["section"] != "demos"])
p1, out = build(d1); p2, _ = build(d2, out)
check("stale-removed", p1.returncode == 0 and p2.returncode == 0
      and not os.path.exists(f"{out}/entries/demos/demo-one.json") and os.path.isfile(f"{out}/papers/index.html"))

# F17: never replace a directory the build did not make
foreign = tempfile.mkdtemp(prefix="rh-foreign-", dir=TMP); open(f"{foreign}/keep.txt", "w").write("x")
p, _ = build(write_tree(all_valid()), foreign)
check("refuse-foreign-out", p.returncode != 0 and "previous build" in p.stderr and os.path.isfile(f"{foreign}/keep.txt"), p.stderr.strip()[:160])
p, out = build(write_tree(all_valid()))
check("site-readable", p.returncode == 0 and (os.stat(out).st_mode & 0o777) == 0o755 and os.path.isfile(f"{out}/.resourcehub-build"),
      oct(os.stat(out).st_mode & 0o777) if os.path.isdir(out) else "no out")

# F13b: the feed carries the newest 50; every other output carries every entry
many = []
for i in range(55):
    e = copy.deepcopy(BASE["papers"]); e["id"] = f"paper-{i:02d}"; e["added"] = f"2026-09-{(i % 28) + 1:02d}"
    many.append(e)
p, out = build(write_tree(many))
if p.returncode == 0:
    items = ET.parse(f"{out}/feed.xml").getroot().findall("./channel/item")
    check("feed-newest-50", len(items) == 50 and len(json.load(open(f"{out}/index.json"))["entries"]) == 55
          and all(os.path.isfile(f"{out}/entries/papers/paper-{i:02d}.json") for i in range(55)), f"items={len(items)}")
else:
    check("feed-newest-50", False, p.stderr.strip()[:160])

# F11c: a failure while writing, or between the two renames, leaves the old site whole and no stray dirs
for point in ("write", "swap"):
    d = write_tree(all_valid()); p1, out = build(d)
    before = snap(out) if p1.returncode == 0 else None
    p2, _ = build(d, out, fault=point)
    stray = [n for n in os.listdir(os.path.dirname(out)) if n.startswith(".site")]
    check(f"fault-{point}", p1.returncode == 0 and p2.returncode != 0 and snap(out) == before and not stray,
          f"rc={p2.returncode} stray={stray}")

# F11d: on Linux a rebuild swaps the old and new site in one atomic exchange, so there is no instant with
# the site missing and nothing is left beside it
d = write_tree(all_valid()); p1, out = build(d); p2, _ = build(d, out)
leftover = [n for n in os.listdir(os.path.dirname(out)) if n.startswith(".site")]
check("atomic-swap", p1.returncode == 0 and p2.returncode == 0 and not leftover
      and ("atomic swap" in p2.stdout) == exchange_supported(), f"stdout={p2.stdout.strip()[:120]} leftover={leftover}")
# the two-rename fallback (non-Linux, or no RENAME_EXCHANGE) restores the old site if the second rename fails
d = write_tree(all_valid()); p1, out = build(d)
before = snap(out) if p1.returncode == 0 else None
p2, _ = build(d, out, fault="fallback-rename")
stray = [n for n in os.listdir(os.path.dirname(out)) if n.startswith(".site")]
check("fallback-rollback", p1.returncode == 0 and p2.returncode != 0 and snap(out) == before and not stray, f"stray={stray}")
# ...and leaves nothing behind if moving the old site aside fails
d = write_tree(all_valid()); p1, out = build(d)
before = snap(out) if p1.returncode == 0 else None
p2, _ = build(d, out, fault="fallback-aside")
stray = [n for n in os.listdir(os.path.dirname(out)) if n.startswith(".site")]
check("fallback-aside-cleanup", p1.returncode == 0 and p2.returncode != 0 and snap(out) == before and not stray, f"stray={stray}")
# a symlinked --out is refused rather than swapped as a link
real = write_tree(all_valid()); p1, target = build(real)
link = tempfile.mkdtemp(prefix="rh-link-", dir=TMP) + "/site"; os.symlink(target, link)
p2, _ = build(real, link)
check("symlink-out-refused", refused(p2, "symlink") and os.path.islink(link), p2.stderr.strip()[:160])

# F15
d = write_tree(all_valid())
p1, o1 = build(d); p2, o2 = build(d)
def snapshot(o):
    return {os.path.relpath(os.path.join(r, f), o): open(os.path.join(r, f), "rb").read() for r, _, fs in os.walk(o) for f in fs}
check("deterministic", p1.returncode == 0 and p2.returncode == 0 and snapshot(o1) == snapshot(o2))

failed = [r for r in results if not r[1]]
for name, ok, detail in results:
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {detail}"))
print(f"{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
