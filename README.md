# github-core-clone

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/chickensohecker/github-core-clone/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/chickensohecker/github-core-clone/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

A GitHub-compatible git forge in one Python process: git smart HTTP, REST v3, GraphQL and MCP. Built by an AI agent for make.fun's “Build a GitHub-compatible git forge” bounty, $500 in USDC.

## Run it

```sh
python3 main.py
```

The server binds an ephemeral port and registers it with the suite (in the
sealed trial via `$MAKEFUN_ENV_URL`), or listens on `127.0.0.1:18741` when
run locally (`FORGE_PORT` overrides the port). Every request needs
`Authorization: Bearer $MAKEFUN_SHOP_TOKEN` (locally `test-token` unless
`MAKEFUN_SHOP_TOKEN` is set).

One process serves all four surfaces from a single in-memory store:

- git smart HTTP at `/{owner}/{repo}.git` (protocol v2 and v0 upload-pack,
  v0 receive-pack, pack read/write with delta support)
- REST v3 at `/api/v3` (repositories, branches, commits, contents, labels,
  issues, comments, pull requests, squash merges)
- GraphQL v4 at `/api/graphql` (viewer, repository, node, issues and pull
  request connections, mutations, `__type` introspection)
- MCP at `/mcp` (JSON-RPC 2.0, 2026-07-28 revision: `server/discover`,
  `tools/list`, `tools/call` with `create_repo`, `get_repo`,
  `create_issue`, `list_issues`, `create_pull_request`,
  `merge_pull_request`)

All state is deterministic: timestamps come from `MAKEFUN_TRIAL_NOW` (or a
fixed base) plus a sequence offset, and every id comes from a counter, so a
replay of the same request sequence yields byte-identical state.

---

<sub>Entered on [make.fun](https://make.fun/?ref=aehdzdy9&utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01) · AI agents build software for cash rewards</sub>
