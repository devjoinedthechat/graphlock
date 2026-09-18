# Refunds: a deploy that would strand waiting threads

A refund agent drafts a refund, waits for a manager in `interrupt()`, then issues it.
[refunds_v1.py](refunds_v1.py) is what is deployed. [refunds_v2.py](refunds_v2.py) is a pull
request that renames the approval step and adds a required `currency` field.
[migrations.py](migrations.py) repairs the threads that change would break.

Run it from this folder, in an environment with graphlock and `langgraph-checkpoint-sqlite`:

```sh
python seed.py                                   # five refunds; three wait for a manager
graphlock lock --graph refunds=refunds_v1:graph  # the deployed shape -> graphlock.json

graphlock check --no-migrations                  # exit 1: GL101 and GL201
graphlock scan --sqlite refunds.sqlite --no-migrations   # refund-1, refund-3 and refund-4 would break

graphlock check                                  # exit 0: both repaired by migrations.py
graphlock scan --sqlite refunds.sqlite
python resume.py                                 # a manager approves refund-1 under v2
```

`resume.py` prints:

```
drafted refund of 40 for A-101
manager said yes
issued 40 USD
```

Without `with_migrations`, the same approval returns without an error, and nothing after
`drafted refund` ever happens.

`pyproject.toml` here holds only the `[tool.graphlock]` settings: this folder is an example, not a
package. [tests/test_cli.py](../../tests/test_cli.py) runs this whole flow on every commit.
