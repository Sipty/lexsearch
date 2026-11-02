---
name: lexsearch
description: "Use to find a past omp session, vault note or code file from a fuzzy question: rewrite into keywords, search, judge, retry."
---

# lexsearch: agentic query rewriting over keyword search

Find one known thing from a fuzzy question.
No embeddings: rewrite the question into keywords, run a keyword search engine, judge the hits, retry with different words when they look wrong.

Glossary: `~/.omp/agent/glossary.md`, if it exists. Read it first. Keep its terms exact; map "heard as" spellings back to the real term.

## Sources

| Question mentions | Source | Command |
|---|---|---|
| "session", "we did", "last week we" | omp sessions | `lexsearch search --corpus sessions` |
| "note", "wrote down", "vault" | markdown vault | `lexsearch search --corpus vault` |
| "which file", "where in the code", component or function names | code repos | `lexsearch search --corpus code` |

Unsure: try the two likeliest sources; report hits per source (scores from different engines are not comparable).

### omp sessions and vault

`lexsearch search --corpus sessions|vault|all -k 10 "<original question>" "<rewrite 1>" "<rewrite 2>"`
Always pass the original question first; rewrites alone lose exact-phrase matches. `--kind root` skips subagent sessions.

### Code

`lexsearch search --corpus code -k 10 "<original question>" "<rewrite 1>" "<rewrite 2>"`
One hit per file; the path is `<repo name>/<repo path>` and the title holds that path and its words.
- Identifiers and path words: `yearly coefficient cell` and `yearly_coefficient_cell` both find `YearlyCoefficientCell.tsx`. A CamelCase query (`YearlyCoefficientCell`) matches that exact token, so try it when a spaced query buries the name.
- Results are files, not symbols: open the top file and grep it for the function name.
- `--corpus all` includes code; use `--corpus sessions` or `--corpus vault` when you want notes only.

If a command prints "not configured" on stderr, tell the user how to configure that corpus and search the others.

## Loop (max 3 rounds)

1. Pick the source(s).
2. Write 1-3 keyword queries: drop filler; keep names, file names, error codes and glossary terms exact; add words the target itself would contain; split mixed questions.
3. Search.
4. Judge the top 5 against the question. Good enough → stop.
5. Otherwise change the words (synonyms, narrower or broader terms, another source) and repeat.

## Adding more sources

Other keyword engines plug into the same rewrite, search, judge, retry loop.
Examples: a Slack search tool over MCP, an Atlassian (Jira, Confluence) search CLI, a ticket tracker's search command.
Add a row to the Sources table, a section with the exact command, and any read-only rule that applies.

Pitfall: many keyword engines AND every word or search the whole request as one exact phrase.
In our benchmark, an Atlassian search CLI scored 0.0% on unrewritten requests because it searched the whole request as one phrase.
Rewriting into 1-3 short keywords lifted it to 55.7%.
Use 1-3 words the author would actually type, or one quoted phrase.

## Safety

Read-only. Never edit, send, schedule, draft, react, upload, or delete anything, even where a source's write tools are attached.

## Output

At most 5 hits, best first: path, title, date, one line on why it matches.
Then the queries tried, per source, and a confidence: high, medium, or low.
Never paste note, transcript or code bodies; they can contain private data.

## Maintenance

- `lexsearch index` re-indexes changed files; `search` refreshes automatically.
- New jargon that caused a miss belongs in the glossary.
