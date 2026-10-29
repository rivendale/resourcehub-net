# resourcehub.net

A public, reviewed index of research papers, people to follow, open-source repo reviews and AI
demos. Built for people and for agents: every entry is also JSON, with `index.json`, `llms.txt`,
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
  `papers`, `people`, `repos`, `demos`.
- Fields: `schemas/common.schema.json` plus `schemas/<section>.schema.json`. Unknown fields fail the
  build. The merged schema for each section is published at `/schemas/<section>.schema.json`.
- `claim` says how far the central claim is checked: `verified` against the primary source, or a
  secondhand claim labelled `unverified`, `hype` or `opinion`.
- Repo reviews show **Reviewed** with a `focus` of `high`, `medium` or `low` against a stated need,
  not praise or a recommendation.
- Links are https and point at the primary source. Redirect and tracker hosts (t.co, bit.ly,
  share.google, lnkd.in, substack `/redirect/` links) and query keys that can identify a reader
  (`token`, `key`, `sig`, `auth`, `email`, `subscriber`, `utm_*`) fail the build.
- A link is a reference, not an endorsement. No personal byline.

## Build and test

```sh
python3 tests/run.py          # end-to-end tests, one per row of tests/FAILURES.md
python3 build.py --out site   # writes site/; a failure leaves the previous site untouched
```

Python standard library only.

## License

MIT ([LICENSE](LICENSE)). Linked works belong to their authors.
