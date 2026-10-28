# continuity

An autonomous development system built from disposable agents.

- You give it goals and constraints.
- Agents ([Kiro CLI V3](docs/kiro-v3.md) over ACP, or any ACP agent) are started for one task each, do their work in an isolated worktree, and are thrown away.
- A separate evaluator judges the merged result.
- The integrator merges what passes.

The source of truth is an append-only, hash-chained delta log, and system state is a fold over that log. That is why the work continues even though no agent, process or machine has to stay alive.

Pure Python stdlib. No MCP. The core is proven without any LLM.

- **What it is for:** [docs/scenarios.md](docs/scenarios.md). There are 22 use cases, each an end-to-end test.
- **Running it with Kiro:** [docs/kiro-v3.md](docs/kiro-v3.md). Covers the protocol facts it relies on (with sources) and a first-run checklist.

```sh
python3 -m continuity init  ~/c/proj --from ~/src/proj
python3 -m continuity goal  ~/c/proj "Validate CLI arguments and print usage on error"
python3 -m continuity run   ~/c/proj --check "python3 -m pytest -q" --protected tests/ --sandbox
python3 -m continuity status ~/c/proj
```

## How one session runs

1. **Prompt.** `start_session` creates a worktree from head. The prompt is built from the log: the goal and its ancestry, the rules, earlier attempts and why they failed, and for a resolution goal what collided.
2. **Start the agent.** `kiro-cli acp --agent-engine=v3 --auth-method=cli` is started, optionally inside bubblewrap. Then `initialize` and `session/new` with `cwd` set to the worktree and `mcpServers: []`. The session's read-only views directory is passed as an additional directory.
3. **The turn.** `session/prompt` runs. During the turn:
   - every `session/request_permission` is answered by policy, choosing one-shot options only;
   - a checkpoint is taken after each finished tool call;
   - other sessions' work appears as immutable snapshots in `views/<sid>/`, and every read of them is logged (inotify, plus ACP signals).
4. **Outcome.**
   - The prompt returns: `end_turn` means propose. A `REMAINING:` line or `max_*` means propose as partial, and the goal stays open. Anything else means lost, with the last state checkpointed for the next attempt.
   - The agent process group is killed in every case.
5. **Integration.** The integrator merges the delta onto head, evaluates exactly that tree (visible checks, then hidden ones whose output is withheld), and integrates with compare-and-swap. A conflict becomes a resolution goal. If a goal is already finished, its other attempts are superseded.

## Parts

| file | what |
|---|---|
| `store.py` | content-addressed blobs and trees (sha256); build droppings like `__pycache__` never enter a tree |
| `hlc.py` | Hybrid Logical Clock (Kulkarni et al. 2014) |
| `log.py` | hash-chained JSONL log; flock'd multi-writer appends; torn-tail recovery; truncation anchor |
| `state.py` | `fold(log)`; strict, so an illegal transition fails even if every hash was recomputed |
| `merge.py` | Myers diff, git-style hunk compaction, diff3, conflicts as data |
| `orchestrator.py` | sessions, observations, drift, integrator with compare-and-swap, supersede |
| `evaluators.py` | visible + hidden checks; the holdout never enters a tree and its output is never reported |
| `context.py` | the prompt, built from the log |
| `views.py`, `inotify.py` | per-session read-only snapshots of others' work; records which session read what |
| `acp/jsonrpc.py`, `acp/client.py` | ACP client for Kiro CLI V3 (v2-tolerant) |
| `acp/policy.py` | permission answers and the client `fs/*` fence |
| `sandbox.py` | bubblewrap around the whole agent process |
| `runner.py`, `__main__.py` | scheduler and CLI |
| `testing/fake_agent.py` | scriptable agent speaking the V3 wire protocol |
| `audit.py`, `sim.py` | independent audit; deterministic simulation with 11 invariants |

## Design decisions

- **No hidden state.** Every event is validated by the same `apply` that replay uses before it is written to disk. Other processes may append (for example `continuity goal` while a runner works). Appends catch up first under a lock, so the chain never forks. One runner per root is enforced by a lock file.
- **Isolated writes, snapshot reads.** An agent writes only in its worktree. It sees others' unfinished work as read-only snapshots, and the system logs what was actually read. Soft dependencies come from observation, not from what agents claim.
- **Disposable means disposable.** Agent processes get SIGKILL if the runner dies (Linux). A restarted runner records still-active sessions as orphaned and keeps their last state for the next attempt.
- **`main/` is a view, not the truth.** Anything that changes `main/` outside the system becomes an `external` delta and is evaluated like any other.
- **Evaluate exactly the future head.** The candidate is `merge(base, head, result)`. That exact tree is evaluated, and integration is a compare-and-swap.
- **Conflicts don't block.** A conflict becomes a resolution goal whose prompt carries both intents.
- **Evaluator separated from implementer.**
  - Protected paths are enforced at permission time and again at integration.
  - Hidden checks are invisible to agents, and so is their output.
  - Permissions are answered with one-shot options only: a persistent "allow always" would outlive the disposable agent.
- **Flags.** A delta that built on unfinished, lost, rejected or conflicted work is flagged, including retroactively. Building on an earlier attempt at the same goal is the designed path and is not flagged.

## Evidence

1. **Use cases end to end:** 22 scenarios in `tests/test_scenarios.py`, with real agent processes speaking ACP, real tests and a hidden holdout. 15 consecutive runs of the suite all passed.
2. **Simulation:** 300 random schedules × 600 ticks, and all 11 invariants held.
   - Load: 53,752 events, 7,183 integrations (each re-derived by the audit), 7,288 human edits to `main/`, 5,363 orchestrator kills and restarts, 3,459 backward clock jumps, 4,350 lines copied from peers' work, and 1,625 attempts to write into a peer view.
   - Outcomes: 1,832 rejections at evaluation, 296 protected-path edits rejected, and 2,110 conflicts, none of which blocked the queue.
3. **Mutation testing:** 19 deliberate bugs, all 19 caught. Each test run happens in a fresh interpreter, so one mutant cannot leak into the next.
   - 9 in the core. One of them evaluates the agent's proposal instead of the merged head: the simulation found two deltas, each green on its own, that produced a red head together.
   - 10 in the ACP/runner layer, for example: an allow-always answer, inotify off, hidden-check output leaked, no history in the prompt, no orphan recovery, unanswered unknown requests, build droppings captured.
   - Mutation testing also found a real bug: cleanup could hang when a run was cancelled while an agent was still alive. It is fixed, with a regression test.
4. **Differential test against git** (`git merge-file` as oracle, real merge history, files changed on both sides):

   | repo | files | same result as git | without compaction |
   |---|---|---|---|
   | psf/requests | 446 | 445 (99.8%) | 436 |
   | pallets/flask | 765 | 764 (99.9%) | 755 |
   | scrapy/scrapy | 1361 | 1356 (99.6%) | 1327 |

   Of the 7 remaining differences, in 4 the continuity result equals the file the humans eventually committed.
5. **Unit and property tests:** 67 tests in total: the 22 scenarios plus
   - adapter units: permission policy (including the `/work/s5` vs `/work/s50` prefix trap and symlink escapes), option choice, transport, inotify, sandbox argv, ignore rules
   - Myers optimality and the diff3 laws
   - log tamper detection, torn tails and two writers on one log
   - a git oracle for ambiguous merges
   - orchestrator scenarios on a real Python repo, including a semantic conflict (both sides green alone, broken together)

## Running the checks

```
python3 -m unittest discover -s tests       # all tests, ~20 s
python3 run_sim.py 300 600                  # simulation
python3 mutants.py [core|adapter]           # do the tests have teeth
python3 tools/git_differential.py <bare-repo>
```

## Known limits

- **Not run against real Kiro here.** No Kiro or bubblewrap was available in the build environment. Everything Kiro-specific follows the documented V3 protocol, and [docs/kiro-v3.md](docs/kiro-v3.md) lists six things to confirm on the first run.
- **Observation completeness.** inotify is Linux-only. On macOS only ACP-level signals record reads.
- **Truncation.** Dropping the end of the log leaves a valid chain. Detecting it needs an anchor published elsewhere (`verify_chain(anchor=...)`).
- **`main/` race.** An edit to `main/` between snapshot and sync can be lost.
- **Flaky tests.** Evaluations are cached by tree hash, so flaky checks need separate handling.
- **Merge.** Merging is line-based (diff3). It differs from git on about 2% of highly duplicate-heavy inputs.

## Next

1. A first real run with Kiro V3: confirm the six open points.
2. Replay a real repo's history as sessions, with git branches as the baseline. Measure conflicts, throughput and manual resolutions.
3. An AST merge alongside diff3, measured with the same differential test.
4. Budgets in credits, from `session_info_update` `turn_completion`.
