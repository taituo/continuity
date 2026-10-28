# Running continuity with Kiro CLI V3 (ACP, no MCP)

continuity drives Kiro as an ACP client: one `kiro-cli acp` process per disposable session, JSON-RPC over stdio. MCP is not used anywhere: `session/new` always sends `mcpServers: []`, and every other capability is built without it:

| Capability | How it works without MCP |
|---|---|
| Seeing peers | Read-only snapshots in the session's `views/<sid>/` directory, with `PEERS.md` as the index |
| Recording what was read | inotify on the snapshot files, which catches built-in read tools and `cat` alike. Backed up by ACP tool `locations`, permission requests, and `fs/*` calls |
| Checkpoints | Taken after every tool call that finishes |
| Finishing | The `session/prompt` response decides the outcome |

## Quick start

Run this on the machine where Kiro is logged in. The worktrees have to be on the same machine as the agent that writes to them.

```sh
kiro-cli login                       # once; the runner uses the CLI's own login
python3 -m continuity init  ~/c/proj --from ~/src/proj
python3 -m continuity goal  ~/c/proj "Validate CLI arguments and print usage on error"
python3 -m continuity run   ~/c/proj \
    --check "python3 -m pytest -q" --protected tests/ \
    --holdout ~/hidden-tests --holdout-check "python3 -m pytest -q _holdout" \
    --parallel 3 --sandbox
python3 -m continuity status  ~/c/proj
python3 -m continuity history ~/c/proj g1
python3 -m continuity audit   ~/c/proj
```

The default agent command is `kiro-cli acp --agent-engine=v3 --auth-method=cli`.

Optional flags:
- `--mode`, `--model` and `--effort` are passed as `_meta.kiro.modeId`, `modelId` and `effortLevel` in `session/new`. V3 ACP no longer takes `--agent`, `--model` or `--trust-*` on the command line.
- From a laptop, everything works over ssh: `ssh host python3 -m continuity status ~/c/proj`.

Before a real run, try a dry run with the bundled fake agent. It speaks the same protocol:

```sh
python3 -m continuity run ~/c/proj --check "true" \
    --agent "python3 -m continuity.testing.fake_agent"   # with FAKE_AGENT_SCRIPT=script.json
```

## What the adapter relies on

These facts were checked against the Kiro docs on 2026-10-28.

| Fact | Where it is used | Source |
|---|---|---|
| Launch V3 with `kiro-cli acp --agent-engine=v3 --auth-method=cli`. `--agent`, `--model`, `--effort`, `--trust-all-tools` and `--trust-tools` are removed in V3 ACP | Default `--agent` | [ACP migration](https://kiro.dev/docs/cli/v3/acp-migration/), [V3 overview](https://kiro.dev/docs/cli/v3/) |
| Newline-delimited JSON-RPC 2.0 on stdio | `acp/jsonrpc.py` | ACP migration |
| `session/new` takes `cwd`, `additionalDirectories`, `mcpServers` and `_meta.kiro{modeId, modelId, effortLevel}` | `AcpSession.new_session`. The views dir is passed as an additional directory | ACP migration |
| A turn completes only when `session/prompt` returns. V3 sends no TurnEnd, and a `turn_end` kind is not completion | `AcpSession.run_turn` | ACP migration, [Kiro ACP](https://kiro.dev/docs/cli/acp/) |
| Stop reasons: `end_turn`, `max_tokens`, `max_turn_requests`, `refusal`, `cancelled` | `end_turn` → propose. `max_*` → propose as partial. Everything else → lost | [ACP prompt turn](https://agentclientprotocol.com/protocol/prompt-turn) |
| On cancel, the client sends `session/cancel` and must answer pending permission requests with `cancelled`. The agent answers `cancelled` | Timeout and stop path. Permission requests after a cancel get `cancelled` | ACP prompt turn |
| `session/request_permission` offers `allow_once`, `allow_always`, `reject_once` and `reject_always`. Context is in `_meta.kiro.consent{capability, resource, workspaceRoot, persistableConsent}`. "A permission response authorizes only the option and resource the agent supplied" | `acp/policy.py`. Only one-shot options are ever chosen, because a persistent allow would outlive the disposable agent | ACP migration, [ACP tool calls](https://agentclientprotocol.com/protocol/tool-calls) |
| Failed tool updates may carry `_meta.kiro.failureReason` (`cancelled`, `denied`, `error`) | Turn note `failures` | ACP migration |
| `session_info_update` dispatches on `_meta.kiro.kind` (`context_usage`, `turn_completion` …) | Recorded as-is in the turn note `info`; usable for cost budgets | ACP migration |
| Updates replayed by `session/load` carry `_meta.kiro.replay` | Ignored (sessions are never loaded) | ACP migration |
| Clients must read the advertised capabilities, ignore unknown methods and metadata, and return an explicit error for unsupported agent→client requests | `-32601` for unknown requests. Unknown notifications are ignored | [Kiro ACP overview](https://kiro.dev/docs/acp/) |
| Kiro's command sandbox covers shell commands but not built-in file tools; permission rules govern those | That is why `--sandbox` wraps the whole agent process in bubblewrap | [Sandbox](https://kiro.dev/docs/sandbox/) |
| Permission rules are `permissions.yaml` with scopes user, workspace (stored outside the repo), agent and session. Evaluation is deny > ask > allow, with no precedence between scopes | See the next section | [Permissions](https://kiro.dev/docs/permissions/), [V3 permissions](https://kiro.dev/docs/cli/v3/permissions/) |
| V3 becomes the default with CLI 3.0 (end of October 2026). `--agent-engine=v3` is explicit either way | Default `--agent` | [CLI V3 blog](https://kiro.dev/blog/cli-3/) |

For CLI v2 compatibility, the client also accepts the `session/notification` method name and PascalCase update kinds. Real ACP clients do the same; see batty-cli's [Kiro shim](https://docs.rs/batty-cli/0.11.3/src/batty_cli/shim/runtime_kiro.rs.html).

## Four layers of fencing

1. **Permission answers.** The runner answers every request:
   - Writes are allowed only inside the session's worktree, and never to protected paths unless the goal is privileged.
   - Reads of other worktrees, other sessions' views, `.continuity/` and the holdout are refused.
   - `mcp` and `subagent` are refused, and web tools can be turned off with `--no-network`.
   - Paths are resolved with `realpath`, so `..` and symlinks cannot escape.
2. **Client `fs/*`.** These calls are advertised and enforced by the same rules, in case Kiro routes file access through the client.
3. **`--sandbox` (bubblewrap).** Inside the sandbox the continuity root is an empty tmpfs except for this worktree (read-write) and its views (read-only). The holdout is hidden. This holds whatever Kiro's own permission settings say.
4. **Integration.** A delta touching a protected path is rejected even if it got past everything else, for example a `sed` in a shell.

## Not verifiable here: check on the first run

The build environment has no Kiro and no bubblewrap. The adapter is written so that each of these unknowns degrades safely, but check them once:

1. **Does V3 ask before writing inside the workspace?**
   - If it allows workspace writes silently, layer 1 never sees them. Protected paths are still rejected at layer 4, and `--sandbox` still fences everything outside.
   - To get write-time refusal as well, add an `ask` rule for `fs_write` on `tests/**` in an agent profile and select it with `--mode`.
   - Whether `modeId` selects custom agent profiles is not documented.
2. **Does Kiro use client `fs/read_text_file` and `fs/write_text_file` when they are advertised?** Either answer works: observation does not depend on it.
3. **What exactly is in `consent.resource`?** It might be an absolute path, a relative path, or a command string for shell. The policy handles all three and falls back to the tool call's `locations` and `rawInput`.
4. **Which directories does Kiro write at runtime?** `--sandbox` makes these read-write: `~/.kiro`, `~/.local/share/kiro-cli`, `~/.cache`, `~/.aws/sso/cache`, `$XDG_RUNTIME_DIR` and `$KIRO_HOME`. If the agent fails under `--sandbox`, read `ROOT/.continuity/agents/<sid>.stderr` and add the missing path (`Bwrap(rw=...)`).
5. **Does Kiro write anything into the workspace itself?** For example `.kiro/` runtime files. If it does and you don't want them in deltas, pass `--ignore '<pattern>'`. Build droppings such as `__pycache__` and `.git` are already ignored.
6. **Does a cancelled turn answer `cancelled` within the grace period?** If not, the runner kills the process group. Either way the session is recorded as lost and nothing hangs.

First run: `--parallel 1 --max-sessions 1` on a toy goal. Then check `history`, the agent's stderr log and `audit`.

## What the prompt contains

The prompt is built from the log in `context.py`. It contains:
- the goal and its ancestry
- the rules it will be judged by, including protected paths and the visible check command
- the convention for partial work: a `REMAINING:` line
- every earlier attempt at the goal, with outcome, intent and evaluator output (hidden-check output is withheld)
- for a resolution goal: the conflicting files, both sides' intents, the conflicted version and its diff
