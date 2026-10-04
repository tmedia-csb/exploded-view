# Exploded View: troubleshooting

Read this only when a step fails. `EV` is `python3 "${CLAUDE_PLUGIN_ROOT}/engine/explodedview.py"`.

## The clone fails

The engine runs the user's own `git` with prompts turned off, trying the HTTPS URL first and then SSH for GitHub. A private repo works wherever `git clone <url>` works in the user's terminal: a credential helper, `gh auth setup-git`, or an SSH key. If neither works, say so and stop. Never ask for a token or put credentials in a URL.

Clones are cached in `~/.cache/exploded-view/repos/` (or `$XDG_CACHE_HOME/exploded-view/repos/`). Deleting a folder there is always safe.

## Where things are written

Each map has its own output folder, `~/exploded-view/<name>/` by default (set `EXPLODED_VIEW_HOME` to move the parent, or pass `-o`). Inside it:

- `index.html`: the map. One file, no network needed, safe to share.
- `map.json`: the map data, for `EV render` or `EV validate`.
- `parts/<commit>/`: the overview and drill-ins, cached by commit. A re-run on the same commit reuses them; `--fresh` ignores them. A local folder with uncommitted changes gets its own cache key.
- `scan.json`, `digest.md`, `prompts/`, `job.json`: working files. `scan.json` and the prompts contain absolute local paths, so share `index.html`, not the folder.

## A check keeps failing

`EV check-part "<out>" <path>` prints each error with where it is. The usual ones:

- an edge or flow step that names an id that doesn't exist at that level (outside ids need `../`);
- an unknown `kind`;
- JSON that isn't valid, often a trailing comma or a markdown fence around it.

Warnings are advisory. A map with warnings is still a good map.

## A drill-in is missing

`EV next "<out>"` lists anything still pending, and `broken_parts` lists part files that aren't valid JSON. Run one by hand with `EV prompt-expand "<out>" <path>` and give the prompt to a subagent. `EV merge` always works with what exists: a missing drill-in leaves that part as a leaf.

## Permissions

Subagents need Read, Glob, and Grep on the repo, Write into the output folder, and Bash for the `check-part` command. If the user is asked to approve each write, allowing writes to `~/exploded-view/` for the session saves a lot of clicks.

## Without the plugin

`EV static <target>` builds a map from folders, imports, and links alone (no AI). `EV prompt <target>` writes a one-pass prompt that any Claude session can follow to write `map.json`; then `EV render map.json --open`.
