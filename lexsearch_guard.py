#!/usr/bin/env python3
"""
lexsearch-guard: stop agents from scanning the notes vault and point them at lexsearch instead.

Without it, a model told about lexsearch often greps the vault anyway: slower, and several times
the tokens. The installer wires it in as a hook in omp and, with --claude, in Claude Code. Both pass
the tool call as JSON on stdin ({"tool_name", "tool_input", "cwd"}).

Exit 0 lets the call through. Exit 2 blocks it, and the text on stderr goes back to the model.

Blocked: a search that walks the vault (recursive grep, rg, find, a glob over notes, the harness's
own grep and glob tools aimed at a folder). Allowed: lexsearch, reading a note, grep inside named
notes, and anything outside the vault. With no vault configured it allows everything.
"""
import json
import os
import re
import shlex
import sys

MARKER = "lexsearch-guard:"
MESSAGE = (
    f"{MARKER} blocked. This notes vault is indexed by lexsearch, so scanning it with grep, find "
    "or glob is not allowed here. Search it instead:\n"
    '  lexsearch search --corpus vault "keywords" "another phrasing"\n'
    "Load the lexsearch skill for the options. You may still read a note, or grep inside one "
    "note, once a search has returned it."
)

SCAN_TOOLS = {"grep", "glob", "find", "search", "Grep", "Glob"}
SHELL_TOOLS = {"bash", "Bash", "Monitor"}
GREPS = {"grep", "egrep", "fgrep", "zgrep", "rg", "ag", "ack", "ugrep"}
WALKERS = {"find", "fd", "fdfind", "tree"}
# Commands that only scan when handed a wildcard over notes, e.g. `cat */*.md`.
READERS = {"cat", "head", "tail", "awk", "sed", "ls", "wc", "strings", "less", "more", "xargs"}
PREFIXES = {"do", "then", "else", "time", "command", "exec", "nohup", "sudo", "!", "{"}
WRAPPERS = {"timeout", "gtimeout", "nice", "stdbuf", "env"}
WILDCARD = re.compile(r"\*|\?\?|\[[^\]]*\]\*")
QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")


def vault_root():
    raw = os.environ.get("LEXSEARCH_VAULT_ROOT")
    if not raw:
        path = os.environ.get("LEXSEARCH_CONFIG") or os.path.expanduser("~/.config/lexsearch/config.json")
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f).get("vault")
        except (OSError, ValueError, AttributeError):
            raw = None
    return os.path.realpath(os.path.expanduser(raw)) if raw else None


def inside(path, root):
    path = os.path.realpath(path)
    return path == root or path.startswith(root + os.sep)


def is_note(path, cwd):
    """True for a path to one existing file, as opposed to a folder or a pattern."""
    return os.path.isfile(path if os.path.isabs(path) else os.path.join(cwd, path))


def scan_tool_blocked(args, cwd, root):
    """The harness's own grep or glob tool: blocked unless every target is a single note."""
    targets = [args.get(k) for k in ("path", "paths", "file", "glob") if args.get(k)]
    flat = [t for group in targets for t in (group if isinstance(group, list) else [group]) if isinstance(t, str)]
    if not flat:
        return inside(cwd, root)
    for t in flat:
        full = t if os.path.isabs(t) else os.path.join(cwd, t)
        if WILDCARD.search(t) or not os.path.isfile(full):
            if inside(os.path.dirname(full) if WILDCARD.search(t) else full, root) or inside(cwd, root):
                return True
    return False


def segments(command):
    """Split a shell line into simple commands, with a flag for 'reads a pipe'."""
    out, piped = [], False
    for part in re.split(r"(\|\||&&|;|\||\n|\$\(|`|\(|\))", command):
        if part in ("|",):
            piped = True
            continue
        if part in ("||", "&&", ";", "\n", "$(", "`", "(", ")"):
            piped = False
            continue
        if part.strip():
            out.append((part.strip(), piped))
            piped = False
    return out


def words(segment):
    try:
        return shlex.split(segment, comments=False, posix=True)
    except ValueError:
        return segment.split()


def shell_blocked(command, cwd, root):
    """True when a shell line walks the vault instead of reading named notes."""
    if not (inside(cwd, root) or root in command):
        return False
    here = cwd
    for segment, piped in segments(command):
        toks = [t for t in words(segment) if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]
        # Peel off shell keywords and wrappers such as `timeout 100` to reach the real command.
        while toks and (toks[0] in PREFIXES or (toks[0] in WRAPPERS and len(toks) > 1)):
            wrapper, toks = toks[0] in WRAPPERS, toks[1:]
            while wrapper and toks and re.match(r"^(-\S*|\d+(\.\d+)?[smhd]?)$", toks[0]):
                toks = toks[1:]
        if not toks:
            continue
        name, rest = os.path.basename(toks[0]), toks[1:]
        if name == "lexsearch":
            continue
        if name == "cd" and rest:
            here = rest[0] if os.path.isabs(rest[0]) else os.path.join(here, rest[0])
            continue
        if name == "xargs":
            # `... | xargs grep pattern` greps whatever the pipe lists; judge the inner command
            # as a plain grep on named files, which is allowed.
            continue
        in_vault = inside(here, root) or root in segment
        if not in_vault:
            continue
        unquoted = QUOTED.sub(" ", segment)
        paths = [t for t in rest if not t.startswith("-")]
        if name in WALKERS:
            return True
        if name in GREPS:
            flags = [t for t in rest if t.startswith("-")]
            recursive = name in ("rg", "ag", "ack") or any(
                t in ("--recursive", "--dereference-recursive") or (not t.startswith("--") and re.search(r"[rR]", t))
                for t in flags)
            files = [p for p in paths[1:] if is_note(p, here)]
            if WILDCARD.search(" ".join(unquoted.split()[1:])) and not piped and _wild_path(unquoted):
                return True
            if recursive and not files and not piped:
                return True
            if any(os.path.isdir(p if os.path.isabs(p) else os.path.join(here, p)) for p in paths[1:]) and recursive:
                return True
            continue
        if name in READERS and _wild_path(unquoted):
            return True
        if name == "ls" and any(t.startswith("-") and "R" in t and not t.startswith("--") for t in rest):
            return True
    return False


def _wild_path(unquoted):
    """A wildcard used as a path (`*/*.md`, `0*/`, `**`), not one inside a quoted pattern."""
    return bool(re.search(r"(^|\s)[^\s|]*(\*\*|\*/|/\*|\*\.md|\*\s|\*$)", unquoted))


def blocked(tool, args, cwd, root):
    if not root or not isinstance(args, dict):
        return False
    if tool in SCAN_TOOLS:
        return scan_tool_blocked(args, cwd, root)
    if tool in SHELL_TOOLS and isinstance(args.get("command"), str):
        start = args.get("cwd") if isinstance(args.get("cwd"), str) and args.get("cwd") else cwd
        return shell_blocked(args["command"], start, root)
    return False


def main():
    try:
        call = json.load(sys.stdin)
    except ValueError:
        return 0
    cwd = call.get("cwd") or os.getcwd()
    try:
        stop = blocked(call.get("tool_name") or "", call.get("tool_input") or {}, cwd, vault_root())
    except Exception:  # a guard that crashes must not take the agent down with it
        return 0
    if stop:
        print(MESSAGE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
