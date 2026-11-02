# lexsearch

lexsearch is a faster, about 17x cheaper alternative to omp's scout for "find that thing I did or wrote" lookups.

It is a local BM25 search over your omp session transcripts, your markdown notes and your code repos.
It also ships an omp agent that rewrites your fuzzy question into keywords, searches, judges the hits and retries.

## Headline numbers

Sessions and notes, 300 fuzzy requests per agent, one run per request ([method and caveats](BENCHMARK.md)).

| Setup | Recall@5 | p50 time | Cost per run |
|---|---|---|---|
| Stock omp `scout` | 97.7% | 21.4 s | $0.0435 |
| `retriever` (lexsearch) | 97.7% | 12.1 s | $0.0025 |

## Quick start

1. Clone the repo:

   ```
   git clone https://github.com/Sipty/lexsearch.git
   cd lexsearch
   ```

2. Install, pointing it at your notes folder:

   ```
   ./install.sh --vault ~/path/to/notes
   ```

3. Ask omp, for example:

   ```
   use the retriever to find the session where we fixed the login redirect
   ```

If `~/.local/bin` is not on your PATH, the installer says so and prints the line to add.

## Requirements

- Python 3 with SQLite FTS5. The stock Python on macOS and most Linux distros has it, and the installer checks.
- omp, for the agent and skill.
  The agent runs on omp's `smol` role.
  The benchmark used Claude Haiku there; gpt-oss models failed as agents (see [BENCHMARK.md](BENCHMARK.md)).
- The CLI alone needs only Python 3, with the standard library.

## What gets installed

| Path | What it is |
|---|---|
| `~/.local/bin/lexsearch` | Symlink to `lexsearch.py` in the clone, so `git pull` updates it |
| `~/.omp/agent/agents/retriever.md` | The omp agent |
| `~/.omp/agent/skills/lexsearch/SKILL.md` | The skill the agent loads |
| `~/.omp/agent/glossary.md` | Your jargon list, copied from `glossary.example.md` (never overwritten) |
| `~/.config/lexsearch/config.json` | Which folders to index |

The installer never overwrites a different file without `--force`.
It prints what it skipped.

## CLI

```
lexsearch index [--corpus all|vault|sessions|code]
lexsearch search [--corpus all|vault|sessions|code] [-k 10] [--kind root|sub|any] [--json] QUERY [QUERY ...]
lexsearch docs --corpus vault|sessions|code|all [--kind root|sub|any] --json
```

- Pass several queries at once. Each one is searched on its own, and the lists are fused.
- Always pass your original question first. Keyword rewrites alone lose exact phrases.
- `index` is incremental.
- `search` refreshes the corpus before it queries.
- `--corpus all` includes code.
- A corpus that is not configured stays empty and prints one line on stderr.

## Configuration

`~/.config/lexsearch/config.json`:

```json
{
  "vault": "~/Documents/notes",
  "sessions": "~/.omp/agent/sessions",
  "code": ["~/src/my-app", "~/src/other-repo"]
}
```

- `vault`: a folder of markdown notes, or `null` for none.
- `sessions`: the omp sessions folder.
- `code`: git repos.
  Only git-tracked files are indexed.
  Two repos with the same folder name are rejected, because doc paths use the repo name.

Environment variables override the config file.

| Variable | Default | Purpose |
|---|---|---|
| `LEXSEARCH_CONFIG` | `~/.config/lexsearch/config.json` | Config file path |
| `LEXSEARCH_VAULT_ROOT` | from config | Notes folder |
| `LEXSEARCH_SESSIONS_ROOT` | `~/.omp/agent/sessions` | Sessions folder |
| `LEXSEARCH_CODE_ROOTS` | from config | Repos, separated by the OS path separator (`:` on macOS and Linux) |
| `LEXSEARCH_DB` | `~/.cache/lexsearch/index.db` | Index file |
| `LEXSEARCH_FROZEN=1` | off | Search the index as-is, without refreshing it (for reproducible benchmarks) |

The code corpus skips binaries, files over 300 KB, lockfiles, minified assets, snapshots, `node_modules`, `dist`, `build`, Django migrations, and secret files such as `.env`, keys and certificates.

## How it works

```mermaid
flowchart LR
  Q[Your question] --> R[Agent rewrites into 1-3 keyword queries]
  R --> L[lexsearch: BM25 per query, fused with RRF]
  L --> J[Agent judges the top 5]
  J -- good enough --> A[Paths, titles, dates]
  J -- no --> R
```

- The CLI does no LLM work.
- The agent does the rewriting and the judging.
- BM25 weights titles 10x over bodies.
- Each query's top 50 are fused with reciprocal-rank fusion (k=60).
- Everything lives in one SQLite file.
- Changed files are found by mtime and size.

The idea is recipe 2 from [RAG is simpler than you think](https://www.lighthousenewsletter.com/p/rag-is-simpler-than-you-think): a small model rewrites the question, plain keyword search does the rest, no embeddings.

## What it is NOT for

- Code mapping and investigation stay with scout.
- lexsearch finds a known item, such as "which file held the yearly coefficient", and scout explores how code fits together.
- It is not semantic search.
- It matches words, so rewrites and the glossary carry the synonyms.

## Adding more sources

The skill explains how to plug other keyword search tools into the same loop, such as a Slack search tool or an Atlassian search CLI.

## Uninstall

```
rm ~/.local/bin/lexsearch
rm ~/.omp/agent/agents/retriever.md
rm ~/.omp/agent/skills/lexsearch/SKILL.md
rm ~/.omp/agent/glossary.md
```

Optional: remove `~/.config/lexsearch` and `~/.cache/lexsearch` too.
Keep the glossary if you edited it.

## Benchmark

The method, the other sources and the caveats are in [BENCHMARK.md](BENCHMARK.md).

## License

MIT. See [LICENSE](LICENSE).
