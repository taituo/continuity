# What this is for: scenarios

You define goals and constraints. Disposable agents do the work, an evaluator judges it, and the integrator merges it. The log remembers everything.

Each scenario below is an end-to-end test in `tests/test_scenarios.py`. The tests drive real agent processes speaking ACP through the runner, against a real Python project with visible tests and hidden holdout checks. The agent is the scripted fake from `continuity/testing/fake_agent.py`, which uses the Kiro V3 wire protocol. Swapping in `kiro-cli acp` changes who decides what to write, not the mechanics.

## Daily use

### 1. A backlog worked in parallel, even in the same file
*Test: `ParallelWork.test_two_features_in_one_file_at_the_same_time`*

You add goals "add sub()" and "add power()". Two agents start from the same head and both edit `calc.py`. Both deltas merge cleanly, the merged result is evaluated, and both land. `status` shows both goals done.

### 2. Long-lived goals as containers
*Test: `ParallelWork.test_durable_goal_is_worked_through_its_children`*

"Maintain calc" is `--durable`. It is never worked on directly and never finishes. Its children are the work. Each agent's prompt shows the ancestry ("part of g3: maintain calc"). To keep a service maintained, a scheduled job adds children such as "upgrade dependencies" or "fix the flaky test X".

### 3. Competing variants, first good one wins
*Tests: `Variants.*`*

`--variants 2` runs two attempts at the same goal at once. The first that passes is integrated. The other is cancelled mid-turn (`superseded`), or rejected as superseded if it already finished. You pay for exploration only until something works.

### 4. Adding goals while it runs
*Test: `SharedWorld.test_goals_added_from_a_shell_while_it_runs`*

`continuity goal ROOT "…"` from another shell while the runner works. The log takes both writers (appends catch up under a lock, so the chain never forks), and the runner picks the new goal up on its next scheduling pass.

## When things collide

### 5. A conflict becomes a goal, not a blocker
*Test: `Conflicts.test_conflict_becomes_a_resolution_goal_with_full_context`*

One agent adds type hints to `mul` while another renames its parameters. The second can't be merged. Instead of blocking the queue:
- A resolution goal opens under the second one's goal.
- The resolver's prompt names the conflicting file and both intents ("collided with s6 (for: add type hints to mul), whose intent was: Typed mul.").
- The resolver can read the conflicted version and its diff.

Once the resolution lands, the original goal counts as done too.

### 6. Someone edits the code while agents work
*Test: `SharedWorld.test_human_edit_during_a_run_is_captured_and_kept`*

A human edits `main/calc.py` mid-run. The edit is captured as an `external` delta, evaluated, and integrated first. The agent's change merges on top, and `main/` ends up equal to the head with both changes. Nothing is overwritten silently.

### 7. Building on someone's unfinished work is remembered
*Test: `SharedWorld.test_building_on_unfinished_peer_work_is_recorded_and_flagged`*

Agent B reads agent A's in-progress snapshot with plain `cat`, which sends no ACP signal at all. inotify records the read. B lands first and gets flagged `dependency_unfinished`. Later A is rejected, and B gets `dependency_rejected` as well. A reviewer can find exactly the integrated work that rests on failed work.

## When agents fail

### 8. Crash: the next agent continues the work
*Test: `Continuity.test_crashed_agent_is_continued_by_the_next_one`*

An agent writes half of `mod()` and dies. Its last checkpoint stays in the log. The next agent's prompt says the previous attempt "did not finish (crashed); it had changed calc.py" and points to that work. The new agent picks it up and finishes. Continuing your own goal's earlier attempt is not flagged.

### 9. Partial work is fine
*Tests: `Continuity.test_partial_work_lands_and_the_goal_stays_open`, `test_max_turn_requests_counts_as_partial`*

An agent does the valuable piece and ends with `REMAINING: power()`. A turn that hits `max_turn_requests` counts the same way. The piece is integrated, the goal stays open, and the next agent starts from it with the remaining part in its prompt.

### 10. The runner itself dies
*Test: `Continuity.test_runner_killed_mid_session_resumes_cleanly`*

`kill -9` the runner while an agent works. The agent dies with it (Linux parent-death signal), even an agent that ignores its stdin closing, so nothing keeps writing for a session nobody tracks. On the next start:
- The session still marked active is recorded as orphaned, and its checkpoint is kept.
- The goal is retried, and the new prompt says what happened.
- A lock file prevents two runners on one root (*`Failures.test_two_runners_cannot_share_a_root`*).

### 11. Stuck, refusing, or unable to start
*Tests: `Failures.*`*

- A stuck agent gets `session/cancel`. One that ignores it is killed with its process group.
- A refusal is recorded as lost, never proposed.
- If the agent can't start at all (missing binary, or with Kiro a missing login), the run stops instead of burning every goal's budget.

## Judging honestly

### 12. Overfitting is caught without leaking the hidden checks
*Test: `Judgement.test_overfitting_is_caught_by_hidden_checks_without_leaking_them`*

An agent "simplifies" `div` so that the visible tests pass but the hidden zero-division check fails. The change is rejected. The retry is told "hidden checks failed (details withheld)" and nothing more. The hidden test's name appears nowhere in the prompt or in the log.

### 13. Editing the tests to pass is refused twice
*Test: `Judgement.test_editing_the_tests_is_refused_twice`*

- An attempt to edit `tests/` with the file tool is refused at permission time; the agent sees `failureReason: denied`.
- The same edit through `sed` in a shell reaches the worktree, but the delta is rejected at integration as `protected:tests/test_calc.py`.

### 14. Hopeless goals cost a bounded amount
*Test: `Judgement.test_budget_bounds_a_hopeless_goal`*

Each goal gets a number of attempts (`--budget`, or per goal). When they're used up, the goal stays open in `status` for a human, and the runner moves on.

## Containment

### 15. An agent cannot touch other sessions or the system
*Test: `SharedWorld.test_agent_cannot_touch_other_sessions_or_the_system`*

These are refused, through permissions and through the client `fs/*` calls alike:
- writes to `main/` or outside the worktree
- reads of `.continuity/log.jsonl` or the holdout

The agent keeps working, and its legitimate change lands. With `--sandbox` those paths don't exist inside the agent's process at all.

### 16. Protocol robustness
*Tests: `Protocol.*`*

The adapter handles all of these, and the work still lands:
- CLI v2 style messages (`session/notification`, PascalCase kinds)
- a non-JSON line on stdout
- an unknown agent→client request (answered with `-32601`)
- writes through client `fs/write_text_file`
- usage info

The fake agent refuses any `session/new` that carries MCP servers, so every passing scenario also shows that none are ever sent.

## Reading the results

```sh
python3 -m continuity status  ROOT        # goal tree: done / open / durable, attempts used
python3 -m continuity history ROOT g7     # each attempt: stop reason, outcome, flags, intent
python3 -m continuity audit   ROOT        # re-derive every integration from the log
```
