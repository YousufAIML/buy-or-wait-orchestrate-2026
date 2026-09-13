# Final Usage Report

Generated for the final deterministic/output build. All provider calls were free-tier calls; estimated cost is **$0.00**.

The usage ledger is append-only. Successful call totals below come from `evaluation/usage_ledger.jsonl`; failed provider records have no token usage and are listed separately. Legacy records created before latency instrumentation have unavailable latency.

## Provider summary

| Provider | Model(s) | Successful calls/records | Input tokens | Output tokens | Total tokens | Cost |
|---|---|---:|---:|---:|---:|---:|
| Gemini | `gemini-3.6-flash` | 18 | 20,459 | 1,590 | 22,049 | $0.00 |
| Groq | `openai/gpt-oss-120b` | 292 | 91,364 | 96,950 | 188,314 | $0.00 |
| **Total** | — | **310** | **111,823** | **98,540** | **210,363** | **$0.00** |

## Gemini usage by phase/purpose

| Phase/purpose | Calls | Input tokens | Output tokens | Total tokens | Cost |
|---|---:|---:|---:|---:|---:|
| Earlier resolved image `event_253` | 1 | 1,126 | 116 | 1,242 | $0.00 |
| Phase 6 quota-check call | 1 | 1,130 | 72 | 1,202 | $0.00 |
| Phase 6 remaining image extractions | 15 | 16,979 | 1,320 | 18,299 | $0.00 |
| Post-fix `event_1442` re-extraction (event-context-aware prompt) | 1 | 1,224 | 83 | 1,307 | $0.00 |
| **Gemini total** | **18** | **20,459** | **1,590** | **22,049** | **$0.00** |

The Phase 6 image work used `gemini-3.6-flash`. The quota-check call was not persisted as a new image override; it was an informational call. The 15 extraction calls populated the remaining image amounts in `evaluation/resolved_overrides.json`. The final `event_1442` call re-extracted the outstanding rent balance with the improved event-context-aware prompt and corrected the override from the receipt's total figure to its balance-due figure.

There were 48 historical Gemini error records in the ledger, including prior model-availability and quota experiments. They produced no tokens and are excluded from successful token totals. The final successful image run had no extraction failures.

Latency for these historical Gemini success records was not captured by the original ledger schema.

## Groq usage by phase/purpose

### Phase 2 message classification

The ledger contains **39 unique message IDs** and **41 successful provider records**. `message_106` and `message_121` each have two successful records because they were reclassified after an earlier result. Totals include all 41 successful records:

| Purpose | Unique items | Successful records | Input tokens | Output tokens | Total tokens | Cost |
|---|---:|---:|---:|---:|---:|---:|
| Message classification | 39 | 41 | 9,606 | 11,129 | 20,735 | $0.00 |

Two historical Groq error records for an unavailable legacy model are present in the ledger; they have no token usage and are excluded above.

### Phase 6 decision explanations

All 250 final numeric decisions have a persisted Groq explanation using `openai/gpt-oss-120b` (251 successful records: `request_105` was regenerated once after the recurrence-outlier guard changed its finalized decision):

| Batch | Calls | Input tokens | Output tokens | Total tokens | Aggregate latency | Average latency | Cost |
|---|---:|---:|---:|---:|---:|---:|---:|
| Initial bounded run before execution timeout | 135 | 42,991 | 43,707 | 86,698 | 455,359.82 ms | 3,373.04 ms | $0.00 |
| Resumed chunks (50 + 50 + 15) | 115 | 38,438 | 41,758 | 80,196 | 377,349.12 ms | 3,281.30 ms | $0.00 |
| Post-fix `request_105` re-explanation (recurrence-outlier guard) | 1 | 329 | 356 | 685 | 1,078.03 ms | 1,078.03 ms | $0.00 |
| **Decision-explanation total** | **251** | **81,758** | **85,821** | **167,579** | **833,786.98 ms** | **3,321.86 ms** | **$0.00** |

The initial run was terminated by the workspace's 600-second command limit after requests `request_26` through `request_160`; its incremental persistence avoided re-calling those 135 requests. The remaining 115 requests were completed in three bounded chunks. After the recurrence-outlier guard changed `request_105`'s finalized numeric decision, only that one explanation was regenerated against the new finalized facts; the other 249 explanations were reused unchanged.

## Final explanation validation

- Final explanations persisted: **250/250**
- Missing explanations: **0**
- Provider failures in the final explanation set: **0**
- Consistency flags: **0**
- Literal `earliest_full_payment=null` values: **0**
- Normalized absent-date values: `none` in the explanation facts; empty CSV cells in `output.csv`

The explanation prompt supplied only finalized deterministic decision fields and instructed Groq not to decide, rank, recalculate, correct, or reinterpret them. The final output validator compared the explanation facts against those fields before writing `output.csv`.
