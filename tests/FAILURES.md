# Failure list (written before build.py)

What the build promises: every published entry matches its section's schema, links only to
public primary sources, and appears in every output (its page, `index.json`, its own JSON,
`llms.txt`, `llms-full.txt`, `feed.xml`). What it does not promise: that an entry is true.
That is the review gate's job; the build only refuses what is malformed or unsafe to publish.

| # | Failure | Required behaviour | Test |
|---|---|---|---|
| F1 | entry file is not valid JSON | build exits non-zero naming the file | `bad-json` |
| F2 | a required field is missing | exit non-zero, names file and field | `missing-field` |
| F3 | wrong type, or a value outside an enum (claim, publication, focus) | exit non-zero | `bad-enum`, `bad-type` |
| F4 | an unknown field (a typo, or a private field riding along) | exit non-zero: schemas forbid extra fields | `extra-field` |
| F5 | `id` differs from the file name, or two entries share an id | exit non-zero | `id-mismatch`, `dup-id` |
| F6 | `section` differs from the directory | exit non-zero | `wrong-section` |
| F7 | a URL that is not https, or a redirect/tracker host (t.co, bit.ly, share.google, lnkd.in, buff.ly, a substack `/redirect/`) | exit non-zero: link the primary source | `tracker-url`, `http-url` |
| F8 | a URL carrying a token-like query key (token, key, sig, auth, email, subscriber, utm_*) | exit non-zero: those can embed a subscriber's identity | `token-url` |
| F9 | entry text containing HTML | rendered as text, never markup | `html-escaped` |
| F10 | a section with no entries | its page says so; every output still valid | `empty-section` |
| F11 | any failure part-way | the previous output directory is untouched | `no-partial` |
| F11b | a rebuild into an existing site leaves files of removed entries | the new site replaces the old one whole | `stale-removed` |
| F12 | `added` is not YYYY-MM-DD, or is in the future | exit non-zero (RH_NOW pins the clock) | `future-date` |
| F13 | an entry missing from an output | every id appears in its page, index.json, llms.txt, llms-full.txt, feed.xml and its own JSON | `all-outputs` |
| F14 | feed.xml or index.json malformed | both parse; feed newest first | `all-outputs` |
| F15 | output differs between two builds of the same input | byte-identical (sorted, no wall-clock stamps beyond RH_NOW) | `deterministic` |
| F16 | absolute or host-bound paths, or a CNAME file in the output | root-relative links only; no CNAME (the Pages API sets the domain) | `all-outputs` |
