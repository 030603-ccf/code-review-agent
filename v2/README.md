# lra — LangGraph Review Agent (v2)

A code review agent built as a LangGraph map-reduce pipeline. Self-contained
single package, no sibling packages, no reference copies, no committed secrets.

## Pipeline

```
scan ──► chunk ──► fan-out ──► review_chunk (parallel) ──► aggregate
                                                              │
                              ┌───────────────────────────────┤
                              ▼                               ▼
                     second_review (optional)            report ──► END
                              │
                              └──► report
```

- **scan** — build a project index (file list + symbol table) with `ast` for
  Python and lightweight heuristics for other languages. Zero LLM.
- **chunk** — split files along symbol boundaries so functions are never cut
  in half. Zero LLM.
- **review_chunk** — one LLM call per chunk, fanned out by `Send`. Deterministic
  security / anti-pattern scanners + cross-file dependency context run first,
  then the LLM's findings are merged in.
- **aggregate** — evidence verification (line-number correction), de-duplication,
  global id reassignment. Zero LLM.
- **second_review** — optional cloud arbitration that confirms/rejects/defers
  each finding, run per-file in parallel.
- **report** — render `report.md`.

## Usage

```bash
cp config.example.yaml config.yaml   # then export the API key env vars
python -m lra review /path/to/project
python -m lra review /path/to/project --incremental          # git diff only
python -m lra review /path/to/project --issue-hint "check for SQL injection"

# after a review, run the fix loop on the findings
python -m lra optimize runs/<thread-id> --backend api --max-rounds 3
python -m lra optimize runs/<thread-id> --backend api --issue-hint "check for SQL injection"
```

Resume / retry / cache:

```bash
python -m lra review /path/to/project --thread-id my-run   # resume same thread
python -m lra review /path/to/project --thread-id my-run --retry-failed
python -m lra review /path/to/project --no-cache           # skip sha1 cache
```

Run artifacts land in `runs/<thread-id>/`: `project_map.json`, `findings.json`,
`report.md`, `checkpoints.sqlite`. A cross-run findings cache lives at
`runs/.findings_cache.json`.

## Feature map

| Layer | Modules |
| --- | --- |
| LLM | `client` (thread-safe, real httpx timeout, env-key, JSON mode) · `structured` (parse → repair → prose-extract → retry) · `prompts` (profile variants + 8 language supplements) |
| Analysis | `scan` (ast + heuristics) · `chunking` (symbol-boundary) · `dep_graph` (cross-file import graph) · `lsp` (language-server diagnostics) |
| Agents | `reviewer` · `aggregator` · `second_reviewer` · `rules` (`.codereview/rules.json`) |
| Tools | `security_scanner` · `anti_pattern_scanner` · `lsp_client` (JSON-RPC over stdio) — deterministic, zero LLM, honest confidence |
| Optimizer | `copier` · `fixer` (api/opencode, compile gate) · `verifier` · `loop` (fix cache + stall detection) |
| Misc | `mistake_notebook` (rejected findings → negative samples) · `cache` (sha1 findings cache) |

Evaluation: `python scripts/analyze.py <run_dir> <quixbugs_dir> --correct-dir
<correct_dir>` computes recall/precision against the quixbugs known-bug dataset;
`--correct-dir` (the corrected programs) enables the line-level recall metric.

## Configuration

See `config.example.yaml`. Secrets are read from environment variables named by
each profile's `api_key_env`; keys are never stored in the repo. The `cloud`
(DeepSeek) profile ships with `extra_body: {"thinking": {"type": "disabled"}}`
already set — see tuning decision #1 below for why.

The `lsp` section (default `enabled: false`) turns on deterministic
language-server diagnostics: severity-Error diagnostics become `correctness`
findings directly, while Warning/Info/Hint are injected into the reviewer prompt
as candidates for the LLM to verify. It requires each configured server on PATH
(`pip install pyright` provides `pyright-langserver`); missing or broken servers
are skipped silently, so leave it off unless they are installed.

## Tuning decisions (why it's configured this way)

These are empirical findings from real A/B runs on the quixbugs dataset. Do not
revert them without re-measuring.

1. **Disable thinking mode.** DeepSeek's thinking mode is ON by default
   (effort=high), and `temperature` is ignored while it's on. `reasoning_content`
   consumes tokens and truncates the JSON output. With
   `extra_body: {"thinking": {"type": "disabled"}}`, JSON failures went 5 → 0,
   tokens 291k → 59k, wall time 143s → 28s, and recall rose. This is the single
   highest-impact setting.
2. **Use `deepseek-v4-flash`, not `-pro`.** pro tested worse on every axis:
   12 JSON failures vs 5, 315s vs 143s, 362k vs 291k tokens — because a
   stronger model is *less* format-compliant. For strict-JSON output,
   "obedient" beats "smart".
3. **rpm 120, concurrency 16.** Probing 20/50/100 concurrent requests returned
   zero 429s, so the old `rpm: 6` was wildly conservative. 8× faster.
4. **sha1 findings cache.** scan already hashes every file; `review_chunk`
   skips the LLM when `(file, sha1, line range)` is cached. The key also
   embeds the model name and a `CACHE_VERSION` (bump it when prompts/schema
   change) so stale results never surface. Repeat runs: 0 requests, 0.1s.
5. **Structured output is three-layered.** JSON mode (prevention) → mechanical
   repair → prose field-extraction (rescue) → LLM retry. The prose extractor
   only trusts a category/severity that appears *uniquely within a block*, and
   never extracts `evidence` verbatim — the aggregator re-reads source lines by
   the reported line number instead.

### Measured results (quixbugs, deepseek-v4-flash, thinking off)

| Metric | Java (40 buggy) | Python (40 buggy) |
| --- | --- | --- |
| Recall (line-level) | 57.5% | 60.0% |
| File-level precision | 84.4% | 87.8% |
| JSON failures | 0 | 0 |
| Wall time (full run) | 28.3s | 34.2s |
| Tokens | 59k | 61k |

Recall is reported **line-level**: a finding must cover the actual bug line
(`scripts/analyze.py --correct-dir` diffs the corrected program to locate it).
File-level recall (~95%/~90%) merely counts a finding landing anywhere in the
buggy file, so it inflates the number and is no longer the headline metric.

Common misses (model capability boundary, not config): quicksort (drops
pivot-equal elements) and reverse_linked_list (pointer mis-wiring) — both are
subtle logic bugs in both languages.

## Design notes

- **No wall-clock kill switch on LLM calls.** Python threads cannot be force
  killed, so a "node-level timeout" wrapping a blocking call is fake —
  `ThreadPoolExecutor.__exit__` blocks on shutdown anyway. Real timeouts come
  from `httpx`; on top of that, transient errors retry with exponential backoff.
- **Thread-safe token accounting.** The client guards its counters with a lock.
- **Failed-block ledger deduplicates and resolves.** A custom reducer keys
  `failed_blocks` by `(file, line range)` and drops a block once retried, so
  `--retry-failed` never re-runs a recovered block.

## Not in scope (deliberately)

- **symbol_backend / distill / aging** — the old config declared these but the
  implementations never existed; they were dead switches, not features.
