# Security

graphlock reads your graph's code and, for `scan`, your checkpointer. `check` and `lock` open no
connections. `scan` only reads. The only thing that writes to a checkpointer is
`with_migrations`, and only through the checkpointer's own API, as part of a checkpoint LangGraph
was already writing.

## What counts as a vulnerability

- **Code execution reachable from stored data.** graphlock decodes checkpoint bytes to find
  classes stored by import path. It must never import or call anything because a checkpoint names
  it.
- **A migration or scan that writes, deletes or corrupts stored threads** it wasn't asked to
  change.
- **A silent miss.** If a change breaks paused threads without an error and `check` or `scan`
  reports nothing, treat it as a security issue and report it the same way. Someone will deploy on
  that green check.
- A supply-chain problem in how graphlock is built or published.

## Reporting

Open a [private security advisory](https://github.com/devjoinedthechat/graphlock/security/advisories/new).
Please don't open a public issue first. You can expect an acknowledgement within a week.

A change graphlock misses that *does* raise an error is an ordinary bug, and a public issue is the
right place for it.

## Supported versions

Pre-1.0: only the latest commit on `main` is supported.
