# Failure list (written before build.py)

What the build promises: every published entry matches its section's schema, links only to
public primary sources, and appears in every output (its page, `index.json`, its own JSON,
`llms.txt`, `llms-full.txt`, `feed.xml`). What it does not promise: that an entry is true.
That is the review gate's job; the build only refuses what is malformed or unsafe to publish.

| # | Failure | Required behaviour | Test |
|---|---|---|---|
| F1 | entry file is not valid JSON | build exits non-zero naming the file | `bad-json` |
| F2 | a required field is missing | exit non-zero, names file and field | `missing-field` |
| F3 | wrong type (including `1` for `true`), a value outside an enum, or a control character in text | exit non-zero | `bad-enum`, `bad-type`, `const-is-typed`, `control-char` |
| F4 | an unknown field (a typo, or a private field riding along) | exit non-zero: schemas forbid extra fields | `extra-field` |
| F5 | `id` differs from the file name, or two entries share an id | exit non-zero | `id-mismatch`, `dup-id` |
| F6 | `section` differs from the directory | exit non-zero | `wrong-section` |
| F7 | a URL that is not https, or a redirect/tracker host (t.co, bit.ly, share.google, lnkd.in, buff.ly, a substack `/redirect/`), including with a trailing dot | exit non-zero: link the primary source | `tracker-url`, `trailing-dot-tracker`, `http-url` |
| F8 | a URL carrying a token-like query key (token, key, sig, auth, email, subscriber, utm_*, and substack's share id `r`) | exit non-zero: those can identify a reader | `token-url`, `substack-share-id` |
| F9 | entry text containing HTML | rendered as text, never markup | `html-escaped` |
| F10 | a section with no entries | its page says so; every output still valid | `empty-section` |
| F11 | any failure part-way | the previous output directory is untouched | `no-partial` |
| F11b | a rebuild into an existing site leaves files of removed entries | the new site replaces the old one whole | `stale-removed` |
| F12 | `added` is not YYYY-MM-DD, or is in the future | exit non-zero (RH_NOW pins the clock) | `future-date` |
| F13 | an entry missing from an output | every id appears in its page, index.json, llms.txt, llms-full.txt and its own JSON; feed.xml carries the newest 50 | `all-outputs`, `feed-newest-50` |
| F14 | feed.xml or index.json malformed | both parse; feed newest first | `all-outputs` |
| F15 | output differs between two builds of the same input | byte-identical (sorted, no wall-clock stamps beyond RH_NOW) | `deterministic` |
| F16 | absolute or host-bound paths, or a CNAME file in the output | root-relative links only; no CNAME (the Pages API sets the domain) | `all-outputs` |
| F17 | `--out` names a directory the build did not make (the repo root, a home dir) | refuse, touch nothing; only a dir carrying `.resourcehub-build` is replaced, and the result is world-readable | `refuse-foreign-out`, `site-readable` |
| F18 | a URL with credentials (`user:pass@`), a backslash, a percent-encoded or missing host, or a subdomain of a tracker host | exit non-zero: only a plain https URL with an ASCII host is published | `userinfo-url`, `backslash-url`, `pct-host`, `hostless-url`, `tracker-subdomain`, `tracker-www` |
| F19 | text with a character XML 1.0 forbids (controls incl. newline, U+FFFE/U+FFFF, lone surrogates) | exit non-zero, so feed.xml always parses | `trailing-newline`, `xml-nonchar`, `lone-surrogate` |
| F20 | `added` is today somewhere east of UTC | accepted: one day of slack; further ahead fails | `added-tomorrow-ok`, `future-date` |
| F11c | a failure while writing files, or between moving the old site aside and the new one in | the old site is restored whole; no stray temp dirs | `fault-write`, `fault-swap` |
| F21 | a title or sentence that starts with a word and a colon ("BERT: ...") | accepted: only `scheme://` is a URL | `colon-and-prose-url-ok` |
| F22 | a URL inside prose (a takeaway that cites a link) | checked by the same rules as a URL field | `prose-url-token`, `prose-url-tracker` |
| F23 | a share or click identifier in any case (`R=`, `fbclid`, `gclid`, `mc_eid`, YouTube `si`), or a port outside 1..65535 | exit non-zero | `substack-R-upper`, `click-id-*`, `share-id-si`, `bad-port` |
| F24 | `--entries` names a directory that does not exist | exit non-zero; the previous site is untouched (never an empty site over a real one) | `missing-entries-dir` |
| F25 | a URL field holding a quote or angle bracket, which a browser may read as userinfo or a separate token | the whole field is checked as one URL and refused | `quote-in-url-field`, `quote-userinfo` |
| F26 | an entry in a misspelt section directory, or a stray non-JSON file | exit non-zero: never silently left out | `misplaced-dir`, `stray-file` |
| F27 | `--entries` exists but holds no section directories | exit non-zero; previous site untouched | `no-section-dirs` |
