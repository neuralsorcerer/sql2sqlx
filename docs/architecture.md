# Architecture

sql2sqlx is a three-phase pipeline built on one invariant:

> **Untouched SQL is preserved character-for-character after decoding.** Output bodies are
> produced by applying *span edits* to slices of the original text -
> tokens are never re-serialized, so formatting, casing and inline
> comments survive exactly.

```text
.sql files
  |
  v
Phase 1: parse each file, in parallel when possible
  - lexer: tokens and comment spans
  - splitter: top-level statements
  - classifier: action drafts and metadata
  - reference scanner: candidate table read sites
  |
  v
Phase 2: link corpus metadata
  - creators and duplicate demotion
  - writer chains and ordering dependencies
  - hasOutput election
  - ref resolution and declaration synthesis
  - action names and output paths
  |
  v
Phase 3: emit SQLX
  - config blocks
  - source-span edits
  - provenance and original comments
  - .sqlx files
```

## Phase 1 - lexing, splitting, classifying

- **Lexer** (`sql2sqlx.lexer`): a single compiled regex alternation
  (CPython's C engine, strictly linear - no backtracking) implementing
  the GoogleSQL lexical spec: all string/bytes/raw forms, triple quotes,
  backtick identifiers with escapes, `--`/`#`/`/* */` comments
  (non-nested, per spec), parameters and every operator. Unterminated
  constructs raise `LexError` with exact line/column. Comment spans are
  captured in the same pass so no file is ever lexed twice.
- **Splitter** (`sql2sqlx.splitter`): splits on `;` only at parenthesis
  depth 0 with an empty *block frame stack*. Frames track BigQuery
  scripting (`BEGIN..END`, `IF..END IF`, loops, `CASE`), recognizing
  openers only in statement position; a bounded lookahead separates the
  scripting `IF c THEN` from the `IF(a,b,c)` function; `BEGIN
  [TRANSACTION];` is not a block.
- **Classifier** (`sql2sqlx.parser`): maps each statement to a draft
  action, extracts targets/metadata, performs the select-list aliasing
  analysis, and proves (or refuses) the MERGE shape while reporting its
  target-schema precondition.
- **Reference scanner** (`sql2sqlx.refs`): context-tracked scan for
  table paths after `FROM`/`JOIN`/from-list commas/`MERGE ... USING`,
  with per-query CTE and range-variable scopes. It models recursive versus
  ordered CTE visibility, correlated scalar versus non-lateral FROM
  subqueries, set-operation branches, implicit aliases and `UNNEST` aliases;
  `EXTRACT(x FROM col)` remains inert. Dashed-project adjacency and target
  skip-spans (`DELETE FROM x`) are handled without re-serializing SQL.

Files using transactions, temporary objects, variables, procedural control
flow or dynamic side effects collapse into a single whole-file `operations` draft. A
conservative nested scan still harvests persistent reference sites and
write targets for dependency wiring without leaking temporary names.

## Phase 2 - the linker

Runs once over lightweight metadata (parallel workers ship back
picklable drafts):

1. one **creator** per resolved target; later duplicates are demoted to
   verbatim operations;
2. **writer chains** per table in corpus order (sorted relative path,
   then statement position); each writer depends on its predecessor, and a
   same-file writer also waits for intervening readers;
3. **`hasOutput` election** only for ownerless operations that actually
   create the configured output;
4. **reference resolution** against defaults
   (`--default-project/-dataset`), rewriting sites to `${ref(...)}` and
   adding reader dependencies on each table's latest preceding writer;
5. future-owner protection that leaves an earlier read literal rather than
   reversing corpus order, plus cycle detection that leaves an unsafe edge
   literal rather than
   emitting an uncompilable Dataform graph;
6. unique action naming and collision-free output paths (deterministic:
   two runs produce identical bytes).

## Phase 3 - emission

`sql2sqlx.emitter` renders the canonical `config { ... }` block, applies
the accumulated span edits, emits literal `${` text through constant SQLX
placeholders (inserted `${ref()}`/`${self()}` stay active), and attaches
provenance comments plus the statement's original comments.

## Performance model

Lexing dominates. The regex master pattern runs at C speed; directory
conversion parallelizes phase 1 with a process pool (`--jobs`).
Measured on this package's benchmark
(`examples/06_benchmark.py`, 120 files / 1,056,600 lines / 31.7 MB,
single core): **43.8 s ~ 24,100 lines/s**, with ~96k actions emitted and
~96k references rewritten. Memory is proportional to corpus size
(original text is retained for span-exact emission).
