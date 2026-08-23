# Architecture

sql2sqlx is a three-phase pipeline built on one invariant:

> **Untouched SQL is preserved character-for-character after decoding.** Output
> bodies are produced by applying *span edits* to slices of the original text -
> tokens are never re-serialized, so formatting, casing and inline comments
> survive exactly.

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

The phases map onto the module layout: `lexer`, `splitter`, `parser` and
`refs` do Phase 1; `converter._Linker` does Phase 2; `emitter` does Phase 3.
The shared, picklable data model lives in `model` so parsed results can cross
process boundaries.

## Phase 1 - lexing, splitting, classifying

This phase is **per file** and has no cross-file dependencies, which is what
makes it parallelizable.

### Lexer (`sql2sqlx.lexer`)

A single compiled regex alternation, executed by CPython's C regex engine,
implements the GoogleSQL lexical specification. Every alternative is written to
match in strictly linear time (no ambiguous nested quantifiers, hence no
catastrophic backtracking), giving high throughput while remaining exactly
character-accurate. It recognizes:

- line comments (`--` and `#`) and non-nested block comments (`/* ... */`,
  ending at the first `*/`, exactly as GoogleSQL defines them);
- all string/bytes/raw literal forms, including triple-quoted strings and
  every valid `r`/`b` prefix combination and case, with GoogleSQL's exact
  backslash-escape rules (a raw literal can never end in an odd number of
  backslashes, and so on);
- backtick-quoted identifiers with escapes and embedded dots;
- numeric literals, named/positional query parameters and system variables,
  and every GoogleSQL operator and punctuation token.

Tokens carry `(kind, text, start, end)` with character offsets into the
original text - the anchor for span-exact emission later. Unterminated strings,
backtick identifiers and block comments raise a `LexError` with an exact
1-based line and column. Comment spans are captured in the **same pass** (via
an output list), so no file is ever lexed twice.

### Splitter (`sql2sqlx.splitter`)

Splits the token stream into top-level statements on `;`, but only at
parenthesis depth 0 **and** with an empty *block-frame stack*. Frames model
BigQuery scripting constructs - `BEGIN ... END`, `IF ... END IF`, loops, and
`CASE` - and openers are recognized only in statement position. A bounded
lookahead distinguishes the scripting `IF cond THEN` from the `IF(a, b, c)`
function, and `BEGIN [TRANSACTION];` is treated as a statement, not a block
opener. The splitter is deliberately lenient: a mismatched `END` is tolerated
rather than fatal, so odd-but-valid scripts still split sensibly.

### Classifier (`sql2sqlx.parser`)

Maps each statement to an `ActionDraft`: its action type, target table,
already-known `config` entries, the metadata extracted from
`PARTITION BY`/`CLUSTER BY`/`OPTIONS`, and any classification warnings. This is
where the opt-in analyses live:

- the select-list aliasing analysis for `INSERT -> incremental` (proving each
  output name is exactly determinable, or refusing);
- the MERGE shape proof (all six obligations) plus its target-schema
  precondition warning;
- detection of the forms that must fall back (`LIKE`/`CLONE`/`COPY`, column-list
  CTAS, temp/external/snapshot tables, guarded creates, `INSERT ... VALUES`).

### Reference scanner (`sql2sqlx.refs`)

A context-tracked scan for physical table paths in read positions - after
`FROM`, `JOIN`, from-list commas and `MERGE ... USING` - carrying per-query
CTE and range-variable scopes. It models recursive versus ordered CTE
visibility, correlated scalar subqueries versus non-lateral `FROM` subqueries,
set-operation branches, implicit aliases and `UNNEST` aliases; `EXTRACT(x FROM
col)` stays inert, and dashed-project adjacency and target skip-spans (e.g. the
target of `DELETE FROM x`) are handled without ever re-serializing SQL. The
output is a list of candidate `RefSite`s (character spans plus the decoded,
still-unresolved `TableName`).

### Whole-file scripts

Files that use transactions, temporary objects, variables, procedural control
flow, or dynamic side effects cannot be safely split into independent Dataform
actions without changing control-flow or scope semantics, so they collapse
into a **single whole-file `operations` draft**. A conservative nested scan
still harvests persistent reference sites and write targets from inside the
script, so dependency wiring works, while temporary names are kept from
leaking into the corpus.

## Phase 2 - the linker

`_Linker` runs once, single-process, over the lightweight drafts that Phase 1
produced (parallel workers ship back picklable `ParsedFile`/`ActionDraft`
objects). In order:

1. one **creator** per resolved target `(project, dataset, table)`; a second
   producer of the same target is demoted to a verbatim `operations` action
   (`DUPLICATE_TARGET`), because Dataform allows exactly one owner per target;
2. **writer chains** per table in corpus order (sorted relative path, then
   statement position): every writer depends on its predecessor, and a
   same-file writer also waits for intervening readers, so
   `DROP x; CREATE x; UPDATE x` keeps its order;
3. **`hasOutput` election**: an ownerless `operations` action that actually
   creates its configured output (and only such an action - never mutating
   DML) is elected as a producer, gaining `hasOutput: true` and a `${self()}`
   rewrite of its own target path;
4. **reference resolution** against the `--default-project/--default-dataset`
   values, rewriting resolved read sites to `${ref(...)}` and adding reader
   dependencies on each table's latest preceding writer;
5. **future-owner protection** that leaves an earlier read literal
   (`FUTURE_CREATOR`) rather than reversing corpus order, plus **cycle
   detection** that omits an unsafe edge (`DEPENDENCY_CYCLE`) rather than
   emitting an uncompilable graph;
6. unique action naming and collision-free output paths - deterministic, so
   two runs produce identical bytes.

See the [dependency model](dependencies.md) for the full semantics of each
edge and the warnings they raise.

## Phase 3 - emission

`sql2sqlx.emitter` renders each linked draft to a `.sqlx` file: it builds the
canonical `config { ... }` block (fixed key order), applies the accumulated
span edits to a slice of the original source text, emits literal `${` and
SQLX-unsafe comments through constant placeholders (while inserted
`${ref()}`/`${self()}` stay active), strips one trailing semicolon, and
attaches the provenance comment plus the statement's own leading comments. The
result is UTF-8 and is written without newline translation, so the newlines
sql2sqlx itself emits are `\n` while newlines inside preserved source spans
survive as they were - a CRLF source keeps CRLF inside its body. See [core
concepts](concepts.md) for the output anatomy.

## Performance model

Phase 1 is the larger share of runtime and is the part that parallelizes:
directory conversion spreads it across a `ProcessPoolExecutor` sized by
`--jobs` (`0` = one worker per CPU). Within Phase 1 no single stage dominates -
profiling the benchmark corpus puts lexing and statement classification at
roughly a third of total runtime each (reference scanning being the bulk of
classification), and splitting well under a tenth. Phases 2 and 3 are a
single-process metadata pass and account for the remaining quarter or so,
nearly all of it emission. That serial share is what bounds the parallel
speedup rather than it growing with core count indefinitely - measured at
about 2x on four cores, which is what Amdahl's law predicts from this split.
Both shares shift with corpus shape, so profile your own if it matters.

Measured on this package's benchmark (`examples/06_benchmark.py`, 120 files /
1,056,600 lines / 31.7 MB, single core): **43.8 s ~ 24,100 lines/s**, with
~96k actions emitted and ~96k references rewritten. Memory is proportional to
corpus size, because the original text of each file is retained for span-exact
emission.
