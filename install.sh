#!/usr/bin/env bash
# One-command install for lexsearch: CLI symlink, omp agent, skill and guard, optionally the same
# for Claude Code, config, first index.
# Run it from the clone. Re-running is safe: identical files are left alone, and
# differing files are only replaced with --force. Honours $HOME and LEXSEARCH_CONFIG,
# so it can be tested against a temporary home directory.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./install.sh [--vault PATH] [--code PATH]... [--claude] [--no-guard] [--force]

  --vault PATH   markdown notes folder to index
  --code PATH    git repo to index (repeat for more repos)
  --claude       also install the skill, agent and guard for Claude Code
  --no-guard     skip the guard hook that stops agents grepping the vault
  --force        replace differing files and config values
EOF
}

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
OMP_DIR="$HOME/.omp/agent"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CONFIG="${LEXSEARCH_CONFIG:-$HOME/.config/lexsearch/config.json}"

VAULT=""
CODE=()
FORCE=0
CLAUDE=0
GUARD=1

while [ $# -gt 0 ]; do
  case "$1" in
    --vault) [ $# -ge 2 ] || die "--vault needs a path"; VAULT="$2"; shift 2 ;;
    --vault=*) VAULT="${1#*=}"; shift ;;
    --code) [ $# -ge 2 ] || die "--code needs a path"; CODE+=("$2"); shift 2 ;;
    --code=*) CODE+=("${1#*=}"); shift ;;
    --force) FORCE=1; shift ;;
    --claude) CLAUDE=1; shift ;;
    --no-guard) GUARD=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

command -v python3 >/dev/null 2>&1 || die "python3 not found; install Python 3 first"
python3 -c 'import sqlite3; c = sqlite3.connect(":memory:"); c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")' 2>/dev/null \
  || die "python3 has no SQLite FTS5 support; use a Python build that includes it (for example Homebrew python3)"

# Copy SRC to DST. Identical files are left alone; differing ones need --force.
place_file() {
  local src="$1" dst="$2"
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    say "ok       $dst (unchanged)"
    return
  fi
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    if [ "$FORCE" -eq 0 ]; then
      say "skipped  $dst (exists and differs; rerun with --force to replace)"
      return
    fi
    rm -f "$dst"
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
    say "replaced $dst"
    return
  fi
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
  say "wrote    $dst"
}

# Symlink LINK -> TARGET. A different existing file or link needs --force.
place_link() {
  local target="$1" link="$2"
  mkdir -p "$(dirname "$link")"
  if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]; then
    say "ok       $link -> $target (unchanged)"
  elif [ -e "$link" ] || [ -L "$link" ]; then
    if [ "$FORCE" -eq 0 ]; then
      say "skipped  $link (exists; rerun with --force to replace)"
    else
      rm -f "$link"
      ln -s "$target" "$link"
      say "replaced $link -> $target"
    fi
  else
    ln -s "$target" "$link"
    say "linked   $link -> $target"
  fi
}

# Put TEXT between lexsearch markers in a Markdown file (AGENTS.md, CLAUDE.md). Only the marked
# block is ever changed; the rest of the file is left as it is.
place_block() {
  python3 - "$1" "$2" <<'PY'
import os
import sys

path, text = sys.argv[1], open(sys.argv[2], encoding="utf-8").read().strip()
start, end = "<!-- lexsearch:start -->", "<!-- lexsearch:end -->"
block = f"{start}\n{text}\n{end}"
old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
if start in old and end in old:
    new = old[:old.index(start)] + block + old[old.index(end) + len(end):]
else:
    new = (old.rstrip("\n") + "\n\n" if old.strip() else "") + block + "\n"
if new == old:
    print(f"ok       {path} (unchanged)")
else:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    print(f"{'updated ' if old else 'wrote   '} {path} (lexsearch block)")
PY
}

# 1. CLI: symlinks so `git pull` in the clone updates the installed commands.
place_link "$HERE/lexsearch.py" "$BIN_DIR/lexsearch"
if [ "$GUARD" -eq 1 ]; then
  place_link "$HERE/lexsearch_guard.py" "$BIN_DIR/lexsearch-guard"
fi
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on PATH; add: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# 2. omp agent, skill, a two-line mention in AGENTS.md, and the guard extension.
place_file "$HERE/omp/agents/retriever.md" "$OMP_DIR/agents/retriever.md"
place_file "$HERE/omp/skills/lexsearch/SKILL.md" "$OMP_DIR/skills/lexsearch/SKILL.md"
place_block "$OMP_DIR/AGENTS.md" "$HERE/omp/notes-mention.md"
if [ "$GUARD" -eq 1 ]; then
  place_file "$HERE/omp/extensions/lexsearch-guard.js" "$OMP_DIR/extensions/lexsearch-guard.js"
fi

# 2b. Claude Code: the same skill, the agent, the mention in CLAUDE.md, and the guard as a
# PreToolUse hook in settings.json. Honours CLAUDE_CONFIG_DIR.
if [ "$CLAUDE" -eq 1 ]; then
  place_file "$HERE/claude/agents/retriever.md" "$CLAUDE_DIR/agents/retriever.md"
  place_file "$HERE/omp/skills/lexsearch/SKILL.md" "$CLAUDE_DIR/skills/lexsearch/SKILL.md"
  place_block "$CLAUDE_DIR/CLAUDE.md" "$HERE/omp/notes-mention.md"
  if [ "$GUARD" -eq 1 ]; then
    python3 - "$CLAUDE_DIR/settings.json" <<'PY'
import json
import os
import sys

path = sys.argv[1]
command = '"$HOME/.local/bin/lexsearch-guard"'
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as exc:
        sys.exit(f"error: {path} is not readable JSON ({exc}); fix it, then rerun")
else:
    cfg = {}
entries = cfg.setdefault("hooks", {}).setdefault("PreToolUse", [])
if any("lexsearch-guard" in h.get("command", "") for e in entries for h in e.get("hooks", [])):
    print(f"ok       {path} (guard hook already there)")
else:
    entries.append({"matcher": "Bash|Grep|Glob|Monitor", "hooks": [{"type": "command", "command": command}]})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote    {path} (guard hook)")
PY
  fi
fi

# 3. Glossary: only ever created, never overwritten, because it is personal.
GLOSSARY="$OMP_DIR/glossary.md"
if [ -e "$GLOSSARY" ]; then
  say "kept     $GLOSSARY (exists; never overwritten)"
else
  mkdir -p "$OMP_DIR"
  cp "$HERE/glossary.example.md" "$GLOSSARY"
  say "wrote    $GLOSSARY (copied from glossary.example.md; add your own terms)"
fi

# 4. Config: merge the flags into the existing file. Keys not mentioned stay as they are.
if [ -n "$VAULT" ] && [ ! -d "$VAULT" ]; then
  warn "vault folder does not exist yet: $VAULT"
fi
for dir in ${CODE[@]+"${CODE[@]}"}; do
  [ -d "$dir" ] || warn "code folder does not exist yet: $dir"
done
python3 - "$CONFIG" "$FORCE" "$VAULT" ${CODE[@]+"${CODE[@]}"} <<'PY'
import json
import os
import sys

path, force, vault, codes = sys.argv[1], sys.argv[2] == "1", sys.argv[3], sys.argv[4:]


def norm(p):
    return os.path.abspath(os.path.expanduser(p))


if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as exc:
        sys.exit(f"error: {path} is not readable JSON ({exc}); fix or remove it, then rerun")
    if not isinstance(cfg, dict):
        sys.exit(f"error: {path} must hold a JSON object; fix or remove it, then rerun")
else:
    cfg = {}
before = json.dumps(cfg, sort_keys=True)

cfg.setdefault("vault", None)
cfg.setdefault("sessions", "~/.omp/agent/sessions")
cfg.setdefault("code", [])

if vault:
    new, old = norm(vault), cfg["vault"]
    if old in (None, new) or force:
        cfg["vault"] = new
    else:
        print(f"skipped  config vault stays {old} (rerun with --force to change it to {new})")

if codes:
    wanted = [norm(c) for c in codes]
    if force:
        cfg["code"] = list(dict.fromkeys(wanted))
    else:
        current = list(cfg["code"])
        cfg["code"] = current + [c for c in dict.fromkeys(wanted) if c not in current]

if os.path.exists(path) and json.dumps(cfg, sort_keys=True) == before:
    print(f"ok       {path} (unchanged)")
else:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote    {path}")
PY

# 5. First index. Calls the clone's script directly, so PATH does not matter here.
say ""
say "indexing..."
python3 "$HERE/lexsearch.py" index

say ""
say "Installed. Try: lexsearch search \"<words you remember>\""
