# Contributing to sql2sqlx

Thanks for helping! The project optimizes for **correctness first**:
untouched SQL must be preserved byte-for-byte, and any conversion that
cannot be proven safe must fall back to a verbatim `operations` action
with a warning - never a guess.

## Development setup

```bash
git clone https://github.com/neuralsorcerer/sql2sqlx
cd sql2sqlx
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,docs]"
```

## Running checks

```bash
pytest            # full test suite
black --check .    # formatting
isort --check-only . # import ordering
mypy              # strict public-package type check
cd docs && make html   # build documentation
```

## Ground rules

1. **Every function needs a Google-style docstring** (Args/Returns/
   Raises). The docs build pulls these via autodoc.
2. **No new runtime dependencies.** The package is deliberately
   stdlib-only.
3. **Add a test for every behavior change**, including at least one
   adversarial input (semicolons in strings, aliases shadowing tables,
   and so on).
4. Fallback paths must add a stable warning `code` - see
   `docs/conversion_rules.md` for the registry.
5. Keep output deterministic: sorted files, stable naming, canonical
   config key order.
