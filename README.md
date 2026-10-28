# continuity

An agent-agnostic core for an autonomous development system. Agents are disposable. The source of truth is an append-only, hash-chained delta log, and system state is a fold over that log. Pure Python stdlib, zero LLM calls: the core is proven with fake agents.

## Parts

| file | what |
|---|---|
| `store.py` | content-addressed blobs and trees (sha256) |
| `hlc.py` | Hybrid Logical Clock (Kulkarni et al. 2014); only the orchestrator issues timestamps |
| `log.py` | hash-chained JSONL log, torn-tail recovery, anchor for truncation detection |
| `state.py` | `fold(log)`, strict: an illegal transition fails even if every hash was recomputed |
| `merge.py` | Myers diff, git-style hunk compaction, diff3, conflicts as data |
| `orchestrator.py` | sessions, peek, drift, integrator with compare-and-swap |
| `evaluators.py` | command-based evaluator; the holdout never enters any tree |
| `audit.py` | independent audit from the log and object store alone |
| `sim.py` | deterministic simulation and 11 invariants |

## Design decisions

- **No hidden state.** The orchestrator applies every event with the same `apply` function that replay uses. An event is validated before it is written to disk.
- **Isolated writes, snapshot reads.** An agent writes only to its own `work/<s>/`. It sees others' unfinished work only through `peek`, which copies a checkpoint and **logs the look**. Soft dependencies come from observation, not from what agents claim.
- **`main/` is a view, not the truth.** If `main/` doesn't match head, the difference is logged as an `external` delta that jumps the queue and is evaluated like any other. If it is rejected, the change stays in the store but is removed from `main/`.
- **Evaluate exactly the future head.** The candidate is `merge(base, head, result)`. That exact tree is evaluated, and integration is a compare-and-swap: if head moved during evaluation, it retries.
- **Conflicts don't block.** A conflict is recorded and opens a resolution goal. The queue moves on.
- **Evaluator separated from implementer.** Protected paths (`tests/`) can only be changed by a privileged goal. The holdout is copied into the candidate only inside the evaluator's own temp directory.
- **Flags.** A delta that looked at unfinished, lost, rejected or conflicted work gets flagged, including retroactively after it was integrated.

## Evidence

1. **Simulation:** 300 random schedules × 600 ticks, and all 11 invariants held.
   - Load: 54,164 events, 7,186 integrations (each re-derived by the audit), 7,312 human edits to `main/`, 5,358 orchestrator kills and restarts, 3,471 backward clock jumps, 4,340 lines copied from peers' work, and 1,615 attempts to write into a peer view.
   - Outcomes: 1,840 rejections at evaluation, 298 protected-path edits rejected, and 2,104 conflicts, none of which blocked the queue.
2. **Mutation testing:** 9 deliberate bugs, all 9 caught. One of them evaluates the agent's proposal instead of the merged head: the simulation found a case where two deltas, each green on its own, produced a red head together.
3. **Differential test against git** (`git merge-file` as oracle, real merge history, files changed on both sides):

   | repo | files | same result as git | without compaction |
   |---|---|---|---|
   | psf/requests | 446 | 445 (99.8%) | 436 |
   | pallets/flask | 765 | 764 (99.9%) | 755 |
   | scrapy/scrapy | 1361 | 1356 (99.6%) | 1327 |

   Hunk compaction was added because of this measurement. Of the 7 remaining differences, in 4 the continuity result equals the file the humans eventually committed.
4. **Unit and property tests (24):**
   - Myers produces an optimal LCS
   - diff3 laws: identity, symmetry, and both sides surviving
   - any log edit is detected, and a re-hashed forgery fails the fold
   - HLC stays monotone with a backward-jumping clock
   - scenarios on a real Python repo: parallel edits to one file, test edits rejected, holdout, semantic conflict (both sides green alone, broken together)

## Running

```
python3 -m unittest discover -s tests       # 24 tests, ~6 s
python3 run_sim.py 300 600                  # simulation
python3 mutants.py                          # do the invariants have teeth
python3 tools/git_differential.py <bare-repo>
```

## Known limits

- **Truncation.** Dropping the end of the log leaves a valid chain. Detecting it needs an anchor published elsewhere (`verify_chain(anchor=...)`).
- **`main/` race.** An edit to `main/` between snapshot and sync can be lost. Fixing it needs a lock or fs events.
- **Flaky tests.** Evaluations are cached by tree hash, so flaky tests need separate handling.
- **Isolation** is structural (copies). In production, use read-only bind mounts or COW clones.
- **Differences from git.** diff3 differs from git in about 2% of highly duplicate-heavy inputs, because git's xdiff prunes frequently repeated lines.
- **Single orchestrator.** `HLC.recv` is ready for federation but not used yet.

## Next

1. Agent adapter: Kiro or Claude Code in a container, `work/` mounted and `peek` as a tool.
2. Replay a real repo's history as sessions, with git branches as the baseline. Measure conflicts, throughput and manual resolutions.
3. AST merge alongside diff3, measured with the same differential test.
4. Intent interpreter as `annotate` events.
5. Budget in tokens, and cheating attempts with real agents.
