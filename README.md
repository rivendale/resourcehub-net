# resourcehub.net

A public, reviewed directory of data sources, research papers, people to follow, open-source repo
reviews and AI demos, across any subject. Built for people and for agents: every entry is also JSON, with `index.json`, `llms.txt`,
`llms-full.txt` and an RSS feed of the newest 50.

## How an entry gets here

1. An agent drafts it from the primary source (the paper, the repo, the person's own work), never
   from a post about it, and opens a pull request adding `entries/<section>/<id>.json`.
2. A second agent reviews the pull request cold: the links resolve to public primary sources, the
   claim matches the source, and nothing private rides along.
3. Merging publishes it. CI runs the tests and the build on every pull request, so a malformed or
   unsafe entry fails before review.

## Entry rules

- One file per entry, `entries/<section>/<id>.json`, where `<id>` is the file name. Sections:
  `sources`, `papers`, `people`, `repos`, `demos`.
- Fields: `schemas/common.schema.json` plus `schemas/<section>.schema.json`. Unknown fields fail the
  build. The merged schema for each section is published at `/schemas/<section>.schema.json`.
- `claim` says how far the central claim is checked: `verified` against the primary source, or a
  secondhand claim labelled `unverified`, `hype` or `opinion`.
- Repo reviews show **Reviewed** with a `focus` of `high`, `medium` or `low` against a stated need,
  not praise or a recommendation, with the facts it rests on: licence, last commit, releases in the
  year, open issues, dated stars, tests and CI, and what the project does not do.
- Sources (official feeds, APIs, datasets, models, standards, references) carry a `subject`, a `kind`,
  an `access` label (`free`, `free-with-key`, `key-or-paid`, `paid`, `membership`, `restricted`), what
  they provide in our own words, and `last_checked`: the US Eastern date the link was opened and the
  entry checked against the page. A page that blocks automated checks or times out is kept only when
  otherwise known-good, with a `link_note`, and never as `verified`. Stars and likes are dated
  snapshots (`counted_on`), not scores. The page lists sources alphabetically within each subject.
- People: official government designation lists (sanctions, exclusions) may be listed, described as
  such; a source whose main use is searching people by name may not. Hosts held under this line are
  in build.py `PEOPLE_SEARCH_HOSTS`; any URL to one, in any field of any section, fails the build.
- Neutral wording: our own prose never ranks or recommends. In sources and repo reviews, "best",
  "leading", "top" and similar words fail the build (FAILURES.md F50); elsewhere review enforces it.
  Nothing here is investment, tax or legal advice; every page and index carries that disclaimer.
- Links are plain https URLs pointing at the primary source. A query string may carry only
  allow-listed keys (`id`, `p`, `page`, `q`, `lang`, `hl`, `tab` anywhere; `v`, `t`, `list`, `index`
  on YouTube (`t`, `list`, `index` on youtu.be); `user` on Google Scholar); a
  fragment may only be a plain `#anchor`; no path parameters (`;name=value`), credentials, or
  redirect and tracker hosts (t.co, bit.ly, share.google, lnkd.in and the like). Anything else fails
  the build: link the clean URL.
- A link is a reference, not an endorsement. No personal byline.

## Build and test

```sh
python3 tests/run.py          # end-to-end tests, one per row of tests/FAILURES.md
python3 build.py --out site   # writes site/; a failure leaves the previous site untouched
```

Python standard library only.

## License

MIT ([LICENSE](LICENSE)). Linked works belong to their authors.
