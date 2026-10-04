---
name: map
description: Map a codebase or an agent harness into an interactive HTML diagram of how it works, with drill-down parts, connections, and workflows you can play step by step. Takes a GitHub URL (public or private), owner/repo, any git URL, or a local folder. Use when the user asks to map, diagram, visualize, or explain how a repository, project, or Claude Code setup works, or runs /exploded-view:map.
argument-hint: "<github-url | owner/repo | local folder> [--depth N] [--max-agents N] [--fresh]"
---

# Exploded View

Map the target given in: $ARGUMENTS

You coordinate. The engine does the mechanical work, you write the top level of the map, and subagents write the drill-ins. The finished map is one HTML file that opens in any browser with no network.

The engine is:

```
python3 "${CLAUDE_PLUGIN_ROOT}/engine/explodedview.py"
```

Below it is written as `EV`. Always quote paths, because they can contain spaces.

## Context discipline (required)

Your context has to stay the same size whether the repo has 50 files or 50,000. So:

- The only file you read in full is the overview prompt in step 2. Beyond that, read the handful of repo files you need for the top level.
- Never read `map.json`, `index.html`, `scan.json`, `digest.md`, the files under `parts/`, or the drill-in prompts. The engine's short outputs tell you everything you need.
- Drill-ins happen in subagents, and their replies are one line each.

## 1. Prepare

Run `EV prepare "<target>"`, passing through any of these the user gave: `--depth N` (drill-in levels below the top; default 2), `--max-agents N` (cap on drill-in subagents; default 30), `--fresh` (ignore cached parts), `--two-pass` (use drill-in subagents even for a small repo). If no target was given, map the current working directory and say so.

The target can be a GitHub URL (including `/tree/<branch>/<folder>` links), `owner/repo`, any git URL, or a local path. Remote repos are shallow-cloned with the user's own git, so private repos work wherever their credentials do. If the clone fails, show the error and suggest checking that `git clone <url>` works in their terminal. Don't try to work around authentication.

The output is short JSON. Keep `out`, `mode`, `overview_prompt`, `overview_part`, and `overview_cached`.

## 2. The top level

If `overview_cached` is true, this commit was mapped before: skip to step 3 (two-pass) or step 4 (one-pass).

Otherwise read the file at `overview_prompt` and follow it. It holds the modelling rules, the schema, the digest of the repo, where to write, and the check command.

- `mode: "two-pass"` (larger repos): you write the top level only, with a `scope` on each part that hides structure.
- `mode: "one-pass"` (small repos, about 40 files or fewer): you write the whole map, with `children` nested directly.

Run the check command it gives you until it says `ok`.

## 3. Drill-ins (two-pass only)

Repeat until nothing is pending:

1. Run `EV next "<out>"`. It lists `pending` rows, each with a `label` and a `prompt` file.
2. Dispatch one subagent per row, all in a single message so they run in parallel (in batches of up to 10 if there are more). Use the general-purpose subagent, and run them in the foreground (`run_in_background: false`) so the whole wave's replies come back together before you continue. If a dispatch is refused for a concurrency limit, send that wave in smaller batches. Give each the description `Map: <label>` and this prompt, with the row's path filled in:

   > Read the file at `<prompt>` and follow its instructions exactly. It tells you which repository files to read, what JSON to write and where, and how to check it. Reply with one line: "ok" or what went wrong.

   Use the default model unless the user asked for a cheaper run, in which case pass `sonnet`.
3. When they have all replied, run `next` again. Deeper drill-ins appear as their parents are written.

If a subagent reports a failure, its row shows up again on the next `next`, which retries each drill-in once and then lists it under `given_up_after_two_tries`: a missing drill-in leaves a leaf in the map, never a broken one. `next` stops listing work once the agent budget is spent (`--max-agents`, default 30) and tells you how many smaller parts it skipped.

## 4. Merge and open

Run `EV merge "<out>" --open`. It assembles the parts, removes anything shaped like a secret or a local home-folder path, validates, writes `index.html`, and opens it.

Then tell the user, briefly:

- where the map is (the `map:` line) and the counts line;
- the validator line. Readability warnings are advisory, so don't apologise for them;
- for a cloned repo, that only the repo's contents were visible: user-level settings, MCP servers configured outside the repo, scheduled jobs, and secrets usually aren't in a repo;
- how to read it: click a card for details, double-click a stacked card to open it, Esc goes up, pick a workflow on the left to play it (arrow keys step), `/` searches every level.

Re-running on the same commit reuses every part already written, so a second run is quick. If something goes wrong, read `${CLAUDE_PLUGIN_ROOT}/skills/map/reference/troubleshooting.md`.
