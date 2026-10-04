#!/usr/bin/env python3
"""Exploded View: turn a codebase or an agent harness into an interactive, drill-down map of how it works.

The AI work is done by the Claude Code session that runs the plugin. This engine does everything else:
it resolves and clones the target, scans it, writes the prompts, merges what the session wrote,
validates it, and renders one self-contained HTML file.

Plugin flow
  prepare <target>          resolve (GitHub URL or local folder), clone if needed, scan, write the digest
                            and the overview prompt into the output folder; prints a short JSON summary
  next <out>                list the drill-ins still to write (and write their prompt files)
  prompt-expand <out> <p>   write the prompt for one drill-in, by node path (e.g. api/auth)
  check-part <out> <p>      validate one written part (`_overview` or a node path) in place
  merge <out>               assemble the parts into map.json, validate, render index.html

Other commands
  static <target>           a map from static analysis only (folders, imports, links, invocations)
  prompt <target>           a self-contained one-pass prompt any Claude session can use to write map.json
  scan <target>             the raw scan as JSON
  render <map.json>         a map file to the HTML viewer
  validate <map.json>       errors (broken references) and advisory readability warnings

Stdlib only. Python 3.9+.
"""
import argparse
import ast
import datetime
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION = "0.2.1"
NAME = "Exploded View"
HOMEPAGE = "https://github.com/tmedia-csb/exploded-view"
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "exploded-view"
MAPS_HOME = Path(os.environ.get("EXPLODED_VIEW_HOME") or Path.home() / "exploded-view")
DEFAULT_DEPTH = 2       # drill-in levels below the top level
DEFAULT_AGENTS = 30     # cap on drill-in subagents per map
SMALL_REPO = 40         # at or under this many files, one pass is enough

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".env", "dist", "build", ".next",
    ".nuxt", ".svelte-kit", "out", "target", ".idea", ".vscode", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "coverage", ".turbo", ".cache", ".parcel-cache", ".gradle", "Pods", "DerivedData",
    ".obsidian", ".trash", ".DS_Store", "vendor", "site-packages", ".netlify", ".vercel", ".terraform",
}
LANG = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".java": "Java",
    ".kt": "Kotlin", ".swift": "Swift", ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".hpp": "C++",
    ".cs": "C#", ".php": "PHP", ".scala": "Scala", ".dart": "Dart", ".lua": "Lua", ".sh": "Shell",
    ".bash": "Shell", ".zsh": "Shell", ".ps1": "PowerShell", ".sql": "SQL", ".html": "HTML", ".css": "CSS",
    ".scss": "CSS", ".vue": "Vue", ".svelte": "Svelte", ".md": "Markdown", ".mdx": "Markdown",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".ini": "INI", ".xml": "XML",
    ".plist": "XML", ".liquid": "Liquid", ".ipynb": "Notebook", ".r": "R", ".jl": "Julia", ".ex": "Elixir",
    ".exs": "Elixir", ".erl": "Erlang", ".clj": "Clojure", ".hs": "Haskell", ".tf": "Terraform",
}
CODE_LANGS = {"Python", "JavaScript", "TypeScript", "Go", "Rust", "Ruby", "Java", "Kotlin", "Swift", "C", "C++",
              "C#", "PHP", "Scala", "Dart", "Lua", "Shell", "PowerShell", "SQL", "Vue", "Svelte", "R", "Julia",
              "Elixir", "Erlang", "Clojure", "Haskell", "Notebook", "Liquid", "HTML", "CSS"}
TEXT_EXTS = set(LANG) | {".txt", ".cfg", ".conf", ".env.example", ".gitignore", ".dockerfile", ".lock"}
# dot-folders that carry real structure (agent config, plugin manifests, CI) and are kept by the non-git walk
KEEP_DOT_DIRS = {".claude", ".claude-plugin", ".github", ".gitlab", ".circleci", ".devcontainer", ".cursor",
                 ".codex", ".agents", ".gemini", ".husky", ".changeset", ".storybook"}
MAX_READ = 400_000
MAX_LEVEL = 12          # target ceiling for parts visible at one level
JS_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"]
KINDS = ["entry", "module", "process", "service", "ui", "data", "config", "external", "person", "agent",
         "skill", "hook", "schedule", "doc", "test", "group"]


# ----------------------------------------------------------------------------------------------
# utilities
# ----------------------------------------------------------------------------------------------

def log(msg):
    print(f"  {msg}", file=sys.stderr, flush=True)


def now_stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def read_text(p):
    try:
        if p.stat().st_size > MAX_READ:
            return None
        data = p.read_bytes()
        if b"\x00" in data[:4096]:
            return None
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


def slug(s):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "node"


def frontmatter(text):
    """Very small YAML-frontmatter reader: returns top-level scalar keys only."""
    if not text or not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out = {}
    key = None
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val in (">", "|", ">-", "|-"):
                out[key] = ""
            else:
                out[key] = val.strip("'\"")
        elif key and line.startswith("  ") and out.get(key) == "":
            out[key] = line.strip()
    return out


SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|github_pat|xox[abpr]|AKIA|AIza|ya29|eyJ)[A-Za-z0-9_\-.]{12,}"),
    re.compile(r"(?i)\b(p|k)=[A-Za-z0-9+/]{40,}={0,2}"),
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|client[_-]?secret)(\s*[:=]\s*)\S{4,}"),
    re.compile(r"(?<![\w/.])[A-Za-z0-9+/_\-]{32,}={0,2}(?![\w/.])"),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
]


EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
EMAIL_OK = re.compile(r"(?i)^(git|noreply|no-reply|example|user|you|name|someone)@|@(example\.(com|org|net)|users\.noreply\.github\.com)$")


def redact(text):
    """Strip things that look like keys, tokens, identifiers, or email addresses before text reaches a map."""
    if not text:
        return text
    text = EMAIL.sub(lambda m: m.group(0) if EMAIL_OK.search(m.group(0)) else "[email]", text)
    for rx in SECRET_PATTERNS:
        def sub(m):
            g = m.group(0)
            if rx.groups >= 2 and m.lastindex and m.lastindex >= 2 and rx.pattern.startswith("(?i)(password"):
                return m.group(1) + m.group(2) + "[redacted]"
            if re.fullmatch(r"[a-z/_\-.]+", g) or g.count("/") > 2:   # long plain words and paths are fine
                return g
            return "[redacted]"
        text = rx.sub(sub, text)
    return text


def first_sentence(text, limit=180):
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    s = m.group(1) if m and len(m.group(1)) > 25 else text
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


# ----------------------------------------------------------------------------------------------
# scan
# ----------------------------------------------------------------------------------------------

def list_files(root):
    root = root.resolve()
    files = None
    try:
        r = subprocess.run(["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
                           capture_output=True, timeout=60)
        if r.returncode == 0 and r.stdout:
            files = [f for f in r.stdout.decode("utf-8", "replace").split("\0") if f]
    except (OSError, subprocess.TimeoutExpired):
        files = None
    if files is None:
        files = []
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d in KEEP_DOT_DIRS or (d not in IGNORE_DIRS and not d.startswith("."))]
            for fn in fns:
                files.append(os.path.relpath(os.path.join(dp, fn), root))
    out = []
    for f in files:
        parts = Path(f).parts
        if any(p in IGNORE_DIRS for p in parts[:-1]):
            continue
        if parts[-1] == ".DS_Store":
            continue
        full = root / f
        if full.is_file() and not full.is_symlink():
            out.append(f.replace(os.sep, "/"))
    return sorted(out)


def describe_file(rel, text):
    """Pull a human summary out of a file: frontmatter summary, module docstring, or header comment."""
    ext = Path(rel).suffix.lower()
    if text is None:
        return ""
    if ext in (".md", ".mdx"):
        fm = frontmatter(text)
        for k in ("summary", "description"):
            if fm.get(k):
                return redact(first_sentence(fm[k]))
        body = re.sub(r"^---.*?\n---", "", text, flags=re.S)
        for para in re.split(r"\n\s*\n", body):
            p = para.strip()
            if p and not p.startswith(("#", "|", "```", "<", "!", "-", "*")):
                return redact(first_sentence(p))
        return ""
    if ext == ".py":
        try:
            doc = ast.get_docstring(ast.parse(text))
            if doc:
                return redact(first_sentence(doc))
        except (SyntaxError, ValueError):
            pass
    return redact(_describe_code(text))


def _describe_code(text):
    lines = text.splitlines()[:30]
    buf = []
    for line in lines:
        s = line.strip()
        if not s and not buf:
            continue
        if s.startswith("#!"):
            continue
        m = re.match(r"^(//+|#+|/\*+|\*+|--|<!--)\s?(.*?)(\*/|-->)?$", s)
        if m and not s.startswith("#include") and not s.startswith("# -*-"):
            t = m.group(2).strip()
            if t and not re.match(r"^(eslint|@ts-|prettier|type:|noqa|pylint|SPDX|Copyright|\(c\))", t):
                buf.append(t)
            elif buf:
                break
        else:
            break
    return first_sentence(" ".join(buf))


PY_IMPORT = re.compile(r"^\s*(?:from\s+(\.*[\w.]*)\s+import\s+([\w*,\s()]+)|import\s+([\w.,\s]+))", re.M)
JS_IMPORT = re.compile(r"""(?:import\s[^'"]*?from\s*|import\s*\(?\s*|require\s*\(\s*|export\s[^'"]*?from\s*)['"]([^'"]+)['"]""")
MD_LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")
MD_IMPORT = re.compile(r"^@(\.{0,2}/?[^\s]+)", re.M)
WIKI = re.compile(r"\[\[([^\]|#]+)")
MENTION = re.compile(r"[\w./~${}-]{1,200}\.(?:py|sh|js|mjs|ts|rb|bash|zsh)\b")
HTML_REF = re.compile(r"""(?:src|href)\s*=\s*['"]([^'"#?]+)""")


def raw_refs(rel, text):
    ext = Path(rel).suffix.lower()
    refs = []
    if text is None:
        return refs
    if ext == ".py":
        try:
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        refs.append(("py", a.name, 0, []))
                elif isinstance(node, ast.ImportFrom):
                    refs.append(("py", node.module or "", node.level, [a.name for a in node.names]))
        except (SyntaxError, ValueError):
            for m in PY_IMPORT.finditer(text):
                if m.group(3):
                    for name in m.group(3).split(","):
                        refs.append(("py", name.strip().split(" ")[0], 0, []))
                else:
                    mod = m.group(1)
                    lvl = len(mod) - len(mod.lstrip("."))
                    refs.append(("py", mod.lstrip("."), lvl, [x.strip() for x in m.group(2).strip("() ").split(",")]))
    elif ext in JS_EXTS:
        for m in JS_IMPORT.finditer(text):
            refs.append(("js", m.group(1), 0, []))
    elif ext in (".md", ".mdx"):
        for m in MD_LINK.finditer(text):
            u = m.group(1)
            if not re.match(r"^[a-z]+:", u):
                refs.append(("path", u, 0, []))
        for m in MD_IMPORT.finditer(text):
            refs.append(("path", m.group(1), 0, []))
        for m in WIKI.finditer(text):
            refs.append(("wiki", m.group(1).strip(), 0, []))
    elif ext in (".html", ".liquid", ".vue", ".svelte"):
        for m in HTML_REF.finditer(text):
            u = m.group(1)
            if not re.match(r"^([a-z]+:|//)", u):
                refs.append(("path", u, 0, []))
    if ext in (".json", ".yaml", ".yml", ".toml", ".sh", ".bash", ".zsh", ".plist", ".xml", ".ini", ".cfg",
               ".py", ".js", ".ts", ".mjs", ".md"):
        # mentions of scripts/files by path: settings.json hooks, launchd plists, package.json scripts, shell
        for m in MENTION.finditer(text):
            refs.append(("mention", m.group(0), 0, []))
    return refs


def build_resolver(files):
    fileset = set(files)
    py_mods = {}
    for f in files:
        if f.endswith(".py"):
            mod = f[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            parts = mod.split(".")
            for i in range(len(parts)):
                py_mods.setdefault(".".join(parts[i:]), f)
    by_base = defaultdict(list)
    by_stem = defaultdict(list)
    by_suffix = defaultdict(list)       # "b/c.py" -> every file ending in "/b/c.py"
    for f in files:
        bits = f.split("/")
        for i in range(1, len(bits)):
            by_suffix["/".join(bits[i:])].append(f)
    for f in files:
        by_base[Path(f).name].append(f)
        by_stem[Path(f).stem.lower()].append(f)

    def norm(p):
        out = []
        for part in p.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if out:
                    out.pop()
                continue
            out.append(part)
        return "/".join(out)

    def resolve(src, ref):
        kind, target, level, names = ref
        here = str(Path(src).parent).replace("\\", "/")
        here = "" if here == "." else here
        if kind == "py":
            cands = []
            if level:
                base = here.split("/") if here else []
                if level > 1:
                    base = base[: len(base) - (level - 1)]
                prefix = ".".join(base)
                mod = ".".join(x for x in (prefix, target) if x)
                cands += [mod + "." + n for n in names] + [mod]
                for c in cands:
                    f = c.replace(".", "/")
                    for cand in (f + ".py", f + "/__init__.py"):
                        if cand in fileset:
                            return cand, "imports"
                return None
            cands = [target + "." + n for n in names if n != "*"] + [target]
            for c in cands:
                if c in py_mods:
                    return py_mods[c], "imports"
            return None
        if kind == "js":
            if not target.startswith((".", "/", "@/", "~/", "src/")):
                return ("pkg:" + ("/".join(target.split("/")[:2]) if target.startswith("@") else target.split("/")[0]), "uses")
            if target.startswith(("@/", "~/")):
                bases = ["src/" + target[2:], target[2:]]
            elif target.startswith("/"):
                bases = [target.lstrip("/")]
            else:
                bases = [norm(here + "/" + target)]
            for b in bases:
                for cand in [b] + [b + e for e in JS_EXTS] + [b + "/index" + e for e in JS_EXTS]:
                    if cand in fileset:
                        return cand, "imports"
                if b.endswith(".js"):
                    for e in (".ts", ".tsx"):
                        if b[:-3] + e in fileset:
                            return b[:-3] + e, "imports"
            return None
        if kind == "path":
            t = target.split("?")[0]
            try:
                from urllib.parse import unquote
                t = unquote(t)
            except Exception:
                pass
            for cand in (norm(here + "/" + t), norm(t.lstrip("/"))):
                if cand in fileset:
                    return cand, "links"
                if cand + ".md" in fileset:
                    return cand + ".md", "links"
                if cand + "/index.html" in fileset:
                    return cand + "/index.html", "links"
            return None
        if kind == "wiki":
            hits = by_stem.get(target.lower()) or by_stem.get(Path(target).stem.lower())
            if hits and len(hits) == 1:
                return hits[0], "links"
            return None
        if kind == "mention":
            t = target.replace("~/", "").replace("$CLAUDE_PROJECT_DIR/", "").replace("${CLAUDE_PROJECT_DIR}/", "")
            t = re.sub(r"^\$\{?\w+\}?/", "", t)
            c = norm(t)
            if c in fileset:
                return c, "invokes"
            suffix = by_suffix.get(c, []) if c else []
            if len(suffix) == 1:
                return suffix[0], "invokes"
            if suffix:
                return None
            hits = by_base.get(Path(t).name)
            if hits and len(hits) == 1:
                return hits[0], "invokes"
            return None
        return None

    return resolve


def detect_harness(root, files, texts, local=False):
    h = {"instructions": [], "skills": [], "agents": [], "commands": [], "hooks": [], "mcp_servers": [],
         "settings": [], "scheduled": [], "manifests": [], "ci": [], "entry_points": []}
    fileset = set(files)
    for f in files:
        name = Path(f).name
        low = f.lower()
        t = texts.get(f)
        if name in ("CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursorrules", "copilot-instructions.md"):
            imports = [m.group(1) for m in MD_IMPORT.finditer(t or "")]
            h["instructions"].append({"file": f, "imports": imports[:60]})
        if name == "SKILL.md":
            fm = frontmatter(t or "")
            h["skills"].append({"file": f, "name": fm.get("name") or Path(f).parent.name,
                                "description": first_sentence(fm.get("description", ""), 240)})
        if "/.claude/agents/" in "/" + f or low.startswith(".claude/agents/"):
            if f.endswith(".md"):
                fm = frontmatter(t or "")
                h["agents"].append({"file": f, "name": fm.get("name") or Path(f).stem,
                                    "description": first_sentence(fm.get("description", ""), 200)})
        if low.startswith(".claude/commands/") and f.endswith(".md"):
            h["commands"].append({"file": f, "name": Path(f).stem})
        if re.search(r"(^|/)\.claude/settings(\.local)?\.json$", f) and t:
            h["settings"].append(f)
            try:
                cfg = json.loads(t)
                for event, groups in (cfg.get("hooks") or {}).items():
                    for grp in groups or []:
                        for hk in grp.get("hooks", []) or []:
                            h["hooks"].append({"event": event, "matcher": grp.get("matcher", ""),
                                               "command": str(hk.get("command", ""))[:200], "file": f})
                for nm in (cfg.get("mcpServers") or {}):
                    h["mcp_servers"].append({"name": nm, "file": f})
            except (ValueError, AttributeError):
                pass
        if name == ".mcp.json" and t:
            try:
                for nm, spec in (json.loads(t).get("mcpServers") or {}).items():
                    h["mcp_servers"].append({"name": nm, "file": f, "command": str(spec.get("command", ""))[:120]})
            except (ValueError, AttributeError):
                pass
        if f.endswith(".plist") and t and "ProgramArguments" in t:
            h["scheduled"].append({"file": f})
        if low.startswith(".github/workflows/"):
            h["ci"].append(f)
        if name in ("package.json", "pyproject.toml", "Cargo.toml", "go.mod", "Gemfile", "requirements.txt",
                    "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "Makefile", "netlify.toml",
                    "vercel.json", "Procfile", "setup.py", "Package.swift", "pubspec.yaml", "serverless.yml"):
            entry = {"file": f}
            if name == "package.json" and t:
                try:
                    pj = json.loads(t)
                    entry.update({"name": pj.get("name"), "scripts": pj.get("scripts", {}),
                                  "main": pj.get("main"), "bin": pj.get("bin"),
                                  "dependencies": sorted((pj.get("dependencies") or {}).keys())[:40]})
                except ValueError:
                    pass
            if name == "Makefile" and t:
                entry["targets"] = re.findall(r"^([a-zA-Z0-9_-]+):", t, re.M)[:30]
            h["manifests"].append(entry)
    for f in files:
        base = Path(f).name
        stem = Path(f).stem
        depth = f.count("/")
        if base in ("__main__.py", "main.py", "app.py", "manage.py", "cli.py", "server.py", "wsgi.py",
                    "main.go", "main.rs", "index.js", "index.ts", "server.js", "server.ts", "app.js", "app.ts",
                    "main.ts", "main.js", "main.swift", "App.swift", "main.dart") and depth <= 3:
            h["entry_points"].append(f)
        elif f.startswith(("bin/", "scripts/", "cmd/")) and depth <= 2:
            h["entry_points"].append(f)
        elif f.endswith(".py") and depth == 0 and texts.get(f) and "__main__" in texts[f]:
            h["entry_points"].append(f)
        elif base == "index.html" and depth <= 2:
            h["entry_points"].append(f)
        _ = stem
    h["entry_points"] = sorted(set(h["entry_points"]))[:40]
    # Config outside the repo belongs to whoever runs the scan, so it is only meaningful when the target is
    # this machine's own folder. A cloned repo never sees it (otherwise the runner's hooks leak into the map).
    if local and (h["instructions"] or any(f.startswith(".claude/") for f in files)):
        outside = outside_harness(root, fileset)
        if outside:
            h["outside_repo"] = outside
    return {k: v for k, v in h.items() if v}


USER_LEVEL = "this machine's user-level config"


def outside_harness(root, fileset):
    """Agent harness config that lives outside git: user-level hooks, untracked MCP config, launchd jobs.

    Local targets only. Every entry is labelled as this machine's config, not the repo's."""
    out = []
    user = Path.home() / ".claude" / "settings.json"
    try:
        cfg = json.loads(user.read_text())
        for event, groups in (cfg.get("hooks") or {}).items():
            for grp in groups or []:
                for hk in grp.get("hooks", []) or []:
                    out.append({"where": "~/.claude/settings.json", "scope": USER_LEVEL, "hook": event,
                                "command": redact(str(hk.get("command", "")))[:160]})
        for nm in (cfg.get("mcpServers") or {}):
            out.append({"where": "~/.claude/settings.json", "scope": USER_LEVEL, "mcp_server": nm})
    except (OSError, ValueError, AttributeError):
        pass
    mcp = Path(root) / ".mcp.json"
    if ".mcp.json" not in fileset and mcp.exists():
        try:
            for nm in (json.loads(mcp.read_text()).get("mcpServers") or {}):
                out.append({"where": ".mcp.json (untracked)", "scope": "this folder, not in git", "mcp_server": nm})
        except (OSError, ValueError, AttributeError):
            pass
    agents = Path.home() / "Library" / "LaunchAgents"
    if agents.is_dir():
        for pl in sorted(agents.glob("*.plist"))[:200]:
            try:
                if str(root) in pl.read_text(errors="replace"):
                    out.append({"where": "~/Library/LaunchAgents", "scope": USER_LEVEL, "launchd_job": pl.stem})
            except OSError:
                pass
    return out[:40]


def guess_kind(rel, text=None):
    p = rel.lower()
    name = Path(p).name
    ext = Path(p).suffix
    if name == "skill.md":
        return "skill"
    if "/hooks/" in "/" + p or name.startswith("hook"):
        return "hook"
    if re.search(r"(^|/)(tests?|__tests__|spec)(/|$)", p) or re.search(r"(\.test\.|\.spec\.|^test_|_test\.)", name):
        return "test"
    if name.endswith(".plist") or "cron" in name or "schedule" in name:
        return "schedule"
    if name in ("claude.md", "agents.md") or "/agents/" in "/" + p or "prompt" in name:
        return "agent"
    if ext in (".md", ".mdx", ".txt", ".rst"):
        return "doc"
    if ext in (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".lock") or name.startswith(".") \
            or name in ("dockerfile", "makefile", "procfile"):
        return "config"
    if ext in (".sql", ".csv", ".db", ".sqlite", ".parquet") or re.search(r"(models?|schema|migrations?|db|data|store)", Path(p).stem):
        return "data"
    if ext in (".html", ".css", ".scss", ".vue", ".svelte", ".tsx", ".jsx", ".liquid") or "component" in p:
        return "ui"
    if rel in ENTRY_HINTS:
        return "entry"
    if ext in (".sh", ".bash", ".zsh", ".ps1"):
        return "process"
    if re.search(r"(api|server|service|routes?|handlers?|controllers?|functions?)", p):
        return "service"
    return "module"


ENTRY_HINTS = set()


def guess_dir_kind(path, kinds):
    p = path.lower()
    name = p.rsplit("/", 1)[-1]
    rules = [
        (r"^(tests?|__tests__|spec|e2e)$", "test"), (r"^(docs?|documentation|wiki|notes)$", "doc"),
        (r"skills?$", "skill"), (r"^hooks?$", "hook"), (r"^(agents?|prompts?)$", "agent"),
        (r"^(config|configs|settings|\.github|\.claude|deploy|infra|ops)$", "config"),
        (r"^(models?|db|data|database|migrations?|schemas?|store|storage)$", "data"),
        (r"^(ui|components?|pages?|views?|web|frontend|public|static|assets|styles?|site|app)$", "ui"),
        (r"^(api|server|services?|routes?|handlers?|controllers?|functions?|backend)$", "service"),
        (r"^(scripts?|bin|cmd|tools?|jobs?|tasks?|workers?)$", "process"),
    ]
    for rx, k in rules:
        if re.search(rx, name):
            return k
    if kinds:
        top, n = kinds.most_common(1)[0]
        if n / max(1, sum(kinds.values())) >= 0.6 and top not in ("config",):
            return top if top != "module" else "group"
    return "group"


def scan(root, quiet=False, local=True):
    root = Path(root).expanduser().resolve()
    if not quiet:
        log(f"scanning {root}")
    files = list_files(root)
    texts = {}
    meta = {}
    for f in files:
        p = root / f
        ext = Path(f).suffix.lower()
        try:
            size = p.stat().st_size
        except OSError:
            continue
        t = read_text(p) if (ext in TEXT_EXTS or not ext or Path(f).name in ("Dockerfile", "Makefile")) else None
        if t is not None:
            texts[f] = t
        meta[f] = {"size": size, "lines": t.count("\n") + 1 if t else 0, "lang": LANG.get(ext, "")}
    files = [f for f in files if f in meta]
    harness = detect_harness(root, files, texts, local=local)
    ENTRY_HINTS.clear()
    ENTRY_HINTS.update(harness.get("entry_points", []))
    resolve = build_resolver(files)
    edges = Counter()
    packages = Counter()
    for f in files:
        for ref in raw_refs(f, texts.get(f)):
            r = resolve(f, ref)
            if not r:
                continue
            tgt, kind = r
            if kind == "invokes" and f.endswith((".md", ".mdx")):
                kind = "mentions"
            if tgt.startswith("pkg:"):
                packages[tgt[4:]] += 1
                continue
            if tgt != f:
                edges[(f, tgt, kind)] += 1
    langs = Counter()
    for f, m in meta.items():
        if m["lang"]:
            langs[m["lang"]] += m["lines"] or 1
    file_rows = []
    for f in files:
        m = meta[f]
        file_rows.append({"path": f, "lang": m["lang"], "lines": m["lines"], "size": m["size"],
                          "kind": guess_kind(f, texts.get(f)), "summary": describe_file(f, texts.get(f))})
    readme = ""
    for cand in ("README.md", "readme.md", "README", "README.rst"):
        if cand in texts:
            readme = redact(texts[cand][:4000])
            break
    out = {
        "explodedview_scan": VERSION, "root": str(root), "name": root.name, "scanned": now_stamp(),
        "file_count": len(files), "languages": dict(langs.most_common()),
        "packages": dict(packages.most_common(40)), "harness": harness, "readme": readme,
        "files": file_rows,
        "edges": [{"from": a, "to": b, "kind": k, "count": c} for (a, b, k), c in edges.most_common()],
    }
    if not quiet:
        log(f"{len(files)} files · {len(out['edges'])} references · languages: "
            + ", ".join(list(out["languages"])[:6]))
    return out


# ----------------------------------------------------------------------------------------------
# static map
# ----------------------------------------------------------------------------------------------

class Dir:
    def __init__(self, path):
        self.path = path
        self.dirs = {}
        self.files = []

    def count(self):
        return len(self.files) + sum(d.count() for d in self.dirs.values())

    def all_files(self):
        out = list(self.files)
        for d in self.dirs.values():
            out += d.all_files()
        return out


def build_tree(files):
    root = Dir("")
    for f in files:
        parts = f.split("/")
        d = root
        for i, part in enumerate(parts[:-1]):
            key = part
            if key not in d.dirs:
                d.dirs[key] = Dir("/".join(parts[: i + 1]))
            d = d.dirs[key]
        d.files.append(f)
    return root


def chunk_label(items, key):
    a, b = key(items[0]), key(items[-1])
    return a if a == b else f"{a} – {b}"


def static_map(sc, max_depth=4):
    files_info = {f["path"]: f for f in sc["files"]}
    tree = build_tree(list(files_info))
    edges = sc["edges"]
    used_ids = set()

    def uid(base):
        b = slug(base)[:40] or "node"
        i, cand = 2, b
        while cand in used_ids:
            cand, i = f"{b}-{i}", i + 1
        used_ids.add(cand)
        return cand

    def file_node(f):
        info = files_info[f]
        bits = [x for x in (info["lang"], f"{info['lines']} lines" if info["lines"] else "") if x]
        return {"id": uid(Path(f).name), "label": Path(f).name, "kind": info["kind"],
                "summary": info["summary"] or " · ".join(bits), "files": [f], "_files": [f]}

    def dir_summary(d, files):
        readme = next((f for f in d.files if Path(f).name.lower() in ("readme.md", "index.md")), None)
        if readme and files_info[readme]["summary"]:
            return files_info[readme]["summary"]
        langs = Counter(files_info[f]["lang"] for f in files if files_info[f]["lang"])
        top = ", ".join(l for l, _ in langs.most_common(3))
        return f"{len(files)} file{'s' if len(files) != 1 else ''}" + (f" · {top}" if top else "")

    def group_node(label, files, children_builder, kind="group", summary=None):
        return {"id": uid(label), "label": label, "kind": kind,
                "summary": summary or f"{len(files)} files", "_files": files, "_build": children_builder}

    def collapse(d):
        label = Path(d.path).name
        while not d.files and len(d.dirs) == 1:
            d = next(iter(d.dirs.values()))
            label += "/" + Path(d.path).name
        return d, label

    def chunk_files(files, depth):
        """Split a long flat file list into readable groups (by date prefix or alphabet)."""
        files = sorted(files, key=lambda f: Path(f).name.lower())
        dated = [f for f in files if re.match(r"^\d{6}", Path(f).name)]
        if len(dated) > len(files) * 0.6:
            buckets = defaultdict(list)
            for f in files:
                m = re.match(r"^(\d{4})", Path(f).name)
                buckets[m.group(1) if m else "undated"].append(f)
            keys = sorted(buckets)
            if 1 < len(keys) <= MAX_LEVEL:
                out = []
                for k in keys:
                    lab = f"20{k[:2]}-{k[2:]}" if k != "undated" else "Undated"
                    out.append(group_node(lab, buckets[k], lambda fs=buckets[k], dd=depth: files_level(fs, dd + 1),
                                          kind="doc" if all(f.endswith(".md") for f in buckets[k]) else "group",
                                          summary=f"{len(buckets[k])} files dated {lab}"))
                return out
        size = -(-len(files) // MAX_LEVEL)
        size = max(size, 8)
        out = []
        for i in range(0, len(files), size):
            part = files[i: i + size]
            lab = chunk_label(part, lambda f: Path(f).name[:3].upper())
            out.append(group_node(lab, part, lambda fs=part, dd=depth: files_level(fs, dd + 1),
                                  summary=f"{len(part)} files, {Path(part[0]).name} to {Path(part[-1]).name}"))
        return out

    def chunk_nodes(nodes_):
        if len(nodes_) <= MAX_LEVEL:
            return nodes_
        size = -(-len(nodes_) // MAX_LEVEL)
        out = []
        for i in range(0, len(nodes_), size):
            part = nodes_[i: i + size]
            out.append({"id": uid("folders-" + part[0]["label"]), "label": f"{part[0]['label']} … {part[-1]['label']}",
                        "kind": "group", "summary": f"{len(part)} folders", "_files": [f for n in part for f in n["_files"]],
                        "_build": lambda p=part: p})
        return out

    def files_level(files, depth):
        if len(files) <= MAX_LEVEL + 2:
            return [file_node(f) for f in files]
        return chunk_files(files, depth)

    def dir_level(d, depth):
        nodes = []
        subdirs = sorted(d.dirs.values(), key=lambda x: -x.count())
        loose = sorted(d.files)
        dir_nodes = []
        for sd in subdirs:
            sd2, label = collapse(sd)
            fl = sd2.all_files()
            kinds = Counter(files_info[f]["kind"] for f in fl)
            n = {"id": uid(label), "label": label + "/", "kind": guess_dir_kind(sd2.path, kinds),
                 "summary": dir_summary(sd2, fl), "_files": fl, "files": [sd2.path + "/"]}
            if len(fl) > 1 and depth < max_depth:
                n["_build"] = lambda x=sd2, dd=depth: dir_level(x, dd + 1)
            elif len(fl) > 1:
                n["_build"] = lambda fs=fl, dd=depth: files_level(fs, dd + 1)
            dir_nodes.append(n)
        budget = MAX_LEVEL
        if len(dir_nodes) > budget - (1 if loose else 0):
            keep = dir_nodes[: budget - 2]
            rest = dir_nodes[budget - 2:]
            rest_files = [f for n in rest for f in n["_files"]]
            keep.append({"id": uid("other-folders"), "label": f"{len(rest)} more folders", "kind": "group",
                         "summary": ", ".join(n["label"] for n in rest[:6]) + ("…" if len(rest) > 6 else ""),
                         "_files": rest_files, "_build": lambda r=rest: chunk_nodes(r)})
            dir_nodes = keep
        nodes += dir_nodes
        room = MAX_LEVEL - len(nodes)
        if loose:
            cfg = [f for f in loose if files_info[f]["kind"] == "config"]
            if len(cfg) >= 2 and len(loose) > 3:
                nodes.append(group_node(f"Config & tooling ({len(cfg)})", cfg,
                                        lambda fs=cfg, dd=depth: files_level(fs, dd + 1), kind="config",
                                        summary=", ".join(Path(f).name for f in cfg[:5]) + ("…" if len(cfg) > 5 else "")))
                loose = [f for f in loose if f not in cfg]
                room -= 1
        if loose:
            if len(loose) <= max(room, 4) and len(loose) <= 8:
                nodes += [file_node(f) for f in loose]
            else:
                by_kind = defaultdict(list)
                for f in loose:
                    by_kind[files_info[f]["kind"]].append(f)
                names = {"doc": "Documents", "config": "Config files", "module": "Code files", "ui": "Interface files",
                         "data": "Data files", "test": "Tests", "process": "Scripts", "service": "Service code",
                         "entry": "Entry points", "skill": "Skills", "hook": "Hooks", "agent": "Agent files",
                         "schedule": "Schedules"}
                if len(by_kind) == 1 or len(by_kind) > room:
                    lab = (names.get(next(iter(by_kind)), "Files") if len(by_kind) == 1 else "Files here")
                    nodes.append(group_node(f"{lab} ({len(loose)})", loose,
                                            lambda fs=loose, dd=depth: files_level(fs, dd + 1),
                                            kind=next(iter(by_kind)) if len(by_kind) == 1 else "group"))
                else:
                    for k, fl in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
                        if len(fl) == 1:
                            nodes.append(file_node(fl[0]))
                        else:
                            nodes.append(group_node(f"{names.get(k, k.title())} ({len(fl)})", fl,
                                                    lambda fs=fl, dd=depth: files_level(fs, dd + 1), kind=k))
        return nodes

    def connect(nodes, outside=None):
        """Aggregate file-level references into edges between the parts visible at this level."""
        owner = {}
        for n in nodes:
            for f in n["_files"]:
                owner[f] = n["id"]
        agg = Counter()
        kinds = defaultdict(Counter)
        ctx = Counter()
        ctx_kinds = defaultdict(Counter)
        for e in edges:
            a, b = owner.get(e["from"]), owner.get(e["to"])
            if a and b and a != b:
                agg[(a, b)] += e["count"]
                kinds[(a, b)][e["kind"]] += e["count"]
            elif outside:
                if a and not b and e["to"] in outside:
                    key = (a, "../" + outside[e["to"]])
                    ctx[key] += e["count"]
                    ctx_kinds[key][e["kind"]] += e["count"]
                elif b and not a and e["from"] in outside:
                    key = ("../" + outside[e["from"]], b)
                    ctx[key] += e["count"]
                    ctx_kinds[key][e["kind"]] += e["count"]
        out = []
        for (a, b), c in agg.most_common(28):
            k = kinds[(a, b)].most_common(1)[0][0]
            out.append({"from": a, "to": b, "label": f"{k} ×{c}" if c > 1 else k, "kind": "data" if k in ("links", "mentions") else ""})
        for (a, b), c in ctx.most_common(6):
            k = ctx_kinds[(a, b)].most_common(1)[0][0]
            out.append({"from": a, "to": b, "label": f"{k} ×{c}" if c > 1 else k, "kind": "data" if k in ("links", "mentions") else ""})
        return out

    def finalize(nodes, depth, outside):
        graph = {"nodes": [], "edges": connect(nodes, outside)}
        for n in nodes:
            build = n.pop("_build", None)
            files = n.pop("_files")
            n["_owned"] = files
            if build and depth < max_depth + 2:
                kids = build()
                if kids and not (len(kids) == 1 and kids[0].get("_files") == files):
                    owner_here = {}
                    for sib in nodes:
                        if sib is n:
                            continue
                        for f in sib.get("_files") or sib.get("files") or []:
                            owner_here[f] = sib["id"]
                    n["children"] = finalize(kids, depth + 1, owner_here)
            if "files" not in n:
                n["files"] = files[:12]
            graph["nodes"].append(n)
        return graph

    top = dir_level(tree, 0)
    root_graph = finalize(top, 0, None)

    # A starting workflow: walk references outward from the first entry point, at the top level.
    flows = []
    top_owner = {}
    for n in root_graph["nodes"]:
        for f in collect_files(n):
            top_owner[f] = n["id"]
    adj = defaultdict(list)
    for e in edges:
        adj[e["from"]].append(e["to"])
    for ep in sc.get("harness", {}).get("entry_points", [])[:3]:
        seen_nodes, steps, seen_files, queue = [], [], {ep}, [ep]
        start = top_owner.get(ep)
        if not start:
            continue
        seen_nodes.append(start)
        while queue and len(steps) < 7:
            f = queue.pop(0)
            for t in adj.get(f, []):
                if t in seen_files:
                    continue
                seen_files.add(t)
                queue.append(t)
                a, b = top_owner.get(f), top_owner.get(t)
                if a and b and a != b and b not in seen_nodes:
                    seen_nodes.append(b)
                    steps.append({"from": a, "to": b, "text": f"{Path(f).name} reaches into {label_of(root_graph, b)}"})
        if steps:
            steps.insert(0, {"node": start, "text": f"Execution starts at {ep}"})
            flows.append({"id": slug("from-" + ep), "name": f"Starting from {Path(ep).name}",
                          "summary": "Follows references outward from this entry point, one part at a time.",
                          "steps": steps})
    if flows:
        root_graph["flows"] = flows
    strip_private(root_graph)
    title = sc["name"]
    langs = list(sc["languages"])[:3]
    return {
        "title": title,
        "summary": first_sentence(re.sub(r"^#.*$", "", sc.get("readme", ""), flags=re.M), 240)
        or f"{sc['file_count']} files" + (f", mostly {', '.join(langs)}" if langs else "") + ". Mapped from folder structure and references.",
        "source": sc["root"], "generated": now_stamp(), "generator": "static", "root": root_graph,
    }


def collect_files(n):
    return list(n.get("_owned") or n.get("files") or [])


def strip_private(graph):
    for n in graph.get("nodes", []):
        n.pop("_owned", None)
        if n.get("children"):
            strip_private(n["children"])


def label_of(graph, nid):
    for n in graph["nodes"]:
        if n["id"] == nid:
            return n["label"]
    return nid


# ----------------------------------------------------------------------------------------------
# prompts + AI synthesis
# ----------------------------------------------------------------------------------------------

SCHEMA_DOC = """
map.json schema (JSON only, no comments):
{
  "title": "Short name of the system",
  "summary": "Two sentences: what this system is for and how it works, in plain English.",
  "root": Graph
}
Graph = {
  "direction": "RIGHT" | "DOWN"           (optional; RIGHT reads best for pipelines),
  "nodes": [Node, ...],                    (5–10 per graph; never more than 12)
  "edges": [Edge, ...],
  "flows": [Flow, ...]                     (top level: 2–5 key workflows; deeper levels: 0–3)
}
Node = {
  "id": "kebab-case, unique within its graph",
  "label": "2–4 word human name (not a filename unless the file IS the concept)",
  "kind": one of entry | module | process | service | ui | data | config | external | person | agent | skill | hook | schedule | doc | test | group,
  "summary": "One plain sentence, under 22 words: the job this part does.",
  "details": "Optional. 2–6 short sentences or '- ' bullets: responsibilities, inputs, outputs, gotchas.",
  "files": ["repo/relative/paths", "folders/end-with-slash/"],   (the 1–8 files that matter most; always relative to the repo root)
  "scope": ["paths or globs this part owns"],                      (two-pass mode only: tells the engine what a separate drill-in covers. Never in one-pass mode)
  "status": "live" | "planned" | "deprecated"                      (optional; default live. planned = designed or documented but not wired up yet)
  "children": Graph                                                (optional: the part's internals, same rules)
}
"files" may include paths outside the repo (e.g. "~/.claude/settings.json") only when the digest lists them as config on this machine. Never write absolute paths such as /Users/... or /home/....
Edge = {
  "from": "node id", "to": "node id",      (inside a child graph, point outward with "../<id>" for the level above, "../../<id>" for two levels up, and so on)
  "label": "short verb phrase: 'sends orders', 'reads config', 'triggers'",
  "kind": "" | "data" | "async"            (data = data flow/read/write; async = event, schedule, or fire-and-forget)
}
Flow = {
  "id": "kebab-case", "name": "What happens when …", "summary": "One sentence.",
  "steps": [ {"from": "id", "to": "id", "text": "What happens on this hop"} | {"node": "id", "text": "What happens here"} ]
}
""".strip()

RULES = """
How to model it (these rules matter more than completeness):
- Model how the system WORKS at runtime, not how the folders are arranged. A node is a responsibility a person would name ("Order intake", "Session bootloader"), not a directory, unless the directory is that responsibility.
- Keep every level readable: 5–10 nodes, 12 max, and that budget INCLUDES people and outside systems. If more parts exist, group them and push detail into children. An outside service that touches the code in one place can fold into the node that uses it, or several can share one "Outside services" node.
- Include the outside world: people/actors (kind person), third-party APIs, databases, LLMs, MCP servers, schedulers (kind external/agent/schedule). Systems rarely make sense without their inputs and outputs.
- Edges carry meaning. Label each with what moves or happens ("writes invoices", "fires on session start"), in 2–5 words. Skip edges that are mere plumbing; DO draw an import edge when it explains behaviour or a guarantee (e.g. "re-scores with the same engine"). Every node should usually have at least one edge.
- Code used from two places (say, by the browser and the server) gets one home node; draw an edge from the other user saying why it is shared.
- People are first-class: end users, and for systems one person runs, that operator (kind person).
- Flows are the story. At the top level, write 2–5 flows for the things a newcomer most needs to understand (the main request path, the build/deploy path, the scheduled job, the agent's session loop). Each step is one hop with a one-sentence explanation. Flows only reference nodes in the same graph (or "../id" for parent siblings).
- Give children to any node that hides real structure (several files or several distinct steps). Leaf nodes are fine; not everything needs a drill-in.
- For agent harnesses (Claude Code, Cursor, custom agents): treat instruction files, memory, skills/workflows, hooks, MCP servers, scheduled jobs, and the human operator as first-class parts, and show the session lifecycle as a flow.
- Mark what isn't real yet. A part that is designed, documented, or stubbed but not wired into the running system gets "status": "planned"; a part kept only for compatibility gets "deprecated". Don't silently present plans as working code.
- The validator reports errors (broken references, unknown kinds) and readability warnings (crowded levels, long labels, unconnected parts). Fix every error. Warnings are advice, not failures: an unconnected leaf is fine when its real connections live elsewhere; reach them with "../../id" when you can.
- Harness config often lives outside the repo (user-level settings and hooks, MCP config, launchd jobs). When the target is a folder on this machine, the digest lists what was found under "outside_repo", labelled as this machine's user-level config; include it when the system depends on it and say in details that it is machine config, not part of the repo. When the target is a cloned repo, nothing outside the repo is visible: don't guess at it.
- The digest can contain private material from file names and first lines. Treat it as context only.
- Be accurate. Read the files you describe. Do not invent components; if something is unclear, say so in details.
- Summaries are plain English for a smart non-specialist. No marketing words.
- Describe mechanisms, not private contents. Never copy secrets, credentials, API keys, passwords, account numbers, infrastructure identifiers (site/project IDs, DNS records, verification tokens, keys), or personal details into the map. Say that a credential exists and where it is configured, never its value.
- Omit keys you have nothing for (no empty "flows": [], no "children" with no nodes).
""".strip()


def scan_digest(sc, max_files=260, scope=None):
    """A compact, prompt-sized view of the scan."""
    files = sc["files"]
    if scope:
        files = [f for f in files if in_scope(f["path"], scope)]
    lines = [f"Repository: {sc.get('display_root') or scrub_paths(sc['root'])}", f"Files: {len(files)} (of {sc['file_count']} total)"]
    if not scope:
        lines.append("Languages (by lines): " + ", ".join(f"{k} {v}" for k, v in list(sc["languages"].items())[:10]))
        if sc.get("packages"):
            lines.append("External packages used: " + ", ".join(list(sc["packages"])[:30]))
        h = sc.get("harness") or {}
        if h:
            lines.append("\nDetected structure:")
            for k, v in h.items():
                lines.append(f"- {k}: " + redact(json.dumps(v, ensure_ascii=False))[:1600])
        if sc.get("readme"):
            lines.append("\nREADME (excerpt):\n" + sc["readme"][:2500])
    dirs = Counter()
    for f in files:
        parts = f["path"].split("/")
        for i in range(1, min(len(parts), 4)):
            dirs["/".join(parts[:i]) + "/"] += 1
    lines.append("\nFolders (file counts):")
    shown = [(d, c) for d, c in sorted(dirs.items()) if c >= 2 or d.count("/") <= 1]
    if len(shown) > 120:   # big repos: keep the shallow levels and the biggest deep folders
        keep = {d for d, c in shown if d.count("/") <= 1}
        keep |= {d for d, c in sorted(shown, key=lambda x: -x[1])[: max(0, 120 - len(keep))]}
        lines += [f"  {d} {c}" for d, c in shown if d in keep]
        lines.append(f"  … {len(shown) - len(keep)} smaller folders not listed")
    else:
        lines += [f"  {d} {c}" for d, c in shown]
    lock = re.compile(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock|Gemfile\.lock|composer\.lock|uv\.lock|bun\.lockb?|Pipfile\.lock|go\.sum|flake\.lock|.*\.min\.(js|css))$")
    by_dir = defaultdict(list)
    for f in files:
        by_dir[str(Path(f["path"]).parent)].append(f)
    folded, keep = [], []
    for d, fs in by_dir.items():
        content = [f for f in fs if f["kind"] in ("doc", "data") or f["lang"] in ("Markdown", "JSON", "YAML", "")]
        if len(content) > 8 and len(content) >= 0.7 * len(fs):
            folded.append(f"  {d}/ · {len(fs)} files, mostly {Counter(f['lang'] or f['kind'] for f in content).most_common(1)[0][0]} content")
            keep += [f for f in fs if f not in content]
        else:
            keep += fs
    noise = re.compile(r"(^|/)(run-artifacts|artifacts|fixtures|snapshots|__snapshots__|testdata|tmp)(/|$)")
    keep = [f for f in keep if not lock.search(f["path"]) and not noise.search(f["path"])]
    ranked = sorted(keep, key=lambda f: (f["kind"] in ("doc",) and f["lines"] < 400, -min(f["lines"], 3000)))
    if folded:
        lines.append("\nContent folders (summarised, not listed file by file):")
        lines += folded[:40]
    lines.append(f"\nNotable files (path · lines · first-line summary), up to {max_files}:")
    budget, listed = 26_000, 0      # characters: keeps the digest the same size on a 20K-file repo
    for f in ranked[:max_files]:
        s = f" · {f['summary']}" if f["summary"] else ""
        row = f"  {f['path']} · {f['lines']}{s[:160]}"
        if budget - len(row) < 0:
            break
        budget -= len(row) + 1
        lines.append(row)
        listed += 1
    if len(ranked) > listed:
        lines.append(f"  … {len(ranked) - listed} more files not listed (use Glob to see them)")
    paths = {f["path"] for f in files}
    noisy = re.compile(r"(^|/)(run-artifacts|artifacts|fixtures|snapshots|__snapshots__|testdata|tmp)(/|$)")
    es = [e for e in sc["edges"] if (e["from"] in paths or e["to"] in paths)
          and not noisy.search(e["from"]) and not noisy.search(e["to"])][:150 if scope else 80]
    if es:
        lines.append("\nResolved references (from → to · kind × count), strongest first:")
        budget = 9_000
        for e in es:
            row = f"  {e['from']} → {e['to']} · {e['kind']} ×{e['count']}"
            if budget - len(row) < 0:
                lines.append("  … more references not listed")
                break
            budget -= len(row) + 1
            lines.append(row)
    return scrub_paths("\n".join(lines))


def in_scope(path, scope):
    for s in scope:
        s = s.strip()
        if not s:
            continue
        if s.endswith("/") and path.startswith(s):
            return True
        if path == s or fnmatch.fnmatch(path, s) or path.startswith(s.rstrip("/") + "/"):
            return True
    return False


# ----------------------------------------------------------------------------------------------
# privacy: paths
# ----------------------------------------------------------------------------------------------

HOME_RX = re.compile(r"(?<![\w.-])(?:/Users|/home)/[^/\s\"'`)\]]+")
WIN_HOME_RX = re.compile(r"\b[A-Za-z]:\\\\?Users\\\\?[^\\\s\"']+", re.I)


def scrub_paths(text, roots=()):
    """Remove the runner's absolute paths: scanned roots become repo-relative, home folders become ~."""
    if not isinstance(text, str) or not text:
        return text
    for r in roots:
        r = str(r or "").rstrip("/")
        if len(r) > 1:
            text = text.replace(r + "/", "").replace(r, ".")
    text = text.replace(str(Path.home()), "~")
    text = HOME_RX.sub("~", text)
    return WIN_HOME_RX.sub("~", text)


# ----------------------------------------------------------------------------------------------
# targets: GitHub URLs and local folders
# ----------------------------------------------------------------------------------------------

class TargetError(Exception):
    pass


def git(args, cwd=None, timeout=300, check=True):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    try:
        r = subprocess.run(["git"] + args, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except FileNotFoundError:
        raise TargetError("git is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise TargetError(f"git {args[0]} timed out after {timeout}s")
    if check and r.returncode:
        raise TargetError((r.stderr or r.stdout).strip()[-600:] or f"git {args[0]} failed")
    return r.stdout.strip() if r.returncode == 0 else ""


def strip_userinfo(url):
    return re.sub(r"^(\w+://)[^/@]+@", r"\1", url or "")


def parse_remote(target):
    """Recognise a remote repository. Returns None for anything that should be treated as a local path."""
    t = target.strip()
    if t.startswith(("./", "../", "~", "/", ".\\")) or t in (".", ".."):
        return None
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+)(/[^#?]*)?", t)
    if m:
        owner, repo, rest = m.group(1), re.sub(r"\.git$", "", m.group(2)), (m.group(3) or "").strip("/")
        info = {"host": "github.com", "owner": owner, "repo": repo,
                "urls": [f"https://github.com/{owner}/{repo}.git", f"git@github.com:{owner}/{repo}.git"],
                "web": f"https://github.com/{owner}/{repo}", "refpath": "", "blob": False}
        parts = rest.split("/") if rest else []
        if len(parts) >= 2 and parts[0] in ("tree", "blob"):
            info["refpath"], info["blob"] = "/".join(parts[1:]), parts[0] == "blob"
        return info
    m = re.match(r"^git@github\.com:([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", t)
    if m:
        owner, repo = m.group(1), m.group(2)
        return {"host": "github.com", "owner": owner, "repo": repo,
                "urls": [t, f"https://github.com/{owner}/{repo}.git"],
                "web": f"https://github.com/{owner}/{repo}", "refpath": "", "blob": False}
    m = re.match(r"^(?:[\w.-]+@)?([\w.-]+):([^\s]+?)(?:\.git)?/?$", t)
    if m and "://" not in t and not re.match(r"^[A-Za-z]:[\\/]", t) and "/" in m.group(2) and not Path(t).expanduser().exists():
        bits = m.group(2).split("/")
        return {"host": m.group(1), "owner": bits[-2], "repo": bits[-1], "urls": [t], "web": "",
                "refpath": "", "blob": False}
    if re.match(r"^(https?|ssh|git)://", t):
        from urllib.parse import urlparse
        u = urlparse(t)
        bits = [b for b in u.path.split("/") if b]
        if len(bits) < 2:
            raise TargetError(f"can't tell the owner and repo from {t}")
        return {"host": u.hostname or "remote", "owner": bits[-2], "repo": re.sub(r"\.git$", "", bits[-1]),
                "urls": [t], "web": "", "refpath": "", "blob": False}
    if re.match(r"^\w[\w.-]*/[\w.-]+$", t) and not Path(t).expanduser().exists():
        owner, repo = t.split("/")
        return parse_remote(f"https://github.com/{owner}/{repo}")
    return None


def remote_refs(url):
    out = git(["ls-remote", "--heads", "--tags", url], timeout=90)
    names = set()
    for line in out.splitlines():
        ref = line.split("\t")[-1]
        names.add(re.sub(r"^refs/(heads|tags)/", "", ref).replace("^{}", ""))
    return names


def clone_remote(info):
    """Shallow-fetch the repo into the cache with the user's own git credentials. Returns (dir, url, ref, sha)."""
    url, refs, errors = None, set(), []
    for cand in info["urls"]:
        try:
            refs = remote_refs(cand)
            url = cand
            break
        except TargetError as e:
            errors.append(f"{strip_userinfo(cand)}: {e}")
    if not url:
        raise TargetError("could not reach the repository. If it is private, check that `git clone "
                          + strip_userinfo(info["urls"][0]) + "` works in your terminal.\n" + "\n".join(errors))
    ref, sub = "", ""
    if info["refpath"]:
        parts = info["refpath"].split("/")
        ref, sub = parts[0], "/".join(parts[1:])
        for i in range(len(parts), 0, -1):
            cand = "/".join(parts[:i])
            if cand in refs:
                ref, sub = cand, "/".join(parts[i:])
                break
        if info["blob"]:
            sub = str(Path(sub).parent) if "/" in sub else ""
    dest = CACHE / "repos" / slug(f"{info['host']}-{info['owner']}-{info['repo']}" + (f"-at-{ref}" if ref else ""))
    if not (dest / ".git").is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        git(["init", "-q"], cwd=dest)
    if git(["remote"], cwd=dest, check=False).split().count("origin"):
        git(["remote", "set-url", "origin", url], cwd=dest)
    else:
        git(["remote", "add", "origin", url], cwd=dest)
    log(f"fetching {strip_userinfo(url)}" + (f" @ {ref}" if ref else ""))
    git(["fetch", "-q", "--depth", "1", "--no-tags", "origin", ref or "HEAD"], cwd=dest, timeout=1800)
    git(["checkout", "-q", "--force", "--detach", "FETCH_HEAD"], cwd=dest)
    git(["clean", "-qfdx"], cwd=dest)
    sha = git(["rev-parse", "HEAD"], cwd=dest)
    return dest, url, ref, sub.strip("/"), sha


def resolve_target(target):
    info = parse_remote(target)
    if info:
        dest, url, ref, sub, sha = clone_remote(info)
        root = (dest / sub) if sub else dest
        if not root.is_dir():
            raise TargetError(f"'{sub}' is not a folder in {info['owner']}/{info['repo']}")
        web = info["web"]
        display = (web or strip_userinfo(url)) + (f"/tree/{ref or sha[:12]}/{sub}" if sub else "")
        name = slug(f"{info['owner']}-{info['repo']}" + (f"-{sub}" if sub else ""))
        return {"kind": "clone", "root": root, "checkout": dest, "remote": strip_userinfo(url), "web": web,
                "ref": ref, "subdir": sub, "sha": sha, "display": display, "name": name, "key": sha[:12]}
    root = Path(target).expanduser().resolve()
    if not root.is_dir():
        raise TargetError(f"{target} is neither a folder on this machine nor a repository URL")
    sha = git(["rev-parse", "HEAD"], cwd=root, check=False)
    key = sha[:12]
    if sha:
        status = git(["status", "--porcelain", "--", "."], cwd=root, check=False)
        if status:
            names = "\n".join(sorted(line[3:] for line in status.splitlines()))
            key += "-wip" + hashlib.sha1(names.encode()).hexdigest()[:6]
    return {"kind": "local", "root": root, "checkout": root, "remote": "", "web": "", "ref": "", "subdir": "",
            "sha": sha, "display": scrub_paths(str(root)), "name": slug(root.name), "key": key}


def tree_fingerprint(root, files):
    h = hashlib.sha1()
    for f in files:
        try:
            st = (Path(root) / f).stat()
            h.update(f"{f}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
        except OSError:
            pass
    return h.hexdigest()[:12]


def coverage_note(t):
    if t["kind"] == "clone":
        return ("Mapped from the repository's contents only. User-level settings, connectors (MCP servers "
                "configured outside the repo), scheduled jobs, and secrets usually live outside a repository, "
                "so they are not visible here.")
    return ("Mapped from a folder on this machine, including untracked files that git doesn't ignore. Agent "
            "config found outside the folder (user-level hooks and MCP servers, launchd jobs) is included only "
            "where it applies, and is labelled as this machine's config, not the folder's.")


# ----------------------------------------------------------------------------------------------
# prompts
# ----------------------------------------------------------------------------------------------

PREAMBLE = ("You are writing part of an Exploded View map: an interactive diagram that shows a newcomer how a "
            "software system works at runtime, one readable level at a time.")


def depth_line(depth):
    return f"Depth: this map is the top level plus {depth} level{'s' if depth != 1 else ''} of drill-in."


def check_block(engine, out, which):
    return f"""Then check what you wrote:
  python3 "{engine}" check-part "{out}" {which}
Fix every error it reports and run it again until it says ok. Warnings are advisory: fix the cheap ones, ignore the rest."""


def one_pass_prompt(sc, depth, dest, check=""):
    return f"""{PREAMBLE}

The repository is at {sc['root']}. Read the files that matter with Read, Glob and Grep; the digest at the end is a starting point, not the whole truth.

{depth_line(depth)} Write the complete map in one pass, nesting "children" directly wherever a part hides real structure, down to {depth} level{'s' if depth != 1 else ''} below the top. This is one-pass mode, so leave out "scope".

{RULES}

{SCHEMA_DOC}

Write the JSON object (title, summary, root) to {dest}. JSON only: no comments, no markdown fences.
{check}

--- SCAN DIGEST ---
{scan_digest(sc)}
"""


def overview_prompt(sc, job):
    return f"""{PREAMBLE}

The repository is at {sc['root']}. Read the files that explain how the system runs (README, entry points, manifests, the main modules) with Read, Glob and Grep; the digest at the end is a starting point, not the whole truth. About a dozen well-chosen files is usually enough at this level.

{depth_line(job['depth'])} You are writing the TOP LEVEL ONLY: 5–10 nodes, their edges, and 2–5 flows. Do not write "children". This is two-pass mode: give each node that hides real structure a "scope", the folders, globs, or files it owns. The engine hands every scoped node to a separate writer who drills into it, so scopes should cover the important code and overlap little. People, outside services, and single-file parts need no scope.

{RULES}

{SCHEMA_DOC}

Write the JSON object (title, summary, root) to:
  {job['parts']}/_overview.json
JSON only: no comments, no markdown fences.
{check_block(job['engine'], job['out'], '_overview')}

--- SCAN DIGEST ---
{scan_digest(sc)}
"""


def expand_prompt(sc, job, title, trail, node, siblings, level, part_file, path):
    depth = job["depth"]
    sib = "\n".join(f"  - {s['id']}: {s.get('label', '')} ({s.get('kind', '')}): {s.get('summary', '')}"
                    for s in siblings if s.get("id") != node.get("id"))
    scope = node.get("scope") or node.get("files") or []
    if level < depth:
        shape = ("Write 3–10 nodes, their edges, and 0–3 flows. There is room for one more level below this one. "
                 "For a sub-part that hides structure: if it owns fewer than about 15 files, nest its \"children\" "
                 "directly (leaf nodes only, with no children of their own); if it is bigger, give it a \"scope\" "
                 "instead and a separate writer will drill into it. Never both on one node.")
    else:
        shape = ("Write 3–10 nodes, their edges, and 0–3 flows. This is the deepest level: no \"scope\" and no "
                 "\"children\".")
    return f"""{PREAMBLE}

The repository is at {sc['root']}. Read the files in this part's scope with Read, Glob and Grep before you describe them.

System: {title}
Where you are: {' › '.join(trail)}  (drill-in level {level}; {depth_line(depth)[7:]})
You are expanding ONE part into its internals:
  id: {node.get('id')}
  label: {node.get('label', '')}
  kind: {node.get('kind', '')}
  summary: {node.get('summary', '')}
  scope: {json.dumps(scope)}

Its siblings at the level above. Draw edges to them as "../<id>" to show what this part talks to:
{sib or '  (none)'}

{shape}

{RULES}

{SCHEMA_DOC}

Write ONLY the Graph object {{"nodes": [...], "edges": [...], "flows": [...]}} to:
  {part_file}
JSON only: no comments, no markdown fences.
{check_block(job['engine'], job['out'], path)}

When you are done, reply with one line: "ok", or what went wrong. Don't paste the JSON into your reply.

--- SCAN DIGEST (this part's scope) ---
{scan_digest(sc, max_files=160, scope=scope)}
"""


# ----------------------------------------------------------------------------------------------
# jobs: an output folder holding the scan, the prompts, and the parts the session writes
# ----------------------------------------------------------------------------------------------

def save(obj, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
    return path


def load_json_lenient(path):
    text = Path(path).read_text()
    try:
        return json.loads(text)
    except ValueError:
        text = text.strip()
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if m:
            text = m.group(1)
        a, b = text.find("{"), text.rfind("}")
        if a == -1 or b == -1:
            raise ValueError("no JSON object found")
        return json.loads(text[a: b + 1])


def as_graph(obj):
    if isinstance(obj, list):
        return {"nodes": obj}
    if isinstance(obj, dict):
        if isinstance(obj.get("root"), dict):
            return obj["root"]
        if isinstance(obj.get("graph"), dict):
            return obj["graph"]
        if isinstance(obj.get("children"), dict) and "nodes" not in obj:
            return obj["children"]
        return obj
    raise ValueError("a part must be a JSON object")


def part_key(path_ids):
    return ".".join(slug(str(i)) for i in path_ids)


def node_path(path_ids):
    return "/".join(str(i) for i in path_ids)


def load_job(out):
    out = Path(out).expanduser().resolve()
    jp = out / "job.json"
    if not jp.exists():
        sys.exit(f"exploded-view: no job.json in {out} (run prepare first)")
    job = json.loads(jp.read_text())
    job["out"], job["parts"] = str(out), str(out / "parts" / job["key"])
    job["engine"] = str(Path(__file__).resolve())
    return job


def load_scan(job):
    sc = json.loads((Path(job["out"]) / "scan.json").read_text())
    sc["root"] = job["scan_root"]
    return sc


def read_overview(job):
    p = Path(job["parts"]) / "_overview.json"
    if not p.exists():
        return None
    obj = load_json_lenient(p)
    if isinstance(obj, dict) and isinstance(obj.get("root"), dict):
        return obj
    return {"title": "", "summary": "", "root": as_graph(obj)}


def owned_files(node, all_files):
    scope = node.get("scope") or [f for f in (node.get("files") or []) if not f.startswith(("~", "/"))]
    return [f for f in all_files if in_scope(f, scope)] if scope else []


def assemble(job, sc):
    """Overview plus every part written so far. Returns (map, pending drill-ins, broken part files)."""
    m = read_overview(job)
    if m is None:
        return None, [], []
    all_files = [f["path"] for f in sc["files"]]
    title = m.get("title") or sc["name"]
    pending, broken = [], []

    def walk(graph, path_ids, trail, level):
        for n in graph.get("nodes") or []:
            if not isinstance(n, dict) or not n.get("id"):
                continue
            ids = path_ids + [n["id"]]
            pf = Path(job["parts"]) / (part_key(ids) + ".json")
            if not (isinstance(n.get("children"), dict) and n["children"].get("nodes")):
                if pf.exists():
                    try:
                        g = as_graph(load_json_lenient(pf))
                        if g.get("nodes"):
                            n["children"] = g
                    except (ValueError, OSError) as e:
                        broken.append({"path": node_path(ids), "error": str(e)[:160]})
                if not n.get("children") and level < job["depth"] and job["mode"] == "two-pass" \
                        and n.get("kind") not in ("person", "external") and n.get("scope"):
                    owned = owned_files(n, all_files)
                    if len(owned) >= 3:
                        pending.append({"ids": ids, "node": n, "siblings": graph.get("nodes") or [],
                                        "trail": trail + [n.get("label") or n["id"]], "level": level + 1,
                                        "owned": len(owned), "part": str(pf)})
            if isinstance(n.get("children"), dict):
                walk(n["children"], ids, trail + [n.get("label") or n["id"]], level + 1)

    walk(m["root"], [], [title], 0)
    return m, pending, broken


def count_parts(job):
    d = Path(job["parts"])
    return len([p for p in d.glob("*.json") if not p.name.startswith("_")]) if d.is_dir() else 0


# ----------------------------------------------------------------------------------------------
# validate + render
# ----------------------------------------------------------------------------------------------

def _ref_ok(v, anc):
    ups = 0
    while v.startswith("../"):
        ups, v = ups + 1, v[3:]
    return 0 < ups <= len(anc) and v in anc[-ups]


def validate_graph(g, trail, anc, problems, warn, fix=True):
    if not isinstance(g, dict):
        problems.append(f"{trail}: a graph must be an object with 'nodes'")
        return
    nodes = [n for n in (g.get("nodes") or []) if isinstance(n, dict)]
    if len(nodes) != len(g.get("nodes") or []):
        problems.append(f"{trail}: every node must be an object")
    g["nodes"] = nodes
    ids = set()
    for n in nodes:
        if not n.get("id"):
            n["id"] = slug(n.get("label", "node"))
        if n["id"] in ids:
            problems.append(f"{trail}: duplicate id '{n['id']}'")
        ids.add(n["id"])
        if not n.get("label"):
            n["label"] = n["id"]
        if n.get("kind") and n["kind"] not in KINDS:
            problems.append(f"{trail}: '{n['id']}' has unknown kind '{n['kind']}' (use one of {', '.join(KINDS)})")
        if n.get("status") and n["status"] not in ("live", "planned", "deprecated"):
            problems.append(f"{trail}: '{n['id']}' has unknown status '{n['status']}'")
        if n.get("files") is not None and not isinstance(n["files"], list):
            problems.append(f"{trail}: 'files' of '{n['id']}' must be a list")
        for f in n.get("files") or []:
            if isinstance(f, str) and re.match(r"^(/Users/|/home/|[A-Za-z]:\\)", f):
                warn.append(f"{trail}: '{n['id']}' lists an absolute path; use a repo-relative one")
    if len(nodes) > MAX_LEVEL:
        warn.append(f"{trail}: {len(nodes)} nodes at one level (aim for 12 or fewer)")
    for n in nodes:
        if len(str(n.get("label", "")).split()) > 5:
            warn.append(f"{trail}: label '{n['label']}' is long (aim for 2–4 words)")
        if len(str(n.get("summary", "")).split()) > 30:
            warn.append(f"{trail}: summary of '{n['label']}' runs {len(str(n['summary']).split())} words (aim for under 22)")
        if n.get("children") is not None and not (isinstance(n["children"], dict) and n["children"].get("nodes")):
            n.pop("children")
    keep = []
    for e in g.get("edges") or []:
        if not isinstance(e, dict):
            problems.append(f"{trail}: every edge must be an object")
            continue
        ok = True
        for end in ("from", "to"):
            v = e.get(end, "")
            if isinstance(v, str) and v.startswith("../"):
                if not _ref_ok(v, anc):
                    problems.append(f"{trail}: edge points at unknown outside node '{v}'")
                    ok = False
            elif v not in ids:
                problems.append(f"{trail}: edge {e.get('from')}→{e.get('to')} references unknown '{v}'")
                ok = False
        if e.get("kind") not in (None, "", "data", "async"):
            warn.append(f"{trail}: edge {e.get('from')}→{e.get('to')} has kind '{e.get('kind')}' (use '', data, or async)")
        if ok or not fix:
            keep.append(e)
    g["edges"] = keep
    if len(nodes) > 1:
        touched = set()
        for e in keep:
            touched.add(e.get("from"))
            touched.add(e.get("to"))
        lonely = [n["label"] for n in nodes if n["id"] not in touched]
        if lonely:
            warn.append(f"{trail}: no connections for {', '.join(map(str, lonely[:6]))}")
    if "flows" in g and not g["flows"]:
        g.pop("flows")
    for f in g.get("flows") or []:
        for s in (f.get("steps") or []) if isinstance(f, dict) else []:
            for end in ("from", "to", "node"):
                v = s.get(end) if isinstance(s, dict) else None
                if v and not (v in ids or (isinstance(v, str) and v.startswith("../") and _ref_ok(v, anc))):
                    problems.append(f"{trail}: flow '{f.get('name')}' step references unknown '{v}'")
    for n in nodes:
        if n.get("children"):
            validate_graph(n["children"], f"{trail} › {n['label']}", anc + [ids], problems, warn, fix)


def validate(m, fix=True, warnings=None):
    """Return a list of errors (things that break the map). Readability issues go into `warnings`."""
    problems = []
    warn = warnings if warnings is not None else []
    if not isinstance(m, dict) or not isinstance(m.get("root"), dict):
        return ["map has no 'root' graph"]
    validate_graph(m["root"], m.get("title", "root"), [], problems, warn, fix)
    return problems


def report(m):
    warns = []
    for p in validate(m, warnings=warns)[:20]:
        log("fixed: " + p)
    for w in warns[:12]:
        log("warning: " + w)


def render(m, out_path):
    tpl = (HERE / "viewer.html").read_text()
    elk = (HERE / "vendor" / "elk.bundled.js").read_text()
    data = json.dumps(m, ensure_ascii=False).replace("</", "<\\/")
    title = (m.get("title") or NAME).replace("<", "&lt;")
    html = tpl.replace("__TITLE__", f"{title} · {NAME}", 1)
    elk_note = "/* elkjs 0.9.3 (Eclipse Layout Kernel), Eclipse Public License 2.0, source: https://github.com/kieler/elkjs */\n"
    html = html.replace("<script>__ELK__</script>",
                        "<script>" + elk_note + elk.replace("</script", "<\\/script") + "</script>", 1)
    html = html.replace("__DATA__", data, 1)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(html)
    return out_path


TEXT_KEYS = {"title", "summary", "label", "details", "text", "name", "kindLabel"}


def sanitize(obj, roots):
    """Last line of defence before a map is written: no secrets, no absolute paths of the runner."""
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k in TEXT_KEYS and isinstance(v, str):
                obj[k] = redact(scrub_paths(v, roots))
            elif k == "files" and isinstance(v, list):
                obj[k] = [scrub_paths(f, roots) if isinstance(f, str) else f for f in v]
            elif isinstance(v, (dict, list)):
                sanitize(v, roots)
    elif isinstance(obj, list):
        for x in obj:
            sanitize(x, roots)
    return obj


def strip_scope(graph):
    for n in graph.get("nodes", []):
        if n.get("scope") and not n.get("files"):
            n["files"] = n["scope"][:8]
        n.pop("scope", None)
        if n.get("children"):
            strip_scope(n["children"])


def map_stats(graph, depth=0):
    nodes, flows, deepest = 0, len(graph.get("flows") or []), depth
    for n in graph.get("nodes") or []:
        nodes += 1
        if n.get("children"):
            a, b, c = map_stats(n["children"], depth + 1)
            nodes, flows, deepest = nodes + a, flows + b, max(deepest, c)
    return nodes, flows, deepest


def find_graph(m, ids):
    """Walk node ids down from the root. Returns (graph containing the last node, ancestor id sets, node)."""
    g, anc, node, trail = m["root"], [], None, [m.get("title") or "root"]
    for i, nid in enumerate(ids):
        node = next((n for n in g.get("nodes") or [] if isinstance(n, dict) and n.get("id") == nid), None)
        if node is None:
            return None, None, None, None
        if i < len(ids) - 1:
            anc.append({n.get("id") for n in g.get("nodes") or [] if isinstance(n, dict)})
            trail.append(node.get("label") or nid)
            g = node.get("children") or {}
    anc.append({n.get("id") for n in g.get("nodes") or [] if isinstance(n, dict)})
    trail.append(node.get("label") or ids[-1])
    return g, anc, node, trail


# ----------------------------------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------------------------------

def emit(obj):
    print(json.dumps(obj, indent=1, ensure_ascii=False))


def cmd_prepare(a):
    try:
        t = resolve_target(a.target)
    except TargetError as e:
        sys.exit(f"exploded-view: {e}")
    sc = scan(t["root"], local=t["kind"] == "local")
    if not sc["file_count"]:
        sys.exit("exploded-view: no files found to map")
    key = t["key"] or "nogit-" + tree_fingerprint(t["root"], [f["path"] for f in sc["files"]])
    out = Path(a.out).expanduser().resolve() if a.out else (MAPS_HOME / t["name"]).resolve()
    parts = out / "parts" / key
    if a.fresh and parts.exists():
        shutil.rmtree(parts)
    parts.mkdir(parents=True, exist_ok=True)
    if (out / "prompts").exists():
        shutil.rmtree(out / "prompts")
    (out / "prompts").mkdir(parents=True)
    mode = "one-pass" if sc["file_count"] <= SMALL_REPO and not a.two_pass else "two-pass"
    depth = max(1, min(a.depth, 4))
    sc["display_root"] = t["display"]
    job = {"exploded_view": VERSION, "target": a.target if t["kind"] == "clone" else t["display"],
           "kind": t["kind"], "source": t["display"], "remote": t["remote"], "web": t["web"], "ref": t["ref"],
           "subdir": t["subdir"], "commit": t["sha"], "key": key, "scan_root": str(t["root"]),
           "checkout": str(t["checkout"]), "mode": mode, "depth": depth, "max_agents": a.max_agents,
           "files": sc["file_count"], "prepared": now_stamp(), "coverage": coverage_note(t)}
    save(job, out / "job.json")
    save(sc, out / "scan.json")
    (out / "digest.md").write_text(scan_digest(sc))
    job = load_job(out)
    ov = Path(job["parts"]) / "_overview.json"
    if mode == "one-pass":
        prompt = one_pass_prompt(sc, depth, ov, check_block(job["engine"], job["out"], "_overview"))
    else:
        prompt = overview_prompt(sc, job)
    (out / "prompts" / "overview.md").write_text(prompt)
    cached = ov.exists()
    emit({"out": job["out"], "source": t["display"], "commit": t["sha"][:12] or "(not a git repo)",
          "files": sc["file_count"], "mode": mode, "depth": f"top level plus {depth} level(s) of drill-in",
          "max_agents": a.max_agents,
          "overview_prompt": str(out / "prompts" / "overview.md"), "overview_part": str(ov),
          "overview_cached": cached, "drill_ins_cached": count_parts(job),
          "next": ("run `next`" if mode == "two-pass" else "run `merge`") if cached
          else "read the overview prompt, write the overview part, check it"})


def cmd_next(a):
    job = load_job(a.out)
    sc = load_scan(job)
    m, pending, broken = assemble(job, sc)
    if m is None:
        sys.exit("exploded-view: the overview part hasn't been written yet")
    attempts_file = Path(job["parts"]) / "_attempts.json"
    attempts = json.loads(attempts_file.read_text()) if attempts_file.exists() else {}
    given_up = [p for p in pending if attempts.get(node_path(p["ids"]), 0) >= 2]
    pending = [p for p in pending if attempts.get(node_path(p["ids"]), 0) < 2]
    budget = max(0, job["max_agents"] - count_parts(job))
    pending.sort(key=lambda p: -p["owned"])
    take, skipped = pending[:budget], pending[budget:]
    for p in take:
        attempts[node_path(p["ids"])] = attempts.get(node_path(p["ids"]), 0) + 1
    save(attempts, attempts_file)
    title = m.get("title") or sc["name"]
    rows = []
    for p in take:
        path = node_path(p["ids"])
        pf = Path(job["out"]) / "prompts" / (part_key(p["ids"]) + ".md")
        pf.write_text(expand_prompt(sc, job, title, p["trail"], p["node"], p["siblings"], p["level"],
                                    p["part"], path))
        rows.append({"path": path, "label": p["node"].get("label"), "files": p["owned"], "level": p["level"],
                     "prompt": str(pf)})
    emit({"pending": rows, "skipped_for_agent_budget": len(skipped), "written_so_far": count_parts(job),
          "given_up_after_two_tries": [node_path(p["ids"]) for p in given_up], "broken_parts": broken, "next": "dispatch one subagent per pending row, then run `next` again"
          if rows else "run `merge`"})


def cmd_prompt_expand(a):
    job = load_job(a.out)
    sc = load_scan(job)
    m, pending, _ = assemble(job, sc)
    if m is None:
        sys.exit("exploded-view: the overview part hasn't been written yet")
    ids = [x for x in a.path.split("/") if x]
    g, anc, node, trail = find_graph(m, ids)
    if node is None:
        sys.exit(f"exploded-view: no node at '{a.path}'")
    pf = Path(job["parts"]) / (part_key(ids) + ".json")
    dest = Path(job["out"]) / "prompts" / (part_key(ids) + ".md")
    dest.write_text(expand_prompt(sc, job, m.get("title") or sc["name"], trail, node, g.get("nodes") or [],
                                  len(ids), str(pf), a.path))
    emit({"path": a.path, "prompt": str(dest), "part": str(pf)})


def cmd_check_part(a):
    job = load_job(a.out)
    sc = load_scan(job)
    which = a.path.strip("/")
    problems, warns = [], []
    if which in ("_overview", "overview"):
        p = Path(job["parts"]) / "_overview.json"
        try:
            ov = read_overview(job)
        except (ValueError, OSError) as e:
            ov, problems = None, [f"not valid JSON: {e}"]
        if ov is None and not problems:
            problems.append(f"{p} doesn't exist yet")
        if ov is not None:
            if not (ov.get("title") and ov.get("summary")):
                warns.append("give the map a title and a two-sentence summary")
            problems += validate(ov, fix=False, warnings=warns)
            if job["mode"] == "two-pass":
                for n in ov["root"].get("nodes") or []:
                    if n.get("children"):
                        problems.append(f"'{n.get('id')}' has children; in two-pass mode give it a scope instead")
                    if not n.get("scope") and n.get("kind") not in ("person", "external") \
                            and len(n.get("files") or []) > 1:
                        warns.append(f"'{n.get('id')}' has no scope, so it won't get a drill-in")
                if len(ov["root"].get("flows") or []) < 2:
                    warns.append("the top level should carry 2–5 flows")
    else:
        ids = [x for x in which.split("/") if x]
        pf = Path(job["parts"]) / (part_key(ids) + ".json")
        m = read_overview(job)
        g = anc = None
        if m is not None:
            m, _, _ = assemble(job, sc)
            g, anc, node, trail = find_graph(m, ids)
        if g is None:
            problems.append(f"no node at '{which}' in the overview")
        elif not pf.exists():
            problems.append(f"{pf} doesn't exist yet")
        else:
            try:
                part = as_graph(load_json_lenient(pf))
                if not part.get("nodes"):
                    problems.append("the part has no nodes")
                else:
                    validate_graph(part, " › ".join(trail), anc, problems, warns, fix=False)
                    if len(ids) >= job["depth"]:
                        for n in part.get("nodes") or []:
                            if n.get("scope"):
                                warns.append(f"'{n.get('id')}' has a scope, but this is the deepest level; it won't be drilled into")
            except (ValueError, OSError) as e:
                problems.append(f"not valid JSON: {e}")
    if problems:
        print("errors:")
        for p_ in problems[:15]:
            print("  - " + p_)
    else:
        print("ok")
    if warns:
        print("warnings (advisory):")
        for w in warns[:8]:
            print("  - " + w)
    sys.exit(1 if problems else 0)


def cmd_merge(a):
    job = load_job(a.out)
    sc = load_scan(job)
    try:
        m, pending, broken = assemble(job, sc)
    except (ValueError, OSError) as e:
        sys.exit(f"exploded-view: the overview part isn't valid JSON ({e})")
    if m is None:
        sys.exit("exploded-view: the overview part hasn't been written yet")
    strip_scope(m["root"])
    roots = [job["scan_root"], job["checkout"]]
    sanitize(m, roots)
    m.setdefault("title", sc["name"])
    m.update({"source": job["source"], "generated": now_stamp(), "generator": "ai", "commit": job["commit"],
              "coverage": job["coverage"], "tool": {"name": NAME, "version": VERSION, "url": HOMEPAGE}})
    if job["web"] and job["commit"]:
        m["repo_url"] = job["web"]
        if job["subdir"]:
            m["subdir"] = job["subdir"]
    for k in ("repo_url", "subdir"):
        if not m.get(k):
            m.pop(k, None)
    fixed = validate(m, fix=True)
    warns = []
    remaining = validate(m, fix=False, warnings=warns)
    out = Path(job["out"])
    save(m, out / "map.json")
    html = render(m, out / "index.html")
    nodes, flows, deepest = map_stats(m["root"])
    print(f"map: {html}")
    print(f"{nodes} parts across {deepest + 1} level{'s' if deepest else ''} · {flows} workflows · "
          f"{count_parts(job)} drill-ins written" + (f", {len(pending)} not written" if pending else ""))
    if broken:
        print(f"{len(broken)} part file(s) were not valid JSON and were left out: "
              + ", ".join(b["path"] for b in broken[:6]))
    print("validator: " + ("clean" if not fixed and not remaining else
                            f"{len(fixed)} broken reference(s) dropped") + f" · {len(warns)} readability warnings (advisory)")
    for p_ in fixed[:5]:
        print("  dropped: " + p_)
    if a.open:
        webbrowser.open(Path(html).resolve().as_uri())


def target_or_exit(target):
    try:
        return resolve_target(target)
    except TargetError as e:
        sys.exit(f"exploded-view: {e}")


def default_out(t, out):
    return Path(out).expanduser().resolve() if out else (MAPS_HOME / t["name"]).resolve()


def cmd_static(a):
    t = target_or_exit(a.target)
    out = default_out(t, a.out)
    sc = scan(t["root"], local=t["kind"] == "local")
    m = static_map(sc, max_depth=a.depth)
    sanitize(m, [t["root"], t["checkout"]])
    m.update({"source": t["display"], "commit": t["sha"], "coverage": coverage_note(t),
              "tool": {"name": NAME, "version": VERSION, "url": HOMEPAGE}})
    if t["web"] and t["sha"]:
        m["repo_url"] = t["web"]
        if t["subdir"]:
            m["subdir"] = t["subdir"]
    report(m)
    save(m, out / "static" / "map.json")
    log(f"wrote {render(m, out / 'static' / 'index.html')}")


def cmd_prompt(a):
    t = target_or_exit(a.target)
    sc = scan(t["root"], local=t["kind"] == "local")
    sc["display_root"] = t["display"]
    out = default_out(t, a.out)
    dest = out / "prompt.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(one_pass_prompt(sc, a.depth, out / "map.json"))
    log(f"wrote {dest}")
    log(f"next: give that prompt to Claude, then run: python3 {Path(__file__).name} render {out / 'map.json'} --open")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="explodedview", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"{NAME} {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="resolve, clone, scan, write the digest and overview prompt")
    p.add_argument("target", help="GitHub URL (or owner/repo, or any git URL) or a local folder")
    p.add_argument("-o", "--out", help=f"output folder (default {MAPS_HOME}/<name>)")
    p.add_argument("--depth", type=int, default=DEFAULT_DEPTH, help="drill-in levels below the top level (default 2)")
    p.add_argument("--max-agents", type=int, default=DEFAULT_AGENTS, help="cap on drill-in writers (default 30)")
    p.add_argument("--two-pass", action="store_true", help="use drill-in writers even for a small repo")
    p.add_argument("--fresh", action="store_true", help="ignore parts cached for this commit")
    p = sub.add_parser("next", help="list (and write prompts for) the drill-ins still to write")
    p.add_argument("out")
    p = sub.add_parser("prompt-expand", help="write the prompt for one drill-in")
    p.add_argument("out")
    p.add_argument("path", help="node path, e.g. api/auth")
    p = sub.add_parser("check-part", help="validate one written part")
    p.add_argument("out")
    p.add_argument("path", help="_overview or a node path")
    p = sub.add_parser("merge", help="assemble, validate, render index.html")
    p.add_argument("out")
    p.add_argument("--open", action="store_true")

    p = sub.add_parser("static", help="static map → <out>/static/index.html")
    p.add_argument("target")
    p.add_argument("-o", "--out")
    p.add_argument("--depth", type=int, default=4)
    p = sub.add_parser("prompt", help="write a one-pass authoring prompt")
    p.add_argument("target")
    p.add_argument("-o", "--out")
    p.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    p = sub.add_parser("scan", help="write scan.json")
    p.add_argument("target")
    p.add_argument("-o", "--out", help="output file")
    p = sub.add_parser("render", help="map.json → html")
    p.add_argument("map")
    p.add_argument("-o", "--out", help="output html (default: index.html next to the map)")
    p.add_argument("--open", action="store_true")
    p = sub.add_parser("validate", help="check a map.json")
    p.add_argument("map")

    a = ap.parse_args(argv)
    handlers = {"prepare": cmd_prepare, "next": cmd_next, "prompt-expand": cmd_prompt_expand,
                "check-part": cmd_check_part, "merge": cmd_merge, "static": cmd_static, "prompt": cmd_prompt}
    if a.cmd in handlers:
        return handlers[a.cmd](a)
    if a.cmd == "scan":
        t = target_or_exit(a.target)
        sc = scan(t["root"], local=t["kind"] == "local")
        dest = Path(a.out) if a.out else default_out(t, None) / "scan.json"
        log(f"wrote {save(sc, dest)}")
    elif a.cmd == "render":
        mp = Path(a.map).expanduser().resolve()
        m = json.loads(mp.read_text())
        report(m)
        dest = Path(a.out) if a.out else mp.parent / "index.html"
        render(m, dest)
        log(f"wrote {dest}")
        if a.open:
            webbrowser.open(dest.resolve().as_uri())
    elif a.cmd == "validate":
        m = json.loads(Path(a.map).read_text())
        warns = []
        probs = validate(m, fix=False, warnings=warns)
        for p_ in probs:
            print("error:", p_)
        for w in warns:
            print("warning (advisory):", w)
        if not probs:
            print("ok" + (f" ({len(warns)} readability warnings, advisory)" if warns else ""))
        sys.exit(1 if probs else 0)


if __name__ == "__main__":
    main()
