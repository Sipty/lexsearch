# Search glossary

Jargon for keyword search over your sessions, notes and code.
Used by the `lexsearch` skill and the `retriever` agent when rewriting questions into BM25 keyword queries.
Rule: keep every term below exact in queries; add the listed synonyms only when they help.

Copy this file to `~/.omp/agent/glossary.md` and replace the example rows with your own jargon.
The "Heard as" column is for voice dictation: speech-to-text garbles some words, and this column maps the garbled spelling back to the real term.
A query for "Py" finds nothing useful, so the agent rewrites it to "omp" first.

## Projects

| Term | Meaning | Synonyms / related keywords | Heard as |
|---|---|---|---|
| ProjectName | One-line description of the project | repo, service, codename | |
| billing-api | Example service that issues invoices | invoice, payments | billing a p i |

## Tools

| Term | Meaning | Synonyms / related keywords | Heard as |
|---|---|---|---|
| omp | Agent harness (oh-my-pi) | pi, harness, subagent, skill | Py, Pi |
| scout | Read-only research subagent | subagent, explore | |
| lexsearch | BM25 search CLI over sessions, notes and code | retriever, FTS5 | |
| Excalidraw | Diagram tool | drawing, diagram | Excalibur |
