#!/usr/bin/env python3
"""
lexsearch: local BM25 full-text search over markdown notes, omp session transcripts and code repos.

The CLI does no LLM work. Callers (agents) rewrite questions into keyword queries and
pass several of them at once; each query is an OR of quoted terms ranked by FTS5 bm25
(title weighted 10x body). Per-query top lists are fused with reciprocal-rank fusion
(k=60), so a result only needs to rank well for one phrasing to surface.

Configuration: a JSON file (~/.config/lexsearch/config.json, or LEXSEARCH_CONFIG) with
"vault", "sessions" and "code". Environment variables override the file. A corpus that
is not configured stays empty, and index/search say how to configure it on stderr.

Decisions:
- Index is a single SQLite file with an external-content FTS5 table, so bodies are stored once.
- Indexing is stat-based (mtime + size). Search refreshes the requested corpus before querying
  unless LEXSEARCH_FROZEN=1, which reads the index as-is (for reproducible benchmarks).
- Session bodies carry only user/assistant text blocks; thinking, tool calls and tool results
  are skipped (noisy, large, and more likely to contain customer data).
- Vault notes all get kind "note", since the root/sub distinction only applies to sessions.
- Code files: kind is the file extension; the title holds the canonical path and its words.
  Only git-tracked files of the configured repos are indexed. A doc path is
  "<repo basename>/<repo-relative path>", so two repos with the same basename would collide
  and are rejected. A refresh deletes only docs whose path is no longer listed, so one repo
  never removes another repo's docs.
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time

CONFIG_PATH = os.path.abspath(
    os.path.expanduser(os.environ.get("LEXSEARCH_CONFIG") or "~/.config/lexsearch/config.json")
)
DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".cache", "lexsearch", "index.db")
DB_PATH = os.environ.get("LEXSEARCH_DB") or DEFAULT_DB
FROZEN = os.environ.get("LEXSEARCH_FROZEN") == "1"
CORPORA = ("vault", "sessions", "code")
BODY_CAP = 200_000       # max chars of session body kept in the index
TOP_PER_QUERY = 50       # candidates taken from each query before fusion
RRF_K = 60
TITLE_WEIGHT = 10.0
BODY_WEIGHT = 1.0
SNIPPET_TOKENS = 16

# Cheap substring prefilter so most JSONL lines are never parsed.
SESSION_MARKERS = ('"type":"message"', '"type":"title"', '"type":"session"')

CODE_MAX_BYTES = 300 * 1024
CODE_SKIP_DIRS = frozenset({"node_modules", "dist", "build"})
CODE_SKIP_NAMES = frozenset({"package-lock.json", "pnpm-lock.yaml"})
CODE_SKIP_SUFFIXES = (".lock", ".min.js", ".min.css", ".svg", ".snap")
CODE_BINARY_EXTS = frozenset([
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tif", ".tiff", ".heic", ".avif",
    ".pdf", ".zip", ".gz", ".tgz", ".tar", ".7z", ".rar", ".whl", ".jar", ".class", ".pyc", ".so",
    ".dylib", ".o", ".a", ".exe", ".bin", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4",
    ".mov", ".m4a", ".wav", ".webm", ".xlsx", ".xls", ".docx", ".pptx", ".sqlite", ".db", ".parquet",
    ".pkl", ".onnx", ".npy", ".npz",
])
# Keys, certificates and credentials: their contents never go into the searchable index.
CODE_SECRET_EXTS = frozenset({".key", ".pem", ".crt", ".csr", ".p12", ".pfx", ".jks", ".keystore", ".creds", ".env"})


def _load_config():
    """Read the JSON config file. A missing file means defaults; a broken one is an error."""
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"lexsearch: cannot read config {CONFIG_PATH}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"lexsearch: config {CONFIG_PATH} must hold a JSON object")
    return data


def _abs(path):
    return os.path.abspath(os.path.expanduser(path))


_CONFIG = _load_config()

# Vault: null or unset in the config and no env override means the corpus is empty.
_vault_raw = os.environ.get("LEXSEARCH_VAULT_ROOT") or _CONFIG.get("vault")
VAULT_ROOT = _abs(_vault_raw) if _vault_raw else None

SESSIONS_ROOT = _abs(
    os.environ.get("LEXSEARCH_SESSIONS_ROOT") or _CONFIG.get("sessions") or "~/.omp/agent/sessions"
)

# Code repos: LEXSEARCH_CODE_ROOTS (os.pathsep-separated) wins over the config list.
if os.environ.get("LEXSEARCH_CODE_ROOTS"):
    _code_raw = [p for p in os.environ["LEXSEARCH_CODE_ROOTS"].split(os.pathsep) if p.strip()]
else:
    _code_raw = _CONFIG.get("code") or []
    if not isinstance(_code_raw, list) or not all(isinstance(p, str) for p in _code_raw):
        raise SystemExit(f"lexsearch: \"code\" in {CONFIG_PATH} must be a list of paths")
CODE_ROOTS = [_abs(p) for p in _code_raw]
# Doc path prefix -> repo root, for reading a file back from its canonical path.
CODE_BY_PREFIX = {os.path.basename(root): root for root in CODE_ROOTS}

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY,
    corpus TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    date TEXT NOT NULL,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS docs_corpus ON docs (corpus, kind);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, body, content='docs', content_rowid='id', tokenize='porter unicode61'
);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    # Several agents may search (and so refresh) at once: wait for the write lock
    # instead of failing, and use WAL so readers never block on a refresh.
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def check_code_roots():
    """Reject configurations whose repos would produce colliding doc paths."""
    seen = {}
    for root in CODE_ROOTS:
        name = os.path.basename(root)
        if not name:
            raise SystemExit(f"lexsearch: code root has no name: {root}")
        if name in seen:
            raise SystemExit(
                f"lexsearch: two code repos are named '{name}': {seen[name]} and {root}. "
                "Doc paths use the repo name, so keep only one of them or rename a checkout."
            )
        seen[name] = root


def unconfigured_notice(corpora):
    """One stderr line per requested corpus that has nothing configured."""
    if "vault" in corpora and VAULT_ROOT is None:
        print(
            "lexsearch: vault not configured, no notes indexed. Run install.sh --vault PATH, "
            f"or set \"vault\" in {CONFIG_PATH}, or LEXSEARCH_VAULT_ROOT.",
            file=sys.stderr,
        )
    if "code" in corpora and not CODE_ROOTS:
        print(
            "lexsearch: no code repos configured, no code indexed. Run install.sh --code PATH, "
            f"or set \"code\" in {CONFIG_PATH}, or LEXSEARCH_CODE_ROOTS.",
            file=sys.stderr,
        )


# ---------- file discovery ----------

def list_vault():
    """Yield (path, kind, stat) for every markdown note, skipping dot-directories."""
    if VAULT_ROOT is None or not os.path.isdir(VAULT_ROOT):
        return
    for dirpath, dirnames, filenames in os.walk(VAULT_ROOT):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".md") and not fn.startswith("."):
                p = os.path.join(dirpath, fn)
                try:
                    yield p, "note", os.stat(p)
                except OSError:
                    continue


def session_kind(path):
    # Depth 2 relative to the sessions root (<project>/<file>.jsonl) is a root session;
    # anything deeper is a subagent session.
    rel = os.path.relpath(path, SESSIONS_ROOT)
    return "root" if rel.count(os.sep) == 1 else "sub"


def list_sessions():
    if not os.path.isdir(SESSIONS_ROOT):
        return
    for dirpath, dirnames, filenames in os.walk(SESSIONS_ROOT):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".jsonl") and not fn.startswith("."):
                p = os.path.join(dirpath, fn)
                try:
                    yield p, session_kind(p), os.stat(p)
                except OSError:
                    continue


def code_skipped(rel):
    parts = rel.split("/")
    name = parts[-1]
    if any(d in CODE_SKIP_DIRS for d in parts[:-1]):
        return True
    if name in CODE_SKIP_NAMES or name.endswith(CODE_SKIP_SUFFIXES):
        return True
    if "migrations" in parts[:-1] and re.fullmatch(r"\d{4}_.*\.py", name):
        return True
    ext = os.path.splitext(name)[1].lower()
    if name == ".env" or (name.startswith(".env.") and ext not in (".example", ".sample", ".template")):
        return True  # env files carry secrets; templates (.env.example) stay indexed
    return ext in CODE_BINARY_EXTS or ext in CODE_SECRET_EXTS


def list_code():
    """Yield (canonical path, kind, lstat) for tracked code files of every configured repo."""
    for root in CODE_ROOTS:
        if not os.path.isdir(root):
            print(f"warning: code repo not found, its docs are dropped: {root}", file=sys.stderr)
            continue
        prefix = os.path.basename(root)
        res = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, check=False)
        if res.returncode != 0:
            # Fail loudly: an empty listing would make the refresh delete every doc of this repo.
            raise SystemExit(f"lexsearch: git ls-files failed in {root}: {res.stderr.decode(errors='replace').strip()}")
        for raw in res.stdout.split(b"\0"):
            if not raw:
                continue
            rel = os.fsdecode(raw)
            if code_skipped(rel):
                continue
            try:
                st = os.lstat(os.path.join(root, rel))
            except OSError:
                continue
            # Symlinks, submodules, tracked-but-deleted and oversized files are not indexed.
            if not stat.S_ISREG(st.st_mode) or st.st_size > CODE_MAX_BYTES:
                continue
            yield f"{prefix}/{rel}", os.path.splitext(rel)[1][1:], st


LISTERS = {"vault": list_vault, "sessions": list_sessions, "code": list_code}


# ---------- parsing ----------

def date_from_mtime(mtime):
    return datetime.datetime.fromtimestamp(mtime).date().isoformat()


def parse_vault(path, st):
    with open(path, encoding="utf-8", errors="replace") as f:
        body = f.read()
    title = os.path.splitext(os.path.basename(path))[0]
    return title, date_from_mtime(st.st_mtime), body


def block_text(content):
    """Concatenate text blocks of a message content field (list of blocks or plain string)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b["text"]
        for b in content
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    )


def recap_text(data):
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("text", "summary", "recap"):
            if isinstance(data.get(key), str):
                return data[key]
    return ""


def parse_session(path, st):
    cwd = ""
    start = None
    session_title = ""
    titles = ""
    parts = []
    total = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not (any(m in line for m in SESSION_MARKERS) or "recap" in line):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue  # tolerate malformed lines
            if not isinstance(e, dict):
                continue
            t = e.get("type")
            if t == "session":
                cwd = e.get("cwd") or cwd
                start = e.get("timestamp") or start
                session_title = e.get("title") or session_title
                continue
            if t == "title":
                if isinstance(e.get("title"), str) and e["title"]:
                    titles = e["title"]  # last one wins
                continue
            if t == "message":
                msg = e.get("message") or {}
                if msg.get("role") not in ("user", "assistant"):
                    continue
                text = block_text(msg.get("content"))
            elif t == "custom" and "recap" in str(e.get("customType", "")):
                text = recap_text(e.get("data"))
            else:
                continue
            if not text or total >= BODY_CAP:
                continue
            parts.append(text)
            total += len(text) + 1
    title = titles or session_title or os.path.splitext(os.path.basename(path))[0]
    date = start[:10] if isinstance(start, str) and len(start) >= 10 else date_from_mtime(st.st_mtime)
    body = "\n".join(([cwd] if cwd else []) + parts)[:BODY_CAP]
    return title, date, body


def code_title(prefix, rel):
    """Title: canonical path, then its words, with CamelCase, snake_case and dots split out."""
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", rel)
    words = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", words)
    words = re.sub(r"[\W_]+", " ", words).strip()
    return f"{prefix}/{rel} {words}"


def parse_code(path, st):
    prefix, _, rel = path.partition("/")
    with open(os.path.join(CODE_BY_PREFIX[prefix], rel), "rb") as f:
        head = f.read(8192)
        if b"\0" in head:
            return None  # binary despite the extension: not indexed
        data = head + f.read()
    return code_title(prefix, rel), date_from_mtime(st.st_mtime), data.decode("utf-8", errors="replace")


PARSERS = {"vault": parse_vault, "sessions": parse_session, "code": parse_code}


# ---------- indexing ----------

def delete_doc(conn, doc_id):
    row = conn.execute("SELECT title, body FROM docs WHERE id = ?", (doc_id,)).fetchone()
    if row:
        # External-content FTS requires the exact indexed values to remove a row.
        conn.execute(
            "INSERT INTO docs_fts (docs_fts, rowid, title, body) VALUES ('delete', ?, ?, ?)",
            (doc_id, row[0], row[1]),
        )
    conn.execute("DELETE FROM docs WHERE id = ?", (doc_id,))


def insert_doc(conn, corpus, kind, path, title, date, st, body):
    cur = conn.execute(
        "INSERT INTO docs (corpus, kind, path, title, date, mtime, size, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (corpus, kind, path, title, date, st.st_mtime, st.st_size, body),
    )
    conn.execute(
        "INSERT INTO docs_fts (rowid, title, body) VALUES (?, ?, ?)",
        (cur.lastrowid, title, body),
    )


def index_corpus(conn, corpus):
    """Bring one corpus in line with disk. Returns a stats dict."""
    t0 = time.perf_counter()
    # Take the write lock before snapshotting stored rows; otherwise two concurrent
    # refreshes both see a new file as missing and the second insert hits UNIQUE(path).
    conn.execute("BEGIN IMMEDIATE")
    stored = {
        path: (doc_id, mtime, size)
        for path, doc_id, mtime, size in conn.execute(
            "SELECT path, id, mtime, size FROM docs WHERE corpus = ?", (corpus,)
        )
    }
    seen = set()
    added = updated = unchanged = binary = 0
    parse = PARSERS[corpus]
    for path, kind, st in LISTERS[corpus]():
        seen.add(path)
        old = stored.get(path)
        if old and old[1] == st.st_mtime and old[2] == st.st_size:
            unchanged += 1
            continue
        try:
            parsed = parse(path, st)
        except OSError as exc:
            print(f"warning: skipping {path}: {exc}", file=sys.stderr)
            continue
        if old:
            delete_doc(conn, old[0])
        if parsed is None:  # not indexable (binary): drop any stale copy, index nothing
            binary += 1
            continue
        title, date, body = parsed
        if old:
            updated += 1
        else:
            added += 1
        insert_doc(conn, corpus, kind, path, title, date, st, body)
    removed = 0
    for path, (doc_id, _, _) in stored.items():
        if path not in seen:
            delete_doc(conn, doc_id)
            removed += 1
    conn.commit()
    counts = dict(conn.execute(
        "SELECT kind, COUNT(*) FROM docs WHERE corpus = ? GROUP BY kind", (corpus,)
    ).fetchall())
    return {
        "corpus": corpus,
        "added": added,
        "updated": updated,
        "removed": removed,
        "unchanged": unchanged,
        "binary": binary,
        "counts": counts,
        "seconds": time.perf_counter() - t0,
    }


def corpora_for(choice):
    return CORPORA if choice == "all" else (choice,)


def cmd_index(args):
    unconfigured_notice(corpora_for(args.corpus))
    conn = connect()
    t0 = time.perf_counter()
    for corpus in corpora_for(args.corpus):
        s = index_corpus(conn, corpus)
        kinds = ", ".join(f"{k}={v}" for k, v in sorted(s["counts"].items())) or "empty"
        skipped = f" binary-skipped={s['binary']}" if s["binary"] else ""
        print(
            f"{s['corpus']}: docs [{kinds}] | +{s['added']} ~{s['updated']} "
            f"-{s['removed']} ={s['unchanged']}{skipped} | {s['seconds']:.2f}s"
        )
    print(f"total {time.perf_counter() - t0:.2f}s (db {DB_PATH})")
    conn.close()


# ---------- search ----------

# English function words and speech filler. They occur in nearly every document, so
# bm25 gives them ~zero weight, yet OR-ing them makes FTS5 score the whole corpus:
# a 20-word question took ~20 s on the code corpus. Words that are also code terms
# (get, find, sort, like, all, any, filter) stay searchable.
STOPWORDS = frozenset([
    "a", "about", "above", "after", "again", "against", "am", "an", "and", "are", "as", "at",
    "be", "because", "been", "before", "being", "below", "between", "both", "but", "by", "could",
    "did", "do", "does", "doing", "during", "each", "few", "for", "from", "further", "had", "has",
    "have", "having", "he", "her", "here", "hers", "herself", "him", "himself", "his", "how", "i",
    "if", "in", "into", "is", "it", "its", "itself", "just", "me", "my", "myself", "nor", "of",
    "once", "or", "our", "ours", "ourselves", "own", "she", "should", "so", "than", "that", "the",
    "their", "theirs", "them", "themselves", "then", "there", "these", "they", "this", "those",
    "through", "to", "too", "until", "very", "was", "we", "were", "what", "when", "where", "which",
    "while", "who", "whom", "why", "will", "with", "would", "you", "your", "yours", "yourself",
    "yourselves", "um", "uh", "basically", "actually", "maybe", "really", "ok", "okay",
])


def query_tokens(query):
    """Word tokens of a query minus stopwords; falls back to all tokens if only stopwords."""
    tokens = re.findall(r"\w+", query)
    kept = [t for t in tokens if t.lower() not in STOPWORDS]
    return kept or tokens


def fts_query(tokens):
    # Tokens are \w+ only, so quoting makes them safe from FTS5 operators.
    return " OR ".join(f'"{t}"' for t in tokens)


def cmd_search(args):
    unconfigured_notice(corpora_for(args.corpus))
    conn = connect()
    corpora = corpora_for(args.corpus)
    if not FROZEN:
        for corpus in corpora:
            index_corpus(conn, corpus)

    kind_clause = "" if args.kind == "any" else " AND d.kind = ?"
    corpus_marks = ",".join("?" for _ in corpora)
    # Rank without snippet(): SQLite evaluates result columns for every matching row
    # before sorting, and snippet() over thousands of large docs (long OR queries on
    # the code corpus) cost ~30 s per search. Snippets are fetched for final hits only.
    sql = (
        "SELECT d.id, d.path, d.title, d.date, d.corpus, d.kind, "
        f"bm25(docs_fts, {TITLE_WEIGHT}, {BODY_WEIGHT}) AS score "
        "FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid "
        f"WHERE docs_fts MATCH ? AND d.corpus IN ({corpus_marks}){kind_clause} "
        "ORDER BY score LIMIT ?"
    )
    snippet_sql = (
        f"SELECT snippet(docs_fts, 1, '[', ']', ' ... ', {SNIPPET_TOKENS}) "
        "FROM docs_fts WHERE docs_fts MATCH ? AND rowid = ?"
    )

    fused = {}   # doc id -> fused score
    info = {}    # doc id -> display fields
    matched = {}  # doc id -> set of query indices
    for qi, query in enumerate(args.query):
        tokens = query_tokens(query)
        if not tokens:
            continue
        match = fts_query(tokens)
        params = [match, *corpora]
        if args.kind != "any":
            params.append(args.kind)
        params.append(TOP_PER_QUERY)
        rows = conn.execute(sql, params).fetchall()
        for rank, (doc_id, path, title, date, corpus, kind, _score) in enumerate(rows, 1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (RRF_K + rank)
            matched.setdefault(doc_id, set()).add(qi)
            if doc_id not in info:
                # Remember the first query that matched; its terms drive the snippet.
                info[doc_id] = {
                    "path": path, "title": title, "date": date,
                    "corpus": corpus, "kind": kind, "_match": match,
                }

    ranked = sorted(fused.items(), key=lambda kv: (-kv[1], info[kv[0]]["path"]))[: args.k]
    hits = []
    for rank, (doc_id, score) in enumerate(ranked, 1):
        h = dict(info[doc_id])
        row = conn.execute(snippet_sql, (h.pop("_match"), doc_id)).fetchone()
        h["snippet"] = " ".join(((row and row[0]) or "").split())
        h.update({
            "rank": rank,
            "score": round(score, 6),
            "queries": sorted(matched[doc_id]),
        })
        hits.append(h)
    conn.close()

    if args.json:
        print(json.dumps(hits, ensure_ascii=False))
        return
    if not hits:
        print("no hits")
        return
    for h in hits:
        print(f"{h['rank']}. [{h['corpus']}/{h['kind']} {h['date']}] {h['title']}")
        print(f"   {h['path']}")
        print(f"   q{','.join(map(str, h['queries']))} | {h['snippet']}")


def cmd_docs(args):
    conn = connect()
    sql = "SELECT path, title, date, corpus, kind, body FROM docs WHERE corpus IN ({})"
    corpora = corpora_for(args.corpus)
    params = list(corpora)
    sql = sql.format(",".join("?" for _ in corpora))
    if args.kind != "any":
        sql += " AND kind = ?"
        params.append(args.kind)
    sql += " ORDER BY path"
    out = sys.stdout
    for path, title, date, corpus, kind, body in conn.execute(sql, params):
        out.write(json.dumps(
            {"path": path, "title": title, "date": date, "corpus": corpus, "kind": kind, "body": body},
            ensure_ascii=False,
        ) + "\n")
    conn.close()


def build_parser():
    p = argparse.ArgumentParser(prog="lexsearch", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("index", help="incrementally (re)index corpora")
    pi.add_argument("--corpus", choices=("all", *CORPORA), default="all")
    pi.set_defaults(func=cmd_index)

    ps = sub.add_parser("search", help="BM25 search with RRF over several queries")
    ps.add_argument("--corpus", choices=("all", *CORPORA), default="all")
    ps.add_argument("-k", type=int, default=10, help="hits to return (default 10)")
    ps.add_argument("--kind", choices=("root", "sub", "any"), default="any")
    ps.add_argument("--json", action="store_true")
    ps.add_argument("query", nargs="+", help="one or more keyword queries, fused with RRF")
    ps.set_defaults(func=cmd_search)

    pd = sub.add_parser("docs", help="dump indexed docs as JSON lines")
    pd.add_argument("--corpus", choices=("all", *CORPORA), required=True)
    pd.add_argument("--kind", choices=("root", "sub", "any"), default="any")
    pd.add_argument("--json", action="store_true", help="accepted for symmetry; output is always JSON lines")
    pd.set_defaults(func=cmd_docs)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    check_code_roots()
    args.func(args)


if __name__ == "__main__":
    main()
