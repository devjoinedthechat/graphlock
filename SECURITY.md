# Security

## What graphlock touches

- **Your code.** `lock`, `check`, `scan` and `show` import the graphs named in `pyproject.toml`
  or `--graph`, and the migrations list. That runs your modules' import-time code, as pytest
  would. `check` also looks up the classes recorded in `graphlock.json`, which is part of your
  repository and trusted like the code.
- **Your checkpointer, read-only.** `scan` loads checkpoints through the checkpointer's own
  `get_tuple` and `list`, using its own serializer, and writes nothing.
- **Your checkpointer, through LangGraph.** `with_migrations` changes what LangGraph reads. It
  writes only when LangGraph writes a checkpoint, and then adds the repaired channels to that
  write.

## Stored data can name modules

Checkpoints store objects by import path. With LangGraph's default serializer, **loading a
checkpoint imports the modules it names**, both when your app resumes a thread and when `scan`
reads it. graphlock adds no imports of its own: it only checks classes that LangGraph has already
loaded, so scanning a store is no more dangerous than resuming its threads.
[tests/test_security.py](tests/test_security.py) holds it to that.

If anyone other than your application can write to the checkpoint store, set
`LANGGRAPH_STRICT_MSGPACK=true`, or give your serializer an `allowed_msgpack_modules` allowlist.
LangGraph then refuses to import unlisted modules, `scan` reads with the same restriction, and a
blocked object is reported as GL301.

## What counts as a vulnerability

- **graphlock importing or calling something because stored data names it.**
- **A migration or scan that writes, deletes or corrupts stored threads** it wasn't asked to
  change.
- **A silent miss.** If a change breaks paused threads without an error and `check` or `scan`
  reports nothing, report it the same way. Someone will deploy on that green check.
- A supply-chain problem in how graphlock is built or published.

## Reporting

Open a [private security advisory](https://github.com/devjoinedthechat/graphlock/security/advisories/new).
Please don't open a public issue first. You can expect an acknowledgement within a week.

A change graphlock misses that *does* raise an error is an ordinary bug, and a public issue is the
right place for it.

## Supported versions

Pre-1.0: only the latest commit on `main` is supported.
