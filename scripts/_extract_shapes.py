"""Print the shape of every graph listed in a LangGraph app's langgraph.json, as JSON.

Run by scripts/history.py inside the app's own environment, once per commit, in a fresh
interpreter so each commit's modules load from scratch.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

DUMMY_KEYS = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "TAVILY_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "EXA_API_KEY",
    "PERPLEXITY_API_KEY",
    "FIREWORKS_API_KEY",
    "MISTRAL_API_KEY",
    "COHERE_API_KEY",
]


def module_for(path: Path) -> tuple[Path, str]:
    """The sys.path root and dotted module name for a source file inside (or outside) a package."""
    parts = [path.stem]
    parent = path.parent
    while (parent / "__init__.py").exists():
        parts.insert(0, parent.name)
        parent = parent.parent
    return parent, ".".join(parts)


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    for key in DUMMY_KEYS:
        os.environ.setdefault(key, "sk-graphlock-dummy")
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    config = json.loads((root / "langgraph.json").read_text())
    import graphlock

    out: dict[str, object] = {}
    for name, spec in config.get("graphs", {}).items():
        file_part, _, attr = spec.partition(":")
        try:
            path = (root / file_part).resolve()
            base, module_name = module_for(path)
            for p in (str(base), str(root), str(root / "src")):
                if p not in sys.path:
                    sys.path.insert(0, p)
            obj = importlib.import_module(module_name)
            for part in attr.split("."):
                obj = getattr(obj, part)
            if callable(obj) and not hasattr(obj, "builder") and not hasattr(obj, "compile"):
                obj = obj()
            out[name] = graphlock.extract_shape(obj)
        except Exception as exc:
            out[name] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
