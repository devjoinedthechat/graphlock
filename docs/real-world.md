# graphlock on real LangGraph apps

The corpus and the property tests use graphs written to exercise graphlock. This page covers seven
public LangGraph apps that weren't: every change to their graphs in their git history, checked the
way `graphlock check` would have checked it in a pull request, and every finding reviewed against
the diff.

## Method

[`scripts/history.py`](../scripts/history.py) replays one app:

```sh
uv run python scripts/history.py https://github.com/langchain-ai/open_deep_research --workdir /tmp/gl
```

- It walks the mainline (`git log --first-parent`), oldest first, and keeps commits that touch Python
  or `langgraph.json`. Merged branches count as the single change they made to the mainline.
- At each commit it reads `langgraph.json` to find the graphs, and extracts their shapes in a fresh
  interpreter. That interpreter runs in an environment built from the app's *current* dependencies,
  with dummy API keys so model clients can be constructed.
- It runs `check` between consecutive shapes of each graph and records the findings, with a
  structural diff of what changed.

A commit that no longer imports with today's dependencies is skipped. The next change then spans
both commits. Three of the apps have long stretches like that: data-enrichment is a year of history
between its only two importable commits.

## Results

Run on 2026-09-19 with LangGraph 1.2.11.

| App | Mainline commits | Import today | Graph changes | Blocked | Warnings only | Info only | Nothing to report |
|---|---|---|---|---|---|---|---|
| [open_deep_research](https://github.com/langchain-ai/open_deep_research) | 67 | 65 | 31 | 3 | 2 | 8 | 18 |
| [local-deep-researcher](https://github.com/langchain-ai/local-deep-researcher) | 25 | 22 | 14 | 0 | 0 | 0 | 14 |
| [company-researcher](https://github.com/langchain-ai/company-researcher) | 23 | 12 | 5 | 0 | 0 | 1 | 4 |
| [react-agent](https://github.com/langchain-ai/react-agent) | 15 | 10 | 4 | 0 | 0 | 0 | 4 |
| [retrieval-agent-template](https://github.com/langchain-ai/retrieval-agent-template) | 14 | 2 | 0 | 0 | 0 | 0 | 0 |
| [data-enrichment](https://github.com/langchain-ai/data-enrichment) | 13 | 2 | 1 | 1 | 0 | 0 | 0 |
| [memory-agent](https://github.com/langchain-ai/memory-agent) | 7 | 7 | 4 | 0 | 0 | 0 | 4 |
| **Total** | **164** | **120** | **59** | **4** | **2** | **9** | **44** |

[executive-ai-assistant](https://github.com/langchain-ai/executive-ai-assistant) was left out: it
depends on LangGraph 0.5, older than graphlock supports.

## The four blocked changes

All four would have broken stored threads.

| Commit | What changed | Rule | Verdict |
|---|---|---|---|
| open_deep_research [`31d8ea3`](https://github.com/langchain-ai/open_deep_research/commit/31d8ea3454) | The package `report_masitro` became `src.report_maistro`. `Section` and `SearchQuery`, stored in state as Pydantic objects, moved with it | GL301 | Real. Stored objects restore as plain dicts where the nodes expect `Section` |
| open_deep_research [`2e32b95`](https://github.com/langchain-ai/open_deep_research/commit/2e32b95985) | The same classes moved from `src.open_deep_research.state` to `open_deep_research.state` when the project was restructured (PR #14) | GL301 | Real wherever the old `src.` path no longer imports, as in the environment used here |
| open_deep_research [`6035b16`](https://github.com/langchain-ai/open_deep_research/commit/6035b16ee9) | `feedback_on_report_plan` went from `str` to `Annotated[list[str], operator.add]` | GL204 | Real. The next write merges the stored string with a list: `TypeError: can only concatenate str (not "list") to str`. It's now the corpus scenario `type-and-reducer-changed` |
| data-enrichment [`ca06154`](https://github.com/langchain-ai/data-enrichment/commit/ca06154f54) | A rewrite, spanning 13 commits: `call_model` became `call_agent_model`, and the state became a dataclass with required `topic` and `extraction_schema` | GL101, GL201 | Real. A thread paused before `call_model` stops, and one without the new fields fails validation |

## Warnings and information

- **Warnings (14, on 6 changes).**
  - Eight restate the GL301 class moves as type changes.
  - Two are `supervisor_messages` changing reducer from `operator.add` to a custom
    `override_reducer`. Stored threads merge differently from the next write on, which is a real
    change in behaviour, and a warning is the right level.
  - Four are other type changes on TypedDict or dataclass fields, whose stored values reach nodes
    unchanged.
- **Information (12).**
  - Six are fields removed from graphs without `interrupt_before` breakpoints, where the value is
    simply no longer read.
  - Five are code changes in the `human_feedback` node, which calls `interrupt()`.
  - One is `bool` widened to `None | bool`.

## The 44 changes with nothing to report

Each was categorised from its structural diff:
- **41** changed the code of nodes that don't call `interrupt()`. Several of those also counted in
  the next item.
- **13** changed only a subgraph's internals, which `check` examined as well.
- **2** added fields.
- **2** moved the state class to another module. Checkpoints store each field separately, never the
  class, so nothing breaks.
- **1** turned a TypedDict state into a dataclass, with every field optional.

None is a kind of change the corpus shows breaking a paused thread.

## What the review changed in graphlock

- **Four false positives, now fixed.** The first run blocked four changes as GL401 where only the
  wording of an `interrupt()` prompt had changed. Answers are passed by position, so rewording
  misroutes nothing. GL401 now tells a *moved* call (breaking) from *reworded* prompts (GL402,
  information) and calls *removed* from the end (a warning). One of the four only existed because
  the first version of the replay compared commits across merged branches; it now follows the
  mainline. A fifth GL401, where the `interrupt()` moved out of the node into a new
  `human_feedback` node, is now a warning: a thread paused there runs the node again without asking,
  and is asked again in `human_feedback`.
- **One miss, now fixed.** The `feedback_on_report_plan` change was only a warning, although it
  crashes. A type change together with a new reducer is now breaking, `scan` tries each stored value
  against the new reducer, and the corpus pins it.
- **Noise, now fixed.** A LangChain tool used as a type annotation was printed through its `repr`.
  It is now named by its class and tool name.

## Limits

- This covers seven apps, most of them templates or research agents. open_deep_research is the
  only one with a human-in-the-loop step.
- Commits that don't import with today's dependencies are skipped, so some changes span several
  commits.
- The environment is today's dependencies, not the ones each commit shipped with. A shape can
  differ from what that commit produced in production if a library changed how it builds graphs.
