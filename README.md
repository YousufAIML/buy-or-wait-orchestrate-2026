# HackerRank Orchestrate

Starter repository for the **HackerRank Orchestrate** 24-hour hackathon (September 2026).

## Buy or Wait?

Build an AI-powered financial agent that decides whether a user can safely afford a requested expense.

A user may ask: **"Can I afford this laptop?"**

Answering well takes more than the current balance. The agent must account for recurring expenses, pending payments, essential spending, confirmed income, available payment options, and relevant details buried in messages and images.

For every request, the agent decides whether the user should pay in full, pay partially, use installments, wait, or not proceed. The recommendation must be personalized: two users with the same balance can deserve different answers based on their commitments, priorities, payment preferences, and willingness to adjust flexible expenses.

A recommendation is safe only if the user can complete the full payment plan, cover essential expenses, and stay above their preferred minimum balance throughout the forecast period.

Read [`problem_statement.md`](./problem_statement.md) for the full task spec, input/output schema, allowed values, conflict-resolution rules, and submission format.

---

## Quick Start

Clone the repository and move into the project directory:

```bash
git clone https://github.com/interviewstreet/hackerrank-orchestrate-september26.git
cd hackerrank-orchestrate-september26
```

Build your solution in `code/main.py`, or use another language and document its entry point clearly.

Your solution must:

- Read the input files from `dataset/`
- Generate one prediction for every request
- Write the final predictions to `output.csv` in the repository root

Run the starter Python entry point with:

```bash
python3 code/main.py
```

After running your solution, confirm that `output.csv` exists in the repository root and contains the required columns and one row for every request.

## How to run the final pipeline

Run commands from the repository root. The scripts in `code/` are the orchestration entry points, not duplicate implementations; the deterministic engine remains under `src/`.

1. **Deterministic validation with mock explanations** (no provider call):
   ```bash
   python3 code/main.py --mode evaluation
   ```
2. **Image evidence resolution**, after verifying the Gemini quota with one deliberate no-retry call, run the existing image-batch entry point with five-second pacing and zero retries:
   ```bash
   python3 - <<'PY'
   from src.extraction.batch import run_image_batch
   print(run_image_batch(fixed_delay_seconds=5.0, gemini_max_retries=0))
   PY
   ```
   It persists successful values to `evaluation/resolved_overrides.json` and uses the cache/usage ledger. Do not rerun it after all image overrides are resolved.
3. **Fresh Groq explanation batch** (live calls, only after numeric decisions are final):
   ```bash
   python3 code/groq_batch.py
   ```
   If the workspace execution limit interrupts a fresh batch, resume only the missing requests in bounded chunks:
   ```bash
   python3 code/groq_resume.py --limit 50
   ```
   Repeat the bounded resume command until it reports 250 persisted explanations and zero consistency flags. It does not recall already persisted requests.
4. **Final live-explanation validator and output writer**:
   ```bash
   python3 code/final_output.py
   ```
   This recomputes the deterministic decisions, checks every live explanation against the finalized fields, validates all output invariants, and writes root-level `output.csv` only when all rows pass.

The final submission package includes these scripts so the staged build and its recovery path are reproducible. Do not run the live Groq scripts during ordinary deterministic tests.

## Important File Locations

```text
dataset/        Input data and the blank output template. Do not modify the input data.
code/           Your solution code.
output.csv      Final generated predictions in the repository root.
code.zip        ZIP file containing your complete solution for submission.
```

The blank template at `dataset/output.csv` is provided as a reference. Your final generated file must be the root-level `output.csv`.

---

## Repository Layout

```text
.
├── AGENTS.md                         # Rules for AI coding tools + transcript logging
├── problem_statement.md              # Full challenge statement
├── README.md                         # You are here
├── code/                             # Your solution code
├── output.csv                        # Final generated predictions
└── dataset/
    ├── requests.csv                  # 250 requests to evaluate — predict these
    ├── output.csv                    # Blank submission template
    ├── sample_requests.csv           # 25 solved examples
    ├── financial_profiles.csv        # Balances, minimum balance, priorities, preferences
    ├── financial_events.csv          # Historical, pending, and confirmed transactions
    ├── request_payment_options.csv   # Payment options available per request
    ├── exchange_rates.csv            # Fixed, dated conversion rates
    ├── messages.csv                  # Messages tied to users, requests, or events
    ├── images.csv                    # Payroll letters, statements, bills, receipts
    └── media/
        └── images/
```

Only `dataset/requests.csv` requires predictions. Everything else is context. Join user records with `user_id`, request records with `request_id`, supporting evidence with `related_event_id`, and exchange rates with the rate date and currency pair.

Amounts are in the user's `home_currency` — the dataset uses INR, ZAR, IDR, USD, and EUR, and every conversion rate you need is in `exchange_rates.csv`. All dates are `YYYY-MM-DD`. Live exchange rates, market data, and banking access are not required.

---

## What You Need to Build

For every row in `dataset/requests.csv`, produce one row in `output.csv` with:

| Column | Meaning |
|---|---|
| `request_id` | The request being answered |
| `amount_safe_to_pay` | Largest amount safe to pay on `request_date` before optional spending changes, after protecting essentials and the minimum balance |
| `affordability_status` | `affordable_now`, `affordable_with_plan`, `affordable_later`, or `not_affordable` |
| `recommended_payment_method` | `full_payment`, `partial_payment`, `installments`, `wait`, or `not_recommended` |
| `payment_plan` | Chronological `<YYYY-MM-DD>:<amount>` entries joined by `\|`, or `none` |
| `earliest_date_for_full_payment` | Earliest date the full amount is forecast safe as one payment; empty if never within the forecast |
| `spending_changes_needed` | Up to three `stop:<event_id>` / `reduce_to:<event_id>:<amount>` changes joined by `\|`, or `none` |
| `decision_explanation` | Short explanation and the financial facts behind it |

`0 <= amount_safe_to_pay <= requested_amount` must always hold. Installment plans must exactly match a supplied payment option, and only recurring expenses marked flexible may be changed.

`affordable_with_plan` means the full request is completed through a partial-payment schedule, installments, or permitted spending changes. Recommend `partial_payment` only when the request allows it, the user accepts it, `0 < amount_safe_to_pay < requested_amount`, and `earliest_date_for_full_payment` is on or before `desired_completion_date`. Use exactly two payments: pay `amount_safe_to_pay` on `request_date`, then pay the remaining amount on `earliest_date_for_full_payment`. The two payments must add up to `requested_amount`. Unlike installments, partial payment does not need to match a supplied payment option.

---

## Suggested Workflow

1. Inspect `dataset/sample_requests.csv` — 25 requests with completed output columns — to understand the expected format and decision style.
2. Reconstruct each user's financial state from `financial_profiles.csv` and `financial_events.csv`: separate recurring expenses from one-time events, reserve pending transactions, count confirmed salary only on its settlement date, and de-duplicate repeated representations of the same event.
3. When an event has a blank `amount`, find its `event_id` as `related_event_id` in `images.csv` and extract the amount from the linked image. Never treat a blank amount as zero. Pull in any other relevant messages, images, and payment options for the request.
4. Forecast forward and generate a plan that keeps the balance above the minimum at every step.
5. Verify deterministically — bounds, plan feasibility, schedule match, flexible-only spending changes — before writing `output.csv`.
6. Score yourself on the solved samples, then run the full dataset.

You may use any language or runtime. Python, JavaScript, and TypeScript are all reasonable choices.

---

## Requirements

Your solution must:

- be runnable from the terminal
- read the provided files from `dataset/`
- produce a valid `output.csv` with the exact required columns in the exact required order
- include one prediction for every `request_id` in `dataset/requests.csv`
- not use organizer-only files or hardcoded labels
- keep behavior deterministic where possible

If you use API keys or secrets, read them from environment variables. Never hardcode secrets in the repo.

## Known limitations

Phase 3 is locked after fixing two structural recurrence bugs:

- **Income recurrence rejection:** regular salary history was previously grouped with irregular bonuses/one-off credits, causing the entire income group to be rejected. Clean regular income subsequences are now preserved.
- **Dense-cadence underprojection:** a sparse same-day calendar subsequence could previously override a denser regular cadence such as 10-day groceries or 5-day transport. Full-group cadence coverage now takes precedence, and independently regular remainder streams are preserved.

The final public-sample match remains limited: exact `amount_safe_to_pay` matches were **1/21 after the final Phase 3 implementation**, compared with **2/21 before the final coverage fix**; exact `earliest_date_for_full_payment` matches were **6/21**, compared with **7/21 before that fix**. The remaining gap is attributed to residual date-projection methodology, especially fixed 30/31-day monthly drift instead of exact calendar recurrence, and possibly the essential-versus-flexible spending distinction, which is not yet fully modeled. These are documented methodology limitations, not confirmed structural bugs. Status and payment-method mismatches are predominantly inherited from this documented recurrence-methodology gap, including deadline-boundary sensitivity where small date differences can flip a completion check from pass to fail.

Eleven of the 250 evaluation requests remain blocked by unresolved Gemini image amounts. They are pending the provider's daily quota reset and will be resolved before the final Phase 6 run; blank amounts are never treated as zero.

This submission intentionally uses a stdlib-only CSV layer because pandas is unavailable in the execution sandbox: `ensurepip` is missing, apt installation is not permitted, and the available Python interpreters do not bundle pandas. `src/engine/dataframe_lite.py` replaces DataFrame operations with typed dataclass loading, centralized coercion (`date`, `Decimal`, boolean, and optional parsing), and explicit O(1) indexes for joins. All financial arithmetic uses `Decimal`; all calendar values use `datetime.date`. The loader is deliberately narrow and does not provide general-purpose vectorized transformations.

### Final architecture and model routing

The final solution has two layers. The evidence layer resolves image amounts and classifies relevant messages at the persisted `evaluation/resolved_overrides.json` boundary; Gemini `gemini-3.6-flash` is used only for image evidence: 16 Phase 6 calls plus the earlier resolved `event_253` call, while the Phase 2 message-classification calls use Groq. The decision layer is deterministic: it performs currency conversion, lifecycle/conflict resolution, 90-day forecasting, payment-plan construction/ranking, spending-change validation, and strict output validation. Groq `openai/gpt-oss-120b` is used only after each numeric decision is final, to phrase `decision_explanation`; its prompt receives finalized fields and cannot revise or recompute them. The final validator checks the live explanation facts against the deterministic values before writing `output.csv`.

The required empty-date convention is intentional: `earliest_date_for_full_payment` is an empty CSV cell when no safe full-payment date exists, while categorical absent values such as `payment_plan` and `spending_changes_needed` use the literal `none`. The documented Phase 3 date-projection limitation remains: small recurrence-projection differences can shift an earliest safe date across a desired-completion deadline.

### Event-context-aware image extraction (post-diagnostic improvement)

A read-only diagnostic confirmed one isolated extraction bug and one instance of the known recurrence limitation. The image-extraction prompt in `src/extraction/handlers.py` now passes the linked event's description as context and instructs the model to select the figure matching that description on multi-figure receipts: when the event describes an outstanding balance, balance due, remaining amount, or unpaid portion, the outstanding/balance-due figure is preferred over a total or gross transaction figure; otherwise the total is preferred. This is a general prompt improvement, not a special case. It was validated by a single re-extraction of `event_1442` ("Outstanding rent balance"), which corrected the override from the receipt's `INR 200,000` total to its `INR 100,000` balance-due figure and made `request_16` match all four sample fields. The confirmed `request_17` case is the already-documented latest-occurrence recurrence limitation and was intentionally left unchanged.

### Recurrence-amount outlier guard (scoped post-diagnostic fix)

A second read-only diagnostic across all 2,467 recurring rules of the 250 evaluation requests found exactly one case where the inferred recurrence amount was a significant outlier: a one-off image-resolved event (an airline ticket for `user_105`) landed as the latest observation of the transport category, and the `max(amounts[-3:])` inference for protected/variable debits projected that one-off amount forward every period. The sample set contained one analogous case (`request_17`'s bulk grocery purchase in the weekly groceries stream). Because the blast radius was exactly one evaluation request plus one sample request (stable across 1.5x/2x thresholds), the fix was scoped minimally in `src/engine/forecast.py`: after the existing amount inference, if a stream has at least four observations and the inferred amount exceeds twice the median of the stream's own prior observations, the rule falls back to that prior median. This guards only genuine outliers within a recurrence stream and leaves all other rules unchanged — exactly one evaluation row (`request_105`) changed as a result, and `request_17` began matching status, method, and earliest-date in the public sample check. The residual `request_17` amount difference remains part of the documented Phase 3 projection limitation.

---

## Evaluation

Your `output.csv` will be compared against hidden ground-truth values.

The scoring will consider:

- accuracy of `amount_safe_to_pay`
- correctness of `affordability_status`
- correctness of `recommended_payment_method` and `payment_plan`
- accuracy of `earliest_date_for_full_payment`
- validity of `spending_changes_needed`
- usefulness and consistency of `decision_explanation`

### Token Usage And Cost Analysis

Your `code.zip` must include one token-usage file:

```text
evaluation/usage_report.md
```

The report must cover model providers and names, model calls, input and output tokens, total and average tokens per request, estimated total and per-request cost. The reported values must correspond to the final full-dataset run that produced your `output.csv`.

---

## Chat Transcript Logging

This repo includes an [`AGENTS.md`](./AGENTS.md) file for AI coding tools. It asks compatible tools to append conversation summaries to a `log.txt` in the repository root — the same directory as `AGENTS.md`:

| Platform | Path |
|---|---|
| macOS / Linux | `<repo root>/log.txt` |
| Windows | `<repo root>\log.txt` |

The path resolves relative to `AGENTS.md`, so it stays correct across clones, renames, and checkouts. `log.txt` is gitignored — upload it as your chat transcript at submission time. Do not paste secrets into the chat.

In case, the harness you are using is not in the repo root, you can explicitly ask the agent to look for the AGENTS.md in this folder & then continue.

---

## Submission

Submit the following files as instructed by HackerRank:

| File | Description |
|---|---|
| `code.zip` | Full runnable solution, prompts/configuration, README, and the required `evaluation/` folder |
| `output.csv` | Predictions for every row in `dataset/requests.csv` |
| `chat_transcript` | The `log.txt` described above, showing how you developed or used the system |

Before submitting, confirm:

- `output.csv` has one row per row in `dataset/requests.csv` (250 rows plus the header).
- `output.csv` has the exact required columns in the exact required order.
- Every `amount_safe_to_pay` satisfies `0 <= amount_safe_to_pay <= requested_amount`.
- Every installment plan matches a supplied payment option, and every spending change targets a flexible recurring expense.
- Your runnable code, setup instructions, and `evaluation/` folder are included in `code.zip`.
