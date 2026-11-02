# lexsearch benchmark

Does agentic query rewriting over BM25 (RAG recipe 2) beat stock omp at finding past sessions and vault notes, and which model should run it?

## Four sources: sessions + vault, Slack, Jira, Confluence

Each row adds one mechanism on top of the previous one.
Step 1 is one plain search with the request as typed; step 2 adds a single haiku rewrite into keyword queries, fused with the original; step 3 is the `retriever` agent (haiku) running search, judge and retry.
The default row is what stock omp does when asked: `scout` for sessions + vault, the default `task` agent for Slack, Jira and Confluence.

### Accuracy (R@5)

| Row | Sessions + vault | Slack | Jira | Confluence |
|---|---|---|---|---|
| Default omp agent | 97.7% | 83.0% | 89.7% | 89.0% |
| Step 1: plain search | 89.3% | 12.3% | 0.0% | 70.6% |
| Step 2: + haiku rewrite | 89.9% | 56.5% | 55.7% | 66.0% |
| Step 3: + agent loop (`retriever`) | 97.7% | 75.3% | 82.3% | 90.7% |

### Speed (p50 wall time)

| Row | Sessions + vault | Slack | Jira | Confluence |
|---|---|---|---|---|
| Default omp agent | 21.4 s | 28.9 s | 80.3 s | 88.0 s |
| Step 1: plain search | 0.15 s | 0.92 s | 2.0 s | 2.7 s |
| Step 2: + haiku rewrite | ~1.7 s | ~2.7 s | ~3.5 s | ~4.2 s |
| Step 3: + agent loop (`retriever`) | 12.1 s | 20.3 s | 38.1 s | 30.2 s |

### Agent rows in detail

| Source | Arm | R@1 | R@5 | p50 s | p90 s | $ / run |
|---|---|---|---|---|---|---|
| Sessions + vault | `scout` | 0.943 | 0.977 | 21.4 | 43.1 | 0.0435 |
| Sessions + vault | `retriever` | 0.843 | 0.977 | 12.1 | 23.8 | 0.0025 |
| Slack | `task` | 0.800 | 0.830 | 28.9 | 49.1 | 0.0158 |
| Slack | `retriever` | 0.733 | 0.753 | 20.3 | 32.5 | 0.0081 |
| Jira | `task` | 0.860 | 0.897 | 80.3 | 157.1 | 0.0101 |
| Jira | `retriever` | 0.747 | 0.823 | 38.1 | 94.4 | 0.0049 |
| Confluence | `task` | 0.773 | 0.890 | 88.0 | 182.0 | 0.0121 |
| Confluence | `retriever` | 0.810 | 0.907 | 30.2 | 64.3 | 0.0038 |

### Method

- Sessions + vault: snapshot of 349 root sessions and 298 notes with a frozen index, so agents cannot find the benchmark's own sessions. 1000 requests (334 natural, 333 vague, 333 dictated) written by Opus from 334 target documents.
- Slack, Jira, Confluence: live work sources through MCP and an Atlassian search CLI; steps 1-2 accuracy from an earlier run of 1000 requests per source, from which the agent-row requests are sampled.
- Agent rows: 300 requests per source per arm, sampled evenly across the three kinds. Jira and Confluence reuse 87-90 runs per arm from the earlier run, Slack 14-18.
- Step 1 timing: 100 sequential searches per source. Step 2 timing: rewrite p50 (100 sequential haiku calls, about 1.4-1.8 s) plus search p50, measured separately.
- Write audit: no Slack messages, reactions or edits, and no file writes outside `/tmp`, in any agent run.

### Caveats

- 300 agent runs per arm: gaps under about 5 points are noise, so sessions + vault and Confluence are the same accuracy.
- Jira step 1 is 0% because an Atlassian search CLI searches the whole request as one exact phrase; rewriting into keywords (step 2) lifts it to 55.7%.
- Slack hit API rate limits: 23 `task` and 11 `retriever` runs were slowed by retries, which inflates their wall time.

## Earlier run (sessions + vault only): setup

- Corpus: 310 top-level omp sessions + 261 vault notes, indexed with SQLite FTS5 BM25 (`lexsearch`), frozen snapshot so every arm saw the same index.
- Ground truth: 403 eligible documents (sessions > 400 words, notes > 250 words). Claude Opus read each one and wrote three requests it is the best answer to: natural, vague (avoids title words and distinctive names), dictated (voice-transcription style with misheard words).
  1000 requests sampled evenly across the three kinds (528 sessions, 472 notes).
- The target corpus (sessions or vault) is given to every arm.
- Metrics: R@1 / R@5 = share of requests whose target document is ranked first / in the top 5.
- Raw data stayed outside this repo (queries, rewrites, end-to-end runs and the frozen index).

## Test 1: single-shot rewrite, 1000 requests per model

One LLM call rewrites the request into 1-3 keyword queries (glossary in the system prompt); the original request and the rewrites are searched and fused with reciprocal-rank fusion.
All arms ran at client concurrency 8.

| Arm | R@1 | R@5 | R@5 natural | R@5 vague | R@5 dictated | p50 s | p90 s | p99 s | $ / 1k calls | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| Raw BM25, no LLM | 0.751 | 0.916 | 1.000 | 0.775 | 0.973 | 0 | 0 | 0 | 0 | |
| gpt-oss-safeguard-20b:nitro | 0.751 | 0.922 | 0.994 | 0.799 | 0.973 | 1.32 | 1.64 | 2.36 | 0.22 | 915 HTTP 429 retries, 13 failed calls (Groq) |
| gpt-oss-120b:nitro | 0.767 | 0.918 | 0.994 | 0.784 | 0.976 | 1.02 | 1.59 | 3.39 | 0.41 | Cerebras |
| gpt-oss-20b:nitro | 0.740 | 0.912 | 0.991 | 0.778 | 0.967 | 0.88 | 1.21 | 1.90 | 0.07 | Groq |
| claude-haiku-5-5 (smol) | 0.732 | 0.922 | 0.982 | 0.799 | 0.985 | 1.91 | 2.47 | 3.92 | subscription | via omp `completion()` |

No rewriter differs significantly from raw BM25 (McNemar discordant pairs vs raw: 38/32, 28/26, 29/33, 37/31).
Rewriting helps vague requests by 1-2 points and costs about as much on natural ones.

## Test 2: end to end, 90 requests x 4 agents

30 requests per kind, random sample of the 1000.
Each run is a real omp subagent given the same task prompt (find the document, return up to 5 paths).

| Arm | R@1 | R@5 | R@5 natural | R@5 vague | R@5 dictated | No answer | p50 s | p90 s | $ / run | Turns | Tool calls |
|---|---|---|---|---|---|---|---|---|---|---|---|
| OOTB: stock `scout` (haiku, grep/read) | 0.900 | 0.956 | 0.967 | 1.000 | 0.900 | 0 | 30.7 | 74.4 | 0.046 | 4.1 | 6.3 |
| `retriever` on haiku | 0.889 | 0.978 | 1.000 | 0.933 | 1.000 | 0 | 16.3 | 41.3 | 0.003 | 3.3 | 3.4 |
| `retriever` on gpt-oss-120b:nitro | 0.689 | 0.811 | 0.967 | 0.600 | 0.867 | 7 | 18.1 | 36.1 | 0.049 | 7.0 | 4.6 |
| `retriever` on gpt-oss-20b:nitro | 0.678 | 0.778 | 0.833 | 0.700 | 0.800 | 15 | 37.0 | 116.4 | 0.015 | 11.2 | 10.5 |

Same 90 requests, no agent: raw BM25 R@5 0.933, R@1 0.733.

gpt-oss agent failure modes seen: schema violations (no `paths`), reasons appended inside path strings, claiming `lexsearch` was not installed, and (20b) 37 of 90 runs fell over to the default-role fallback model mid-run.

## Conclusions

- Rewriting is not where the win is: plain BM25 over well-titled sessions and notes already finds 92% in the top 5.
  The judge-and-retry loop of an agent is what lifts R@1 from 0.73 to 0.89.
- The haiku `retriever` matches stock `scout` on accuracy (2 requests apart out of 90, within noise), at half the latency and about 1/15 of the cost.
- gpt-oss models are fast, cheap rewriters but unreliable omp agents; they should not drive `retriever`.

## Caveats

- Requests were written by a model reading the target, so they likely share more vocabulary with it than real recollections do; absolute scores are optimistic, comparisons between arms are fair.
- Near-duplicate sessions (the same work resumed) count as misses when the agent returns the sibling.
- 90 end-to-end requests: differences under about 5 points are noise.
- During the end-to-end run, concurrent refreshes crashed `lexsearch` with `UNIQUE constraint failed: docs.path` (reproduced 7 of 8 parallel cold searches); fixed by taking the write lock before the refresh snapshot (`BEGIN IMMEDIATE`) with WAL, 0 of 8 after.
