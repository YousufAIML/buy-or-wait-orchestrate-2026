# Buy or Wait? — Agent Handoff

_Last updated: 2026-09-12. This file is the resume point for a fresh agent session with no chat history._

## Current status

Phases 0–3 are implemented and validated to the extent documented below. **Phase 4 has not started.** Do not implement payment-plan ranking, spending-change fallback, or output assembly until the recurrence logic and unresolved image extractions are reviewed.

The current Phase 3 engine is deterministic but its recurrence heuristics are not final: public sample validation is poor. Treat the current forecast numbers as diagnostic, not submission-ready.

## Architecture

The project has two strictly separated layers:

### Layer A — deterministic engine: `src/engine/`

The engine contains no provider, network, or extraction imports and makes no LLM calls.

- `models.py`: typed dataclasses for users, events, requests, payment options, messages, images, exchange rates, solved samples, and output templates.
- `dataframe_lite.py`: stdlib-only CSV loading over `csv.DictReader`; centralized coercion for `date`, `Decimal`, booleans, optionals, and pipe-separated fields; unique and one-to-many O(1) indexes.
- `currency.py`: exact dated pair-matched conversion using `Decimal`; no implicit inverse-rate or fallback behavior.
- `resolver.py`: deterministic linked-event lifecycle resolution. It preserves legitimate cash legs such as expense plus refund/reversal, excludes duplicates/failed/cancelled/unrealized records, reserves pending debits, excludes pending credits, and includes scheduled confirmed salary credits on settlement date.
- `overrides.py`: validates the persisted extraction-to-engine boundary (`version: 1`). The engine receives one `ResolvedOverrides` object, not the extraction cache.
- `forecast.py`: inclusive `request_date` through `request_date + 90 days` balance timeline, recurring-event projection, `amount_safe_to_pay`, and `earliest_date_for_full_payment`.
- `reconnaissance.py`: typed Phase 0 dataset reconnaissance.

All financial math uses `Decimal`; all dates use `datetime.date`. Blank amounts raise `MissingForecastAmount`; they are never treated as zero.

### Layer B — extraction: `src/extraction/`

- `llm_client.py`: stdlib `urllib.request` clients only.
  - Gemini endpoint: `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`
  - Gemini default model: `gemini-3.6-flash`
  - Groq endpoint: `https://api.groq.com/openai/v1/chat/completions`
  - Groq default model: `openai/gpt-oss-120b`, overridden by `GROQ_MODEL`.
  - Explicit timeout, HTTP 429 `Retry-After` handling, transport retries, loud failures, response header/body preservation, and usage/error ledger records.
- `cache.py`: SQLite cache keyed by `image_amount/event_id` and `message_classification/message_id`.
- `handlers.py`: strict validated image amount extraction and message classification. Instruction-like content is ignored and cached as untrusted. Image results require positive amount, matching currency, and numeric confidence in `[0, 1]`. Message actions are `confirm`, `amend_amount`, `amend_date`, `cancel`, or `none`.
- `env.py`: `load_dotenv_manual()` reads simple `KEY=VALUE` lines without overwriting existing environment variables.
- `usage.py`: append-only JSONL usage/error ledger.
- `batch.py`: full extraction batch and image-only batch with cache restoration and provider pacing.

Extraction results are persisted to `evaluation/resolved_overrides.json`. The engine reads this validated file, never the SQLite extraction cache.

## Phase 0 validated results

- Read `AGENTS.md`, `CLAUDE.md`, `problem_statement.md`, and the dataset schema.
- Pandas was confirmed unavailable: no `pip`, `ensurepip`, usable apt installation, or alternate interpreter with pandas. A venv interpreter exists but has no pip/packages.
- Typed reconnaissance loaded all participant CSVs successfully:
  - 275 users
  - 25,342 financial events
  - 250 evaluation requests
  - 25 solved sample requests
  - 790 payment options
  - 215 messages
  - 16 images
  - 134 exchange rates
  - 250 output-template rows
- All 16 blank event amounts have linked image rows and image files.
- `sample_requests.csv` is separate from the 250 evaluation requests.

## Phase 1 validated results

- Money values are `Decimal`; calendar values are `date`.
- Dated currency conversion and linked-event resolution have synthetic tests.
- Real-data smoke resolution processed all 25,342 events and produced auditable conflict decisions.
- The event schema has no explicit source column; same-source inference uses conservative description markers. This remains a documented ambiguity.

## Phase 2 validated results

- No SDKs are used; both clients are direct urllib REST clients.
- Keys are read only from `os.environ`; no key argument or literal key exists in source.
- Gemini and Groq each passed real smoke calls end-to-end:
  - Gemini: `gemini-3.6-flash`, image extraction succeeded for `event_253/image_01`.
  - Groq: `openai/gpt-oss-120b`, message classification succeeded for `message_14/event_1785`.
- Real Groq smoke parsed to `confirm`, confidence `0.95`, with 224 input, 247 output, and 471 total tokens.
- Real Gemini smoke parsed to IDR 4,365,000, confidence `0.99`. Its provider response reported prompt 1,126, candidate output 116, thoughts 395, total 1,637; the current ledger records prompt/candidate counts as input/output.
- The full mocked/unit suite passed before Phase 3 with 27 tests, then passed with 34 tests after the override and Phase 3 additions.

## Full extraction batch status

The required batch covered all evidence, not only direct evaluation-request evidence:

- Messages targeted: **39** event-linked messages.
- Messages resolved: **39/39**.
- Two initial responses used `confidence: "high"` instead of a numeric score; strict validation rejected them. The prompt was tightened to require numeric confidence and the two unresolved items were retried successfully.
- Blank image events targeted: **16**.
- Image amounts resolved: **1/16** (`event_253`, restored from a validated smoke result/cache).
- The remaining 15 image calls repeatedly returned Gemini HTTP 429 after provider `Retry-After` handling.
- The later image-only retry processes one image at a time with a fixed five-second delay after every uncached attempt, including failures. It still resolved only **1/16**.
- No message calls were made during the image-only retry.
- Persisted file: `evaluation/resolved_overrides.json`.
- Current unresolved image event IDs:
  - `event_1442`, `event_1545`, `event_1700`, `event_1786`, `event_3051`
  - `event_3231`, `event_4535`, `event_5170`, `event_6033`, `event_6859`
  - `event_7307`, `event_7941`, `event_9421`, `event_9806`, `event_10521`

The usage ledger contains successful provider records and error records. At the last aggregate check it had 42 successful records, 10,732 input tokens, 11,245 output tokens, 21,977 total tokens, and `$0` cost; later image-only 429 failures add error entries but no successful tokens. Free-tier provider cost is `$0`.

## Gemini image quota scheduling dependency

The 15 unresolved image amounts are a provider-quota scheduling dependency, not an engine bug. Do not spend Luna session credits waiting for the quota reset and do not make additional Gemini image calls during the exhausted quota window.

- The captured Gemini error identifies `GenerateRequestsPerDayPerProjectPerModel-FreeTier`, metric `generate_content_free_tier_requests`, model `gemini-3.6-flash`, and quota value `20`.
- Public model documentation confirms `gemini-2.5-flash-lite` as a real stable multimodal model with image input and free-tier pricing. Public documentation does not expose a confirmed separate usable RPD number for this project, so do not assume switching models will help or guess an unverified model ID.
- The operational retry schedule is after the next UTC midnight (`2026-09-13T00:00:00Z`), not now. The provider error supplies only a relative retry delay and no reset timestamp. The public rate-limit documentation currently describes RPD reset at midnight Pacific, so verify the provider's actual reset boundary before the retry; the exact reset timezone is not established by the error body.
- The 15 event IDs remain unresolved and must be retried one at a time after the reset window: `event_1442`, `event_1545`, `event_1700`, `event_1786`, `event_3051`, `event_3231`, `event_4535`, `event_5170`, `event_6033`, `event_6859`, `event_7307`, `event_7941`, `event_9421`, `event_9806`, `event_10521`.
- The engine intentionally raises `MissingForecastAmount` for each unresolved blank amount. It never converts a blank amount to zero. The 11 affected evaluation requests remain honestly blocked: `request_33`, `request_35`, `request_48`, `request_55`, `request_64`, `request_73`, `request_78`, `request_84`, `request_101`, `request_105`, and `request_113`.

## Phase 3 implementation and tests

Run the full suite with:

```bash
python3 -m unittest discover -s tests -v
```

Current suite result: **38 tests passed**.

Synthetic coverage includes:

- Minimum balance never at risk.
- User already below minimum on request date.
- Payment appears safe on request date but breaks safety on a later day.
- No recurring events.
- Regular monthly history projected through the horizon.
- Future pending debit using settlement date.
- Scheduled confirmed salary included on settlement date.
- Persisted override typing and validation.

The first real-data Phase 3 run without image overrides forecasted 239/250 requests. The final override-applied run also forecasted **239/250** because the 11 unresolved evaluation requests depend on the unresolved image events below:

```text
request_33, request_35, request_48, request_55, request_64, request_73,
request_78, request_84, request_101, request_105, request_113
```

All successful forecasts satisfied `0 <= amount_safe_to_pay <= requested_amount`. There were 117 successful forecasts with an earliest safe full-payment date.

## Four-row diagnostic already performed

The four rows were selected as the two closest and two farthest absolute amount mismatches among forecastable samples. The trajectory is the no-request-payment balance trajectory.

### `request_22` — closest

- Requested: `731.5`
- Expected safe amount: `475.46`
- Computed safe amount: `475.25`
- Difference: `0.21`
- Starting balance: `1132.46`
- Minimum to keep: `500`
- 90-day minimum: `975.25` on `2024-12-15`
- Minimum-date event: recurring `event_1891`, `delivery_membership`, debit `5`.

### `request_06` — close

- Requested: `620.4`
- Expected safe amount: `603.3`
- Computed safe amount: `620.4`
- Difference: `17.1`
- Starting balance: `1942.4`
- Minimum to keep: `800`
- 90-day minimum: `1592.80` on `2026-01-13`
- Minimum-date event: recurring `event_531`, `transport`, debit `32.9`.

### `request_04` — far

- Requested: `12,693,000`
- Expected safe amount: `8,401,800`
- Computed safe amount: `0`
- Difference: `8,401,800`
- Starting balance: `52,206,950`
- Minimum to keep: `30,686,600`
- 90-day minimum: `-52,177,524.06` on `2024-09-02`
- Minimum-date events:
  - recurring `event_286`, utilities debit `2,004,118.60`
  - recurring `event_291`, rent debit `12,293,000`
  - recurring `event_356`, dining debit `1,886,856.10`

### `request_11` — farthest

- Requested: `13,110,000`
- Expected safe amount: `12,510,645`
- Computed safe amount: `0`
- Difference: `12,510,645`
- Starting balance: `63,531,795`
- Minimum to keep: `34,140,600`
- 90-day minimum: `-6,267,591.21` on `2025-07-27`
- Minimum-date event: recurring `event_967`, groceries debit `1,341,187.18`. The negative value is cumulative across earlier recurring debits; this is the event occurring on the minimum date.

## Root cause assessment: bug vs. heuristic imprecision

The synthetic safety arithmetic and edge-case tests pass. No confirmed arithmetic or Decimal-summation bug has been found.

The sample divergence is primarily **heuristic imprecision**, plus incomplete image evidence:

- All 21 computed sample amount mismatches used inferred recurrence rules plus the protected-variable recent-maximum and optional/fixed recent-median policies.
- Resolver/status exclusions also affected `request_01`, `request_05`, `request_06`, `request_21`, `request_22`, and `request_25`.
- Earliest-date matches were only the five valid `None` cases: `request_10`, `request_14`, `request_15`, `request_24`, and `request_25`.
- Four samples remain unforecastable because their image amounts are unresolved, not because of arithmetic failure.

Do not treat the recurrence policy as final.

## Coverage fix implemented after final three-sample diagnostic

The recurrence grouping now computes full-group regularity before accepting a
calendar subsequence. A valid denser full-group cadence wins over a sparse
monthly/weekly candidate. If the excluded remainder is itself regular, it is
preserved as a second recurrence stream rather than discarded. The regression
suite covers monthly salary plus irregular bonus, dense 10-day groceries, dense
5-day transport, and a weekly stream with same-day monthly coincidences.

Validation after this fix:

- 38 unit tests passed.
- Sample forecastability remained 21/25 because four image amounts remain unresolved.
- Exact sample amount matches: **1/21**, compared with 2/21 immediately before this coverage fix.
- Exact sample earliest-date matches: **6/21**, compared with 7/21 immediately before this coverage fix.
- Full evaluation forecastability remained 239/250 with 11 unresolved image blocks.
- Negative evaluation trajectories: **25**, compared with 24 immediately before this fix.
- Zero-safe evaluation results: **33**, compared with 27 immediately before this fix.
- The dense-cadence diagnostics improved substantially for `request_02`, `request_03`, and `request_25`, but exact sample labels indicate remaining policy/date-recurrence divergence.

## Proposed recurrence fixes before Phase 4

These are proposed, not implemented:

1. Replace fixed median-day increments for 30/31-day series with calendar-aware monthly recurrence, preserving day-of-month where supported.
2. Detect weekly recurrence using weekday continuity and monthly recurrence using day-of-month continuity rather than only gap tolerance.
3. Require recent continuity and distinguish fixed commitments from regular but discretionary transaction streams.
4. Separate essential/protected variable spending budgets from optional spending instead of applying one global recent-max/median rule.
5. Make category-specific budget policy explicit and rerun the exact sample diff after each change.
6. Recheck resolver exclusions against sample evidence before changing conflict precedence.

A fresh agent should make these changes only after reviewing this handoff and should rerun the 34-test suite plus the complete sample diff before Phase 4.

## Environment and secrets

Required environment variables:

```text
GEMINI_API_KEY
GROQ_API_KEY
```

Optional model override:

```text
GROQ_MODEL
```

If `GROQ_MODEL` is unset, the default is `openai/gpt-oss-120b`. Gemini defaults in code to `gemini-3.6-flash`.

`.env` is supported by `src/extraction/env.py` and must be loaded at the very beginning of an entry-point script with:

```python
from src.extraction.env import load_dotenv_manual
load_dotenv_manual()
```

`.env` and `.env.*` are ignored by `.gitignore`; `.env.example` is allowed. Never print, commit, package, or paste key values. The originally exposed chat credentials were not used or copied into source or logs; rotate them and use replacement values locally.

## Resume instructions

1. Read this file and `AGENTS.md`.
2. Run `python3 -m unittest discover -s tests -v`.
3. Inspect `evaluation/resolved_overrides.json`, `evaluation/usage_ledger.jsonl`, and the unresolved image list above.
4. Refine recurrence logic only with deterministic `src/engine` code and rerun sample validation.
5. Do not access extraction cache from the engine.
6. Do not start Phase 4 until recurrence/sample validation is reviewed and explicitly approved.
