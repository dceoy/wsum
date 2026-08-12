# Normalization, scoring, and retry policy

## Formats

Detect content by bytes and declared media type, independently of the URL extension.
Reject recognized type mismatches.

- HTML: remove executable and layout noise, apply the supported strict selector
  subset, preserve headings, paragraphs, lists, tables, prices, specifications, and
  terms, normalize Unicode with NFKC, normalize whitespace, and deduplicate repeated
  lines.
- PDF: accept bounded, unencrypted, text-based PDFs with supported text streams.
  Reject encrypted, malformed, oversized, excessive-object, image-only, and
  no-text files. OCR is out of scope.
- RSS/Atom: reject DTD/entity declarations, bound entry count, normalize stable IDs,
  titles, links, publication metadata, and content, then sort by stable ID so
  reordering does not change the hash.
- Plain text: normalize encoding, line endings, Unicode, and whitespace.

Accept UTF-8, UTF-8 with BOM, Shift JIS/CP932, EUC-JP, ISO-8859-1, and
Windows-1252 text encodings. Reject other declared encodings. Require identity HTTP
content encoding so the byte cap applies to the exact parser input.

Use normalization version `2026-01` and calculate
`SHA256(version + "\n" + kind + "\n" + normalized_text)`.

The selector grammar supports tag, `#id`, `.class`, `[attribute]`,
`[attribute=value]`, and descendant selectors. Reject pseudo-selectors,
comma groups, sibling/child combinators, malformed selectors, configured selectors
with no match, and empty extraction.

## Diff bounds

Use line-level `SequenceMatcher` with auto-junk disabled. Keep one context line by
default, at most 30 sections, and at most 12,000 changed-context characters.
Include stable section IDs and the nearest heading or feed entry anchor. Mark
truncation explicitly.

`SequenceMatcher` with auto-junk disabled has a quadratic worst case for both
heavily repeated lines and adversarial permutations of unique lines. Before
diffing, conservatively bound that work by the product of the before/after line
counts and short-circuit to the same budget-exceeded result as the line-count cap
(`diff_budget_exceeded`, a synthetic document-level section, fail-closed) when it
exceeds `max_diff_complexity`.

When the retained section budget (30 sections / 12,000 characters) cannot hold
every changed section, prioritize sections that match a scoring signal (price,
specification, terms, availability, eligibility) over sections that do not, so a
truncated evidence set is never missing the content that drove the score. If even
the signal-bearing sections do not all fit, mark `signal_section_truncated`: the
routine must not trust a `material=false` verdict made over that incomplete
evidence to advance the baseline (see routine-setup.md).

Equal hashes return `unchanged`. A missing previous snapshot returns
`baseline_created` and never notifies.

## Scoring

Start with up to 60 points from changed-character ratio. Add deterministic weights:

| Signal                                  | Points |
| --------------------------------------- | -----: |
| Price                                   |     30 |
| Contractual terms or policy             |     30 |
| Availability                            |     25 |
| Eligibility or application requirements |     25 |
| Specifications                          |     20 |
| Rewrite of at least 65%                 |     20 |

Cap at 100. Cap obvious date/counter-only noise at 15. Scores below 35 are `minor`;
35–69 are `moderate` candidates; 70–100 are `high` candidates. Only candidates may
invoke Claude. Claude can still return `material=false`.

Change thresholds and weights through validated `DiffConfig` values, not source
edits.

## Resource and retry defaults

- Static timeout: 15 seconds per socket operation (connect, header read, or a
  single body-read chunk), bounded by a 60 second total wall-clock deadline
  across connect, redirects, and all body reads for one `fetch_url` call. The
  per-op timeout alone cannot stop a server that trickles single bytes just
  before each timeout; the total deadline can.
- Redirects: 5 maximum.
- Fetched response: 5 MB maximum.
- Loaded normalized snapshot: 10 MB maximum.
- Browser timeout: 30 seconds.
- Browser requests: 100 maximum.
- Rendered DOM: 5 MB maximum.
- Retry attempts: 3.
- Backoff: 1, 2, then at most 10 seconds; no random jitter so tests and audit are
  deterministic.
- Concurrency: 2 by default, 4 maximum.

Retry connection failures, timeouts, rate limits, server failures, and temporary
connector failures. Do not retry policy denial, malformed URLs/responses, unsupported
content, parser failures, selector drift, client errors, invalid summaries, or
ambiguous successful delivery.
