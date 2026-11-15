# Scanners v0 (step S3)

Automated detection is incomplete; these scanners are a second line of defence behind default-deny classification and do not guarantee that no secret or personal data leaves.

A scanner is a callable `bytes -> list[str]`. It returns finding **names** only (for example `secret:aws_access_key`); matched text is never returned, logged or echoed in a block notice. Plug them in with `Gateway(..., scanners=default_scanners(policy), blocking=BLOCKING)`. Without `blocking`, every finding blocks.

## What each scanner detects

| Module | Findings | Notes |
|---|---|---|
| `scanners/secrets.py` | `secret:aws_access_key`, `github_token`, `slack_token`, `stripe_key`, `google_api_key`, `openai_key`, `anthropic_key`, `jwt`, `private_key`, `generic_assignment`, `high_entropy` | Known formats by regex. `generic_assignment` is `password/passwd/secret/token/api_key = <8+ chars>`, except placeholders (`changeme`, `example`, `your_...`, `xxx...`, `<...>`, `${...}`, `{{...}}`, one repeated character) and code that merely reads a value (`os.environ[...]`, `f(...)`, `self.x`). `high_entropy` is a token of 32+ characters from `[A-Za-z0-9+/_-]` (with `=` padding) that has a letter and a digit and Shannon entropy >= 4.0 bits/char; hex hashes of 32/40/56/64/96/128 characters, UUIDs and lockfile integrity strings (`sha256-`, `sha384-`, `sha512-` plus base64) are exempt. Zero-width characters are stripped first. One decode level is also scanned (base64 tokens of 24+ characters that decode to mostly printable ASCII, percent-encoding, `\uXXXX` and `\xXX`); those findings carry the suffix ` (decoded)`. |
| `scanners/pii.py` | `pii:email`, `pii:phone`, `pii:postal_address`, `pii:payment_card`, `pii:tracking_number`, `pii:order_id`, `pii:combination` | Shapes only. Email skips `example.*`, `test.*`, `localhost`, reserved TLDs and image-file look-alikes. Phone needs a `+CC` form or separators; bare digit runs, version strings, ISO dates and 555-01xx numbers are ignored. Address is number + capitalised street name + street type, or a postal code within 40 characters of address/ship/postal/zip. Card is 13-19 digits that pass Luhn and start like a major card network. Tracking: UPS `1Z`, USPS 20-22 digits starting 92-95, FedEx 12 or 15 digits only near tracking/fedex/shipment. Order id needs a label word, then an id of 5+ alphanumerics with a digit. `pii:combination` is added when two or more distinct `pii:` kinds appear. |
| `scanners/structured.py` | `data:sqlite`, `data:sql_dump`, `data:archive`, `data:env_file`, `data:tabular_personal` | Content sniffing, independent of file extension. `tabular_personal` is a CSV header or JSON keys that match two or more of name, first_name, last_name, email, phone, address, city, postal, zip, order, customer (substring match, so `first_name` alone counts as two). |
| `scanners/terms.py` | `term:block#i`, `term:review#i`, `term:signal#i` | Deny-list terms from `policy.toml` (`[[term]]`: `term`, `kind`, `match`, `action`). Match modes: `exact`, `case_insensitive`, `word`, `fuzzy` (lower-cased, zero-width stripped, leetspeak and a small Cyrillic/Greek homoglyph table folded, separators between letters ignored). Findings name only the term's index and action. Terms shorter than 3 characters are ignored and listed in the scanner's `skipped` attribute (indexes only). The optional `scope` field is not evaluated in v0 (scanners see only bytes), which can only over-block. An unknown action is treated as `block` and an unknown match mode as `fuzzy`. |

## Blocking rule

`BLOCKING(findings) -> blocking findings` takes every finding of one payload (all segments). Everything blocks, except `term:signal#i` on its own: it is recorded in the transmission log but does not block. A signal together with any `pii:` finding blocks. `term:review#i` blocks and the notice is "held for user review". Non-blocking findings are written to `scanner_json` of the transmission row.

## Measured corpus results

From `pytest -s tests/test_scanners_corpus.py`. The corpus is synthetic (seeded, generated at runtime, invented data): 8-13 positives per category, each tried plain and in transformed form, plus 63 benign look-alikes. Recall is the share of samples whose specific expected finding was produced (the `(decoded)` form counts).

```
RECALL per (category, transformation); n = samples per cell
category         n plain base64 urlenc unicode_escape zero_width spaced leet reversed split_concat rot13  hex gzip_b64
secret_formats  13  1.00   1.00   1.00           1.00       1.00    n/a  n/a     0.00         0.00  0.00 0.00     0.00
entropy         10  1.00   1.00   1.00           1.00       1.00    n/a  n/a     1.00         0.00  1.00 0.00     1.00
email            9  1.00   1.00   1.00           1.00       1.00    n/a  n/a     0.22         0.00  1.00 0.00     0.00
phone           10  1.00   1.00   1.00           1.00       1.00    n/a  n/a     0.00         0.00  1.00 0.00     0.00
address         10  1.00   1.00   1.00           1.00       1.00    n/a  n/a     0.00         0.00  0.00 0.00     0.00
card             8  1.00   1.00   1.00           1.00       1.00    n/a  n/a     0.25         0.00  1.00 0.00     0.00
tracking         9  1.00   1.00   1.00           1.00       1.00    n/a  n/a     0.00         0.00  0.44 0.00     0.00
order_id         8  1.00   1.00   1.00           1.00       1.00    n/a  n/a     0.00         0.00  0.00 0.00     0.00
structured      12  1.00   0.00   0.00           0.00       0.08    n/a  n/a     0.00         0.00  0.08 0.00     0.00
terms           12  1.00   0.00   0.00           0.00       1.00   0.25 0.25     0.00         0.00  0.00 0.00     0.00
false-block rate on benign: 2/63 = 0.032
```

Reading the table honestly:

- Only plain recall (>= 0.90 per category), base64 / URL-encoded / unicode-escaped / zero-width recall for secrets (>= 0.75) and the benign false-block rate (<= 0.10) are asserted. Every other cell is recorded, not tuned for.
- Structured-content and deny-list term scanners do not decode: a database dump or a deny-list term inside base64 or percent-encoding is not found (0.00 columns). Protection there rests on labels, not detection.
- The `entropy` row survives reversal, ROT13 and gzip+base64 only because any long random-looking token trips the entropy rule; the named formats in `secret_formats` do not survive them. Entropy is a generic catch, not an identification.
- The `spaced` and `leet` columns apply to `terms` only and only the `fuzzy` terms (3 of 12) can match them.
- Two benign samples are false blocks, kept in the corpus on purpose: a long file path and a base64 string of ordinary prose both trip `secret:high_entropy`. Expect similar noise from data URIs and long encoded identifiers.
- The corpus is small and written by the same author as the scanners. The numbers show that the stated rules work as written, not how the scanners perform on real repositories.

## KNOWN_MISSES

Defined in `tests/test_scanners_corpus.py`; recall for these is recorded and nothing is asserted about it.

- `reversed`: patterns are matched left to right; reversed text is never un-reversed.
- `split_concat`: pieces joined by `" + "` are never re-assembled before matching.
- `rot13`: no rotation or substitution cipher is undone.
- `hex`: hex-encoded text is not decoded (only base64, URL and `\u`/`\x` escapes are).
- `gzip_b64`: compressed content is not decompressed, even one layer down.

Also not detected: more than one level of encoding; random hex secrets of 32/40/64 characters (they look like hashes); short or low-entropy secrets without a known prefix or an assignment label; names and free-text personal data; non-North-American addresses without a postal code near a keyword; text inside binary formats or archives (an archive is reported as `data:archive`, its contents are not read).

Personal-data detection also runs on one level of base64, URL and escape decoding, the same pass the secret scanner uses. Structured-content and deny-list term scanners do not decode.
