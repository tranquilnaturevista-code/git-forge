# Git Forge

[![make.fun](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01)
[![Rank](https://make.fun/badge/github-core-clone-01/tranquilnaturevista-code/git-forge/rank.svg)](https://make.fun/bounty/github-core-clone-01)
[![Reward](https://make.fun/badge/github-core-clone-01/tranquilnaturevista-code/git-forge/reward.svg)](https://make.fun/bounty/github-core-clone-01)

A standalone, GitHub-compatible git forge in pure Python 3.13 standard
library — one process, no UI, no outside service, no git binary. Built for
the [make.fun competition "Build a GitHub-compatible git forge"](https://make.fun/bounty/github-core-clone-01)
(github-core-clone-01).

## What it serves

- **Git smart HTTP** at `/{owner}/{repo}.git` — upload-pack protocol v2
  (ls-refs, fetch with shallow/deepen and haves) with a v0 fallback, and
  receive-pack v0 with report-status. Git objects (loose model, packfile
  read/write) are implemented in pure Python; fast-forward-only pushes.
- **REST v3** under `/api/v3` — repositories, branches, commits, contents,
  issues, comments, labels, pull requests and merges (squash).
- **GraphQL v4** at `/api/graphql` — the documents `gh` and
  `github-mcp-server` send, including introspection, batched operations and
  cursor pagination.
- **MCP** at `/mcp` — JSON-RPC 2.0 over Streamable HTTP, revision
  2026-07-28 (`server/discover`), with six tools: `create_repo`,
  `get_repo`, `create_issue`, `list_issues`, `create_pull_request`,
  `merge_pull_request`.

All four surfaces act on one shared in-memory store, so a repository or an
issue created over MCP reads back over REST with the same ids. Every id
comes from a monotonic counter and every timestamp is a fixed epoch, so two
runs fed the same requests behave identically.

## Layout

- `main.py` — HTTP server, routing, and the register/done lifecycle.
- `store.py` — the shared store: repositories, git objects, refs, issues,
  pull requests, labels, comments.
- `web.py` — request/response plumbing shared by the surfaces.
- `gitobjs.py` — pkt-line framing and packfile read/write.
- `githttp.py` — git smart-HTTP handlers.
- `rest.py` — REST v3 handlers.
- `graphql.py` — GraphQL parser and executor.
- `mcp.py` — MCP handlers and tools.

## Run it

```sh
python3 main.py
```

In a trial the process reads `MAKEFUN_ENV_URL` and `MAKEFUN_SHOP_TOKEN`
from the environment: it binds an ephemeral port, registers it with the
env, serves until the env says done, then exits 0. Without
`MAKEFUN_ENV_URL` it serves standalone on `$PORT` (default 0) for local
testing with the kit's runner:

```sh
node --experimental-strip-types sdk/run.ts --url http://127.0.0.1:PORT --token "$TOKEN" --perf --json
```

## License

MIT — see [LICENSE](LICENSE).
