# The agent loop

The loop is the product. Everything else — tools, sandbox, API, UI — exists to
make one iteration cheap, observable and safe.

## One iteration

```
 ┌─ iteration N ─────────────────────────────────────────────────────────┐
 │                                                                       │
 │  budget check ──► exhausted? ──► TIMEOUT, stop                        │
 │       │                                                               │
 │  inject pending directives (recovery, failure classification, budget) │
 │       │                                                               │
 │  model call: system prompt + compacted transcript + tool schemas      │
 │       │                                                               │
 │       ├──► no tool calls?  ──► nudge and continue (a wasted turn)     │
 │       │                                                               │
 │  for each requested tool call:                                        │
 │       ├─ derive UI state from the tool  (search_code → SEARCHING)     │
 │       ├─ emit TOOL_CALLED                                             │
 │       ├─ registry.call()  ──► policy check ──► execute                │
 │       ├─ record in the anti-loop detector                             │
 │       ├─ emit TOOL_RESULT, fold into the run record                   │
 │       └─ finish() requested? ──► answer remaining calls, break        │
 │                                                                       │
 │  append tool results to the transcript (capped)                       │
 │  anti-loop check ──► signal? ──► emit + queue a recovery directive    │
 │  append continuation nudge (budget, last test result, patch state)    │
 │                                                                       │
 └───────────────────────────────────────────────────────────────────────┘
```

## The state machine

```
PENDING
   │
   ▼
PREPARING ──► ANALYZING ──┬──────────────────────────────┐
                          │                              │
              ┌───────────┴───────────┐                  │
              ▼           ▼           ▼                  │
          SEARCHING    READING   HYPOTHESIZING           │
              │           │           │                  │
              └───────────┴─────┬─────┘                  │
                                ▼                        │
                            PATCHING                     │
                                │                        │
                                ▼                        │
                             TESTING                     │
                                │                        │
                       green? ──┴── no ──► REFLECTING ───┘
                          │
                          ▼
                      RESOLVED          FAILED / TIMEOUT / CANCELLED
```

Working state is derived from the tool the agent selected
(`TOOL_STATE_MAP` in `domain/enums.py`). Every transition is persisted as a
`STATE_CHANGED` event, so the UI can replay a run exactly.

## Phases the prompt enforces

| Phase | What the agent must do |
|---|---|
| **A. Understand** | Extract expected vs observed behaviour, error type, and concrete identifiers from the issue |
| **B. Localise** | `search_code` on those identifiers, `find_symbol` for definitions, read line ranges — not whole files. Find the tests that cover the code |
| **C. Hypothesise** | Call `record_hypothesis` with cause, evidence, files, fix and validation plan *before* editing |
| **D. Patch** | Minimal, root-cause, convention-matching. No unrelated refactoring, no weakening tests |
| **E. Validate** | Targeted test first, then broaden to catch regressions |
| **F. Observe** | Classify the failure: `ORIGINAL_BUG`, `REGRESSION`, `WRONG_ASSUMPTION`, `UNRELATED`, `ENVIRONMENT` |
| **G. Stop** | `finish(resolved=true)` only after a green test run; `finish(resolved=false)` when genuinely stuck |

## Stop conditions

The loop terminates on exactly one of:

1. `finish` accepted — and `finish(resolved=true)` is **refused** unless
   - the working tree contains changes, **and**
   - a test run has happened, **and**
   - the most recent test run was green.
2. A budget is exhausted (`max_iterations`, `max_tool_calls`, `max_runtime_s`,
   `max_cost_usd`) → `TIMEOUT`.
3. Cancellation → `CANCELLED`.
4. An unrecoverable provider or internal error → `FAILED`.

A second guard runs at finalisation: a run claiming `RESOLVED` with an empty
diff is downgraded to `FAILED` with the reason
*"agent reported success but produced no patch"*.

## Anti-loop detection

Four signals, checked at the end of every iteration
(`agent/antiloop.py`):

| Signal | Trigger | Injected directive |
|---|---|---|
| `repeated_action` | Same tool + canonically identical args ≥ 3× | Name what that action already established, then take a *different* action — widen the search, read the tests, look at callers |
| `repeated_patch` | Normalised patch hash seen ≥ 2× | Your hypothesis is probably wrong, not your patch syntax. Re-read the failing test and record a **new** hypothesis |
| `test_churn` | ≥ 3 test runs with no intervening mutation | The result cannot change without a code change. Read the failure or finish honestly |
| `stalled` | ≥ 4 iterations with no mutating tool call | Investigation is not the goal — a patch is. Commit to the most plausible cause and edit now |

Arguments are canonicalised before hashing (`timeout_s` and `limit` are
stripped, since they do not change what an action *means*). Patch hashes ignore
whitespace, `@@` offsets and `index` lines, so a re-submitted patch with shifted
line numbers is still recognised as a repeat.

When a signal fires the agent is given a factual digest of the run — files
inspected, files modified, current hypothesis, last test result, failing tests,
queries already tried — and asked three questions before it may act:

1. Which assumption of yours is most likely wrong?
2. What evidence would disprove it?
3. What is the single most informative *different* action you can take now?

Each signal fires once per run, so recovery escalates instead of nagging.

## Transcript compaction

A 30-iteration run with full file reads and test output would exceed any
context window. `agent/transcript.py`:

- caps any single tool result at `max_tool_output_bytes` on the way in
  (head + tail, with a byte count in the middle);
- keeps the most recent 6 tool-result messages verbatim — that is the agent's
  working memory;
- replaces older tool *results* with one-line summaries, while preserving the
  message structure so every `tool_use` still has a matching `tool_result`.

Assistant text and tool *calls* are never compacted: the record of what the
agent decided stays intact for the whole run.

## What is recorded

Per run: state transitions, tool calls with arguments and outcomes, structured
hypotheses, patch attempts, test runs with parsed counts and failure excerpts,
model usage, and budget snapshots.

What is **not** recorded: model chain-of-thought. Providers that emit
`thinking` blocks have them dropped at the provider boundary. The only free
text stored from the model is its short visible status line
(`AGENT_MESSAGE`) and the structured fields it passed to `record_hypothesis`.
