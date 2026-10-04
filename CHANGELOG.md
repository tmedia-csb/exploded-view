# Changelog

## 0.2.0 (unreleased)

First release as a Claude Code plugin.

- `/exploded-view:map <target>` maps a GitHub URL (including `/tree/<branch>/<folder>` links), `owner/repo`, any git URL, or a local folder.
- The user's own Claude Code session writes the map: the top level in the main session, each drill-in in a parallel subagent. No API key, nothing hosted.
- The main session never reads the merged map, so context use doesn't grow with the size of the repo.
- Remote repos are shallow-cloned with the user's own git credentials; private repos work wherever `git clone` does.
- Parts are cached by commit, so a re-run on an unchanged repo reuses finished drill-ins.
- Maps of cloned repos say plainly that only the repo's contents were visible. Config outside the repo (user-level hooks, untracked MCP config, launchd jobs) is read only for local folders and labelled as machine config.
- Key files on GitHub maps link to the exact commit.
- Every map is scrubbed of secret-shaped strings and absolute home-folder paths before it is written.
- Viewer: Exploded View branding, a Source section with the repo, commit, and coverage note, and a fix for a negative zoom scale on narrow screens.

## 0.1 (prototype, "Blueprint")

A command-line tool with a scanner, a static mapper, two-pass AI mapping through the `claude` CLI, a validator, a redactor, and the ELK-based viewer.
