# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""The conversion pipeline: parse -> link -> emit.

Phase 1 (parallelizable, per file)
    :func:`parse_source` lexes, splits and classifies one file into
    :class:`~sql2sqlx.model.ActionDraft` objects. Files requiring shared
    BigQuery context (transactions, temporary objects, variables or
    procedural control flow) become a single whole-file ``operations``
    draft; persistent reference sites and write targets are still harvested.

Phase 2 (single pass over metadata)
    :class:`_Linker` builds the cross-file picture:

    * one **creator** per produced table (later duplicate creators are
      demoted back to verbatim operations - Dataform permits exactly one
      owner per target);
    * **writer chains** per table in corpus order (sorted file path, then
      statement position): every writer depends on its predecessor, so
      ``DROP x; CREATE x; UPDATE x`` keeps its original order;
    * **hasOutput election**: an ownerless operation is elected only when it
      actually creates its target, satisfying Dataform's output contract;
    * **reference rewriting**: every read of a produced table becomes
      ``${ref(...)}``; readers additionally depend on the latest preceding
      writer, and cycle-producing edges are conservatively omitted;
    * unique action naming and collision-free output paths.

Phase 3
    :mod:`sql2sqlx.emitter` renders each draft to a ``.sqlx`` file.

The public API - :func:`convert_string`, :func:`convert_file`,
:func:`convert_directory` - wraps the pipeline; directory conversion
parallelizes phase 1 across a process pool (lexing dominates runtime).
"""

from __future__ import annotations

import bisect
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sql2sqlx.emitter import (
    apply_edits_escaped,
    build_sqlx,
    normalize_sqlx_comment,
    ref_expr,
    sqlx_escape_edits,
)
from sql2sqlx.errors import ConversionError, LexError
from sql2sqlx.lexer import IDENT, OP, LineIndex, Token, tokenize
from sql2sqlx.model import (
    ActionDraft,
    ActionType,
    ConversionOptions,
    ConversionReport,
    ConversionResult,
    Layout,
    ParsedFile,
    RefSite,
    ReportWarning,
    SqlxFile,
    TableName,
    sanitize_filename,
)
from sql2sqlx.parser import classify_statement
from sql2sqlx.refs import parse_table_path, scan_ref_sites
from sql2sqlx.splitter import split_statements
from sql2sqlx.version import __version__

#: Resolved table identity used throughout the linker.
TableKey = Tuple[Optional[str], Optional[str], str]
DependencyRef = str

#: Operations eligible for ``hasOutput``. Dataform requires these actions to
#: actually create their configured output; mutating DML is deliberately absent.
_ELECTABLE = frozenset(
    {
        "CREATE TABLE",
        "CREATE VIEW",
        "CREATE MATERIALIZED VIEW",
        "CREATE EXTERNAL TABLE",
        "CREATE SNAPSHOT TABLE",
    }
)

#: Warning codes that assert a typed (table/view/incremental/declaration)
#: conversion. When a duplicate creator is demoted back to a verbatim
#: ``operations`` action those claims no longer describe the emitted SQLX, so
#: they are dropped in :meth:`_Linker._demote` to keep the report consistent
#: with the output (a stale ``INSERT_INCREMENTAL`` beside ``DUPLICATE_TARGET``
#: would otherwise say the action is incremental when it is now operations).
_TYPED_CONVERSION_WARNINGS = frozenset(
    {
        "INSERT_INCREMENTAL",
        "MERGE_INCREMENTAL",
        "TARGET_SCHEMA_REQUIRED",
        "CREATE_REPLACE_SEMANTICS",
        "IF_NOT_EXISTS",
        "DECLARATION_DROPPED_DDL",
    }
)

# Statement heads that require BigQuery's shared script context. RETURN ends
# the script, while ASSERT is an execution guard whose failure must prevent
# later statements from running; splitting either into independent Dataform
# actions changes control-flow semantics.
_PROCEDURAL_HEADS = frozenset(
    {
        "IF",
        "LOOP",
        "WHILE",
        "REPEAT",
        "FOR",
        "CASE",
        "RETURN",
        "ASSERT",
    }
)


def _word(tokens: Sequence[Token], i: int) -> str:
    """Return an uppercased identifier at ``i``, or the empty string."""
    if 0 <= i < len(tokens) and tokens[i].kind == IDENT:
        return tokens[i].upper
    return ""


def _statement_head(tokens: Sequence[Token]) -> str:
    """Return the first keyword of a non-empty statement token sequence."""
    return _word(tokens, 0)


def _create_prefix(tokens: Sequence[Token], start: int = 0) -> Tuple[int, bool, str]:
    """Parse CREATE modifiers and return ``(entity_index, temporary, entity)``."""
    i = start + 1
    if _word(tokens, i) == "OR" and _word(tokens, i + 1) == "REPLACE":
        i += 2
    temporary = _word(tokens, i) in ("TEMP", "TEMPORARY")
    if temporary:
        i += 1
    if _word(tokens, i) in ("MATERIALIZED", "EXTERNAL", "SNAPSHOT"):
        i += 1
    return i, temporary, _word(tokens, i)


def _script_reasons(statements: Sequence[Any]) -> List[str]:
    """Return shared-context reasons requiring whole-file script emission."""
    reasons: Set[str] = set()
    for statement in statements:
        tokens = statement.tokens
        head = _statement_head(tokens)
        if head == "DECLARE" or head == "SET":
            reasons.add("variable state")
        elif head == "CALL" or (head == "EXECUTE" and _word(tokens, 1) == "IMMEDIATE"):
            reasons.add("dynamic side effects")
        elif head in _PROCEDURAL_HEADS:
            reasons.add("assertion sequencing" if head == "ASSERT" else "procedural control flow")
        elif head == "BEGIN":
            if len(tokens) == 1 or _word(tokens, 1) == "TRANSACTION":
                reasons.add("transaction scope")
            else:
                reasons.add("procedural control flow")
        elif head in ("COMMIT", "ROLLBACK"):
            reasons.add("transaction scope")
        elif head == "CREATE":
            _, temporary, entity = _create_prefix(tokens)
            if temporary and entity in ("TABLE", "FUNCTION", "AGGREGATE"):
                reasons.add("temporary object scope")
    return sorted(reasons)


def _rename_target(
    tokens: Sequence[Token],
    i: int,
    original: TableName,
) -> Optional[Tuple[TableName, Tuple[int, int]]]:
    """Parse ``RENAME TO new_name`` after an ALTER target."""
    if _word(tokens, i) != "RENAME" or _word(tokens, i + 1) != "TO":
        return None
    match = parse_table_path(tokens, i + 2)
    if match is None or not (1 <= len(match.parts) <= 3) or not all(match.parts):
        return None
    renamed = TableName.from_parts(list(match.parts))
    if renamed.project is None and renamed.dataset is None:
        renamed = TableName(original.project, original.dataset, renamed.table)
    elif renamed.project is None and original.project is not None:
        renamed = TableName(original.project, renamed.dataset, renamed.table)
    return renamed, (match.start, match.end)


def _scan_script_writes(
    tokens: Sequence[Token],
    statement_start_offsets: Set[int],
) -> List[Tuple[TableName, Tuple[int, int], bool]]:
    """Conservatively discover persistent and temporary writes in a script.

    Nested procedural statements are present in the same significant token
    stream, so scanning keyword-led target shapes is both more useful and safer
    than treating an entire ``BEGIN`` block as an opaque operation.
    """
    writes: List[Tuple[TableName, Tuple[int, int], bool]] = []
    n = len(tokens)
    i = 0
    while i < n:
        head = _word(tokens, i)
        if not head or tokens[i].start not in statement_start_offsets:
            i += 1
            continue
        target_i: Optional[int] = None
        temporary = False
        merge_statement = False
        if head == "MERGE":
            # Only a MERGE *statement* writes anything, and everything up to
            # its terminator belongs to it: the INSERT/UPDATE/DELETE keywords
            # inside its WHEN ... THEN branches are not independent writes
            # (`THEN INSERT ROW` / `THEN INSERT VALUES (...)` would otherwise
            # register phantom targets named ROW/VALUES).
            merge_statement = True
            target_i = i + 1
            if _word(tokens, target_i) == "INTO":
                target_i += 1
        elif head == "CREATE":
            entity_i, temporary, entity = _create_prefix(tokens, i)
            if entity not in ("TABLE", "VIEW"):
                i += 1
                continue
            if entity == "TABLE" and _word(tokens, entity_i + 1) == "FUNCTION":
                i += 1
                continue
            target_i = entity_i + 1
            if (
                _word(tokens, target_i) == "IF"
                and _word(tokens, target_i + 1) == "NOT"
                and _word(tokens, target_i + 2) == "EXISTS"
            ):
                target_i += 3
        elif head == "INSERT":
            target_i = i + 1
            if _word(tokens, target_i) == "INTO":
                target_i += 1
        elif head == "UPDATE":
            target_i = i + 1
        elif head == "DELETE":
            target_i = i + (2 if _word(tokens, i + 1) == "FROM" else 1)
        elif head == "TRUNCATE":
            target_i = i + (2 if _word(tokens, i + 1) == "TABLE" else 1)
        elif head in ("DROP", "ALTER"):
            j = i + 1
            modifier = _word(tokens, j)
            if modifier in ("MATERIALIZED", "EXTERNAL", "SNAPSHOT"):
                j += 1
            if _word(tokens, j) not in ("TABLE", "VIEW"):
                i += 1
                continue
            j += 1
            if _word(tokens, j) == "IF" and _word(tokens, j + 1) == "EXISTS":
                j += 2
            target_i = j
        elif head == "LOAD":
            j = i + 1
            if _word(tokens, j) == "DATA":
                j += 1
            if _word(tokens, j) in ("INTO", "OVERWRITE"):
                j += 1
            target_i = j
        if target_i is None or target_i >= n:
            i += 1
            continue
        match = parse_table_path(tokens, target_i)
        if match is not None and 1 <= len(match.parts) <= 3 and all(match.parts):
            name = TableName.from_parts(list(match.parts))
            is_temp = temporary or (name.dataset is not None and name.dataset.upper() == "_SESSION")
            writes.append((name, (match.start, match.end), is_temp))
            if head == "ALTER":
                renamed = _rename_target(tokens, match.next_index, name)
                if renamed is not None:
                    renamed_name, renamed_span = renamed
                    renamed_temp = temporary or (
                        renamed_name.dataset is not None
                        and renamed_name.dataset.upper() == "_SESSION"
                    )
                    writes.append((renamed_name, renamed_span, renamed_temp))
            i = match.next_index
            if merge_statement:
                # Skip the WHEN ... THEN branches up to the terminator.
                while i < n and not (tokens[i].kind == OP and tokens[i].text == ";"):
                    i += 1
            continue
        i += 1
    return writes


def _scan_script_ddl_reads(
    tokens: Sequence[Token],
    statement_start_offsets: Set[int],
) -> List[RefSite]:
    """Discover table sources in copy/clone/replica DDL inside scripts."""
    reads: List[RefSite] = []
    i = 0
    n = len(tokens)
    while i < n:
        if _word(tokens, i) != "CREATE" or tokens[i].start not in statement_start_offsets:
            i += 1
            continue
        entity_i, _temporary, entity = _create_prefix(tokens, i)
        if entity not in ("TABLE", "VIEW"):
            i += 1
            continue
        target_i = entity_i + 1
        if entity == "TABLE" and _word(tokens, target_i) == "FUNCTION":
            i += 1
            continue
        if (
            _word(tokens, target_i) == "IF"
            and _word(tokens, target_i + 1) == "NOT"
            and _word(tokens, target_i + 2) == "EXISTS"
        ):
            target_i += 3
        target = parse_table_path(tokens, target_i)
        if target is None:
            i += 1
            continue
        j = target.next_index
        source_i: Optional[int] = None
        if entity == "TABLE" and _word(tokens, j) in ("LIKE", "CLONE", "COPY"):
            source_i = j + 1
        elif (
            entity == "VIEW"
            and _word(tokens, j) == "AS"
            and _word(tokens, j + 1) == "REPLICA"
            and _word(tokens, j + 2) == "OF"
        ):
            source_i = j + 3
        if source_i is not None:
            source = parse_table_path(tokens, source_i)
            if source is not None and 1 <= len(source.parts) <= 3 and all(source.parts):
                reads.append(
                    RefSite(
                        source.start,
                        source.end,
                        TableName.from_parts(list(source.parts)),
                    )
                )
        i = target.next_index
    return reads


def _temporary_name(name: TableName, temp_tables: Set[str]) -> bool:
    """Whether ``name`` resolves to a script-local temporary table."""
    return name.table.upper() in temp_tables and (
        name.dataset is None or name.dataset.upper() == "_SESSION"
    )


def _comment_fragment(value: str) -> str:
    """Escape characters that could terminate a generated line comment."""
    out: List[str] = []
    for character in value:
        code = ord(character)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        elif code in (0x2028, 0x2029) or 0xD800 <= code <= 0xDFFF:
            out.append(f"\\u{code:04x}")
        else:
            out.append(character)
    return "".join(out)


# ---------------------------------------------------------------------------
# Phase 1: per-file parsing
# ---------------------------------------------------------------------------


def parse_source(path: str, relpath: str, text: str, opts: ConversionOptions) -> ParsedFile:
    """Parse one SQL source into action drafts.

    Never raises for bad input: lexer failures are captured on the
    returned :class:`~sql2sqlx.model.ParsedFile` as ``error`` so a single
    broken file cannot abort a corpus conversion.

    Args:
        path: Original path (reporting only).
        relpath: Path relative to the conversion root (layout + ordering).
        text: Decoded file contents.
        opts: Conversion options.

    Returns:
        The parsed file.
    """
    input_bytes = len(text.encode("utf-8", "replace"))
    if text.startswith("\ufeff"):
        text = text[1:]
    spans: List[Tuple[int, int]] = []
    try:
        tokens = tokenize(text, comment_spans_out=spans)
    except LexError as exc:
        return ParsedFile(path, relpath, text, error=str(exc), input_bytes=input_bytes)
    statement_start_offsets: Set[int] = set()
    stmts = split_statements(tokens, statement_start_offsets)
    parsed = ParsedFile(
        path, relpath, text, statements=len(stmts), comment_spans=spans, input_bytes=input_bytes
    )
    if not stmts:
        return parsed
    line_index = LineIndex(text)
    script_reasons = _script_reasons(stmts)
    if script_reasons:
        scans: List[List[Tuple[TableName, Tuple[int, int], bool]]] = []
        statement_warnings: List[Tuple[str, str, int]] = []
        for statement in stmts:
            entity = ""
            entity_index = -1
            temporary = False
            if _statement_head(statement.tokens) == "CREATE":
                entity_index, temporary, entity = _create_prefix(statement.tokens)
            if (
                temporary
                and entity == "TABLE"
                and _word(statement.tokens, entity_index + 1) != "FUNCTION"
            ):
                statement_warnings.append(
                    (
                        "TEMP_TABLE",
                        "Temporary objects have no standalone Dataform action "
                        "equivalent; preserved inside the whole-file script.",
                        statement.start,
                    )
                )
            if entity == "PROCEDURE":
                statement_warnings.append(
                    (
                        "PROCEDURE_PRESERVED",
                        "Stored procedure definition kept verbatim; its body is "
                        "not linked as an immediately executed workflow "
                        "dependency.",
                        statement.start,
                    )
                )
            else:
                for index, token in enumerate(statement.tokens):
                    is_dynamic = (
                        token.start in statement_start_offsets
                        and token.kind == IDENT
                        and (
                            token.upper == "CALL"
                            or (
                                token.upper == "EXECUTE"
                                and _word(statement.tokens, index + 1) == "IMMEDIATE"
                            )
                        )
                    )
                    if is_dynamic:
                        statement_warnings.append(
                            (
                                "DYNAMIC_SIDE_EFFECTS",
                                "Called procedures and dynamic SQL can hide table "
                                "reads or writes; kept verbatim, but review manual "
                                "dependencies.",
                                token.start,
                            )
                        )
                        break
            # A procedure body is defined, not executed, by CREATE PROCEDURE;
            # its internal DML must not be registered as an immediate write.
            scans.append(
                []
                if entity == "PROCEDURE"
                else _scan_script_writes(
                    statement.tokens,
                    statement_start_offsets,
                )
            )
        temp_tables = {
            name.table.upper()
            for statement_writes in scans
            for name, _span, temporary in statement_writes
            if temporary
        }
        sites: List[RefSite] = []
        writes: List[TableName] = []
        for statement, statement_writes in zip(stmts, scans):
            spans_to_skip = [span for _name, span, _temporary in statement_writes]
            entity = ""
            if _statement_head(statement.tokens) == "CREATE":
                _, _, entity = _create_prefix(statement.tokens)
            if entity != "PROCEDURE":
                found = scan_ref_sites(
                    statement.tokens,
                    _statement_head(statement.tokens) or "SCRIPT",
                    skip_spans=spans_to_skip,
                    statement_start_offsets=statement_start_offsets,
                )
                found.extend(
                    _scan_script_ddl_reads(
                        statement.tokens,
                        statement_start_offsets,
                    )
                )
                seen_spans: Set[Tuple[int, int]] = set()
                for site in found:
                    span = (site.start, site.end)
                    if _temporary_name(site.name, temp_tables) or span in seen_spans:
                        continue
                    seen_spans.add(span)
                    sites.append(site)
            for name, _span, temporary in statement_writes:
                if not temporary and not _temporary_name(name, temp_tables):
                    writes.append(name)
        draft = ActionDraft(
            action_type=ActionType.OPERATIONS,
            target=None,
            writes_target=False,
            creates_target=False,
            body_start=stmts[0].start,
            body_end=stmts[-1].end,
            stmt_start=stmts[0].start,
            stmt_end=stmts[-1].terminator_end,
            extra_write_targets=writes,
            ref_sites=sites,
            sqlx_escape_edits=[
                edit for statement in stmts for edit in sqlx_escape_edits(statement.tokens)
            ],
            source_line=line_index.locate(stmts[0].start)[0],
            original_kind="SCRIPT",
            script=True,
            warnings=[
                (
                    "SCRIPT_FILE",
                    "File requires shared BigQuery script context "
                    f"({', '.join(script_reasons)}); the whole file was kept "
                    "as one operations action so statement order and scope are "
                    "preserved.",
                    stmts[0].start,
                )
            ]
            + statement_warnings,
        )
        if writes:
            names = ", ".join(sorted({t.display() for t in writes}))
            draft.warnings.append(
                (
                    "SCRIPT_WRITES",
                    f"Script writes: {names}. Downstream readers are ordered "
                    "after this script; ordering between multiple writers of "
                    "the same table follows corpus order.",
                    stmts[0].start,
                )
            )
        parsed.drafts.append(draft)
        return parsed
    for s in stmts:
        draft = classify_statement(s, text, opts)
        draft.sqlx_escape_edits = sqlx_escape_edits(s.tokens)
        draft.source_line = line_index.locate(s.start)[0]
        parsed.drafts.append(draft)
    return parsed


def _parse_worker(job: Tuple[str, str, str, ConversionOptions]) -> ParsedFile:
    """Process-pool entry point: read and parse one file.

    Args:
        job: ``(path, relpath, encoding, options)``.

    Returns:
        The :class:`~sql2sqlx.model.ParsedFile` (with ``error`` set on
        read/decode failure).
    """
    path, relpath, encoding, opts = job
    input_bytes = 0
    try:
        with open(path, "r", encoding=encoding, newline="") as fh:
            input_bytes = os.fstat(fh.fileno()).st_size
            text = fh.read()
    except (OSError, UnicodeError, LookupError) as exc:
        return ParsedFile(path, relpath, "", error=str(exc), input_bytes=input_bytes)
    parsed = parse_source(path, relpath, text, opts)
    parsed.input_bytes = input_bytes
    return parsed


# ---------------------------------------------------------------------------
# Phase 2 + 3: linking and emission
# ---------------------------------------------------------------------------


class _Linker:
    """Cross-file resolution, dependency wiring, naming, and emission."""

    def __init__(
        self, files: List[ParsedFile], opts: ConversionOptions, report: ConversionReport
    ) -> None:
        """Initialize with parsed files (failed files excluded upstream).

        Args:
            files: Successfully parsed files.
            opts: Conversion options.
            report: Report to accumulate findings into.
        """
        self.files = sorted(files, key=lambda f: f.relpath)
        self.opts = opts
        self.report = report
        self.ordered: List[Tuple[ParsedFile, ActionDraft]] = [
            (f, d) for f in self.files for d in f.drafts
        ]
        self.creators: Dict[TableKey, Tuple[ParsedFile, ActionDraft]] = {}
        self.chains: Dict[TableKey, List[Tuple[ParsedFile, ActionDraft]]] = {}
        self.names: Dict[int, str] = {}
        self.dep_refs: Dict[int, DependencyRef] = {}
        self.used_names: Set[str] = set()
        self._next_suffix: Dict[str, int] = {}
        self._next_path_suffix: Dict[str, int] = {}
        self._line_cache: Dict[str, LineIndex] = {}
        self._unresolved: Set[str] = set()
        self._order_index = {id(d): i for i, (_f, d) in enumerate(self.ordered)}
        self._dependency_graph: Dict[int, Set[int]] = {}
        self._dependency_targets: Set[int] = set()
        self._cycle_warnings: Set[Tuple[int, int]] = set()
        self._chain_orders: Dict[TableKey, List[int]] = {}
        self._chain_positions: Dict[Tuple[TableKey, int], int] = {}
        self._access_relpath: Optional[str] = None
        self._readers_since_write: Dict[TableKey, List[ActionDraft]] = {}

    # -- helpers ------------------------------------------------------------

    def _resolve(self, name: TableName) -> TableKey:
        """Resolve a table name against defaults; return its identity key."""
        return name.resolve(self.opts.default_project, self.opts.default_dataset).key()

    def _warn(self, f: ParsedFile, d: Optional[ActionDraft], code: str, message: str) -> None:
        """Record a linker-stage warning against a file/draft."""
        line = d.source_line if d is not None else 0
        self.report.warnings.append(ReportWarning(code, message, f.relpath, line))

    @staticmethod
    def _key_display(key: TableKey) -> str:
        """Dotted display form of a resolved key."""
        return ".".join(p for p in key if p)

    def _lines(self, f: ParsedFile) -> LineIndex:
        """Cached :class:`LineIndex` for a file."""
        idx = self._line_cache.get(f.relpath)
        if idx is None:
            idx = LineIndex(f.text)
            self._line_cache[f.relpath] = idx
        return idx

    def _comments(self, f: ParsedFile) -> List[Tuple[int, int]]:
        """Comment spans captured during the file's single lexing pass."""
        return f.comment_spans

    def _add_dependency_edge(
        self, f: ParsedFile, d: ActionDraft, dependency: ActionDraft, reason: str
    ) -> bool:
        """Add an action dependency unless it would create a DAG cycle."""
        source_id = id(d)
        dependency_id = id(dependency)
        if source_id == dependency_id:
            return False
        edges = self._dependency_graph.setdefault(source_id, set())
        if dependency_id in edges:
            return True
        # A path can reach the source only when some existing edge targets
        # it. This O(1) guard keeps long writer chains linear while retaining
        # a full reachability check for forward cross-file references.
        if source_id in self._dependency_targets:
            stack = [dependency_id]
            seen: Set[int] = set()
            while stack:
                node = stack.pop()
                if node == source_id:
                    pair = (source_id, dependency_id)
                    if pair not in self._cycle_warnings:
                        self._cycle_warnings.add(pair)
                        self._warn(
                            f,
                            d,
                            "DEPENDENCY_CYCLE",
                            f"{reason} was left literal/implicit because adding "
                            f"a dependency from {self.names[source_id]!r} to "
                            f"{self.names[dependency_id]!r} would create a "
                            "Dataform cycle.",
                        )
                    return False
                if node in seen:
                    continue
                seen.add(node)
                stack.extend(self._dependency_graph.get(node, ()))
        edges.add(dependency_id)
        self._dependency_targets.add(dependency_id)
        return True

    def _latest_preceding_writer(
        self,
        key: TableKey,
        current: ActionDraft,
    ) -> Optional[ActionDraft]:
        """Return the last writer before ``current`` in corpus order."""
        current_index = self._order_index[id(current)]
        orders = self._chain_orders.get(key, [])
        position = bisect.bisect_left(orders, current_index) - 1
        if position < 0:
            return None
        return self.chains[key][position][1]

    # -- pipeline stages ------------------------------------------------------

    def run(self) -> List[SqlxFile]:
        """Execute all linking stages and emit every ``.sqlx`` file."""
        self._collect_creators()
        self._build_chains()
        self._elect_outputs()
        self._assign_names()
        files = self._emit_all()
        self.report.refs_unresolved = sorted(self._unresolved)
        return files

    def _collect_creators(self) -> None:
        """Register one creator per produced table; demote duplicates."""
        for f, d in self.ordered:
            if d.target is None or not d.creates_target:
                continue
            key = self._resolve(d.target)
            if key in self.creators:
                self._demote(f, d)
            else:
                self.creators[key] = (f, d)

    def _demote(self, f: ParsedFile, d: ActionDraft) -> None:
        """Turn a duplicate creator back into a verbatim operations draft."""
        assert d.target is not None
        self._warn(
            f,
            d,
            "DUPLICATE_TARGET",
            f"{d.original_kind} for {d.target.display()} demoted to "
            "operations: another statement already produces this "
            "table and Dataform allows exactly one owner per target. "
            "The statement runs verbatim, ordered after the owner.",
        )
        d.action_type = ActionType.OPERATIONS
        d.creates_target = False
        d.writes_target = True
        d.body_start = d.stmt_start
        d.body_end = d.stmt_end
        d.edits = []
        d.config = {}
        # The abandoned typed conversion's warnings (e.g. "converted to
        # incremental") no longer describe the emitted operations action;
        # drop them so the report matches what was actually generated.
        d.warnings = [w for w in d.warnings if w[0] not in _TYPED_CONVERSION_WARNINGS]

    def _build_chains(self) -> None:
        """Group creators and writers per table, in corpus order."""
        for f, d in self.ordered:
            keys: List[TableKey] = []
            if d.target is not None and (d.creates_target or d.writes_target):
                keys.append(self._resolve(d.target))
            for extra in d.extra_write_targets:
                key = self._resolve(extra)
                if key not in keys:
                    keys.append(key)
            for key in keys:
                self.chains.setdefault(key, []).append((f, d))
        for key, chain in self.chains.items():
            self._chain_orders[key] = [self._order_index[id(draft)] for _file, draft in chain]
            for position, (_file, draft) in enumerate(chain):
                self._chain_positions[(key, id(draft))] = position
            rels = {cf.relpath for cf, _ in chain}
            if len(chain) > 1 and len(rels) > 1:
                self.report.warnings.append(
                    ReportWarning(
                        "ORDER_ASSUMED",
                        f"Table {self._key_display(key)} is written by "
                        f"statements in multiple files ({', '.join(sorted(rels))}); "
                        "their relative execution order was inferred from sorted "
                        "file paths - verify the generated dependency chain.",
                        sorted(rels)[0],
                        0,
                    )
                )

    def _elect_outputs(self) -> None:
        """Give ``hasOutput`` to the first eligible writer of ownerless tables."""
        for key, chain in self.chains.items():
            if key in self.creators:
                continue
            for f, d in chain:
                if d.script or d.target is None:
                    continue
                if self._resolve(d.target) != key:
                    continue
                if d.original_kind in _ELECTABLE and d.primary_target_span is not None:
                    d.config["hasOutput"] = True
                    d.edits.append(
                        (d.primary_target_span[0], d.primary_target_span[1], "${self()}")
                    )
                    self.creators[key] = (f, d)
                    break

    def _assign_names(self) -> None:
        """Assign unique action names and dependency strings."""
        for _f, d in self.ordered:
            if d.target is None or not (d.creates_target or d.config.get("hasOutput")):
                continue
            ident = id(d)
            name = d.target.table
            self.names[ident] = name
            if d.target.project:
                dep = ".".join((d.target.project, d.target.dataset or "", name))
            else:
                dep = f"{d.target.dataset}.{name}" if d.target.dataset else name
            self.dep_refs[ident] = dep
            self.used_names.add(name)
        for f, d in self.ordered:
            ident = id(d)
            if ident in self.names:
                continue
            if d.script or d.target is None:
                base = sanitize_filename(PurePosixPath(f.relpath).stem)
            else:
                verb = d.original_kind.split()[0].lower()
                base = sanitize_filename(f"{d.target.table}_{verb}")
            name = self._uniq(base)
            self.names[ident] = name
            self.dep_refs[ident] = name

    def _uniq(self, base: str) -> str:
        """Return ``base`` (or ``base_N``) unused by any other action."""
        if base not in self.used_names:
            self.used_names.add(base)
            self._next_suffix.setdefault(base, 2)
            return base
        counter = self._next_suffix.get(base, 2)
        name = f"{base}_{counter}"
        while name in self.used_names:
            counter += 1
            name = f"{base}_{counter}"
        self._next_suffix[base] = counter + 1
        self.used_names.add(name)
        return name

    # -- emission --------------------------------------------------------------

    def _emit_all(self) -> List[SqlxFile]:
        """Emit every draft plus synthesized external declarations."""
        out: List[SqlxFile] = []
        declared: Dict[TableKey, TableName] = {}
        paths: Set[str] = set()
        last_drafts = {id(f.drafts[-1]) for f in self.files if f.drafts}
        current_rel = None
        prev_end = 0
        for f, d in self.ordered:
            if f.relpath != current_rel:
                current_rel = f.relpath
                prev_end = 0
            out.append(self._emit_draft(f, d, declared, paths, prev_end, id(d) in last_drafts))
            prev_end = d.stmt_end
            tally = self.report.actions_by_type
            tally[d.action_type.value] = tally.get(d.action_type.value, 0) + 1
        for key in sorted(declared, key=lambda k: (k[0] or "", k[1] or "", k[2])):
            name = declared[key]
            config: Dict[str, Any] = {"type": ActionType.DECLARATION.value}
            if name.project:
                config["database"] = name.project
            if name.dataset:
                config["schema"] = name.dataset
            config["name"] = name.table
            rel = self._dedupe_path(f"sources/{sanitize_filename(name.table)}.sqlx", paths)
            out.append(SqlxFile(rel, build_sqlx(config), ActionType.DECLARATION, name.table))
            tally = self.report.actions_by_type
            key_name = ActionType.DECLARATION.value
            tally[key_name] = tally.get(key_name, 0) + 1
        out.sort(key=lambda s: s.relpath)
        return out

    def _dedupe_path(self, rel: str, paths: Set[str]) -> str:
        """Ensure a portable output path, suffixing ``_N`` when needed."""
        base_key = rel.casefold()
        if base_key not in paths:
            paths.add(base_key)
            self._next_path_suffix.setdefault(base_key, 2)
            return rel
        counter = self._next_path_suffix.get(base_key, 2)
        candidate = f"{rel[:-5]}_{counter}.sqlx"
        while candidate.casefold() in paths:
            counter += 1
            candidate = f"{rel[:-5]}_{counter}.sqlx"
        self._next_path_suffix[base_key] = counter + 1
        paths.add(candidate.casefold())
        return candidate

    def _declarable(self, name: TableName) -> bool:
        """Whether an unresolved reference may become a declaration."""
        if name.dataset is None:
            return False
        parts = [p for p in (name.project, name.dataset, name.table) if p]
        if any(p.upper() == "INFORMATION_SCHEMA" for p in parts):
            return False
        if any(p.lower().startswith("region-") for p in parts):
            return False
        # Wildcard tables and partition/time decorators are table
        # expressions, not standalone relations that Dataform can declare.
        return not any(marker in name.table for marker in ("*", "$", "@"))

    def _emit_draft(
        self,
        f: ParsedFile,
        d: ActionDraft,
        declared: Dict[TableKey, TableName],
        paths: Set[str],
        prev_end: int,
        is_last_in_file: bool,
    ) -> SqlxFile:
        """Link and render one draft into a :class:`SqlxFile`."""
        deps: List[DependencyRef] = []
        if self._access_relpath != f.relpath:
            self._access_relpath = f.relpath
            self._readers_since_write.clear()
        own_keys: Set[TableKey] = set()
        if d.target is not None and (d.creates_target or d.writes_target):
            own_keys.add(self._resolve(d.target))
        for extra in d.extra_write_targets:
            own_keys.add(self._resolve(extra))
        read_keys = {self._resolve(site.name) for site in d.ref_sites} - own_keys
        # Preserve read-before-write order within a source file. A reader
        # already depends on its latest preceding writer (RAW); the inverse
        # edge below prevents a later mutation from racing ahead of readers
        # that occur between two writes (WAR).
        for key in own_keys:
            for reader in self._readers_since_write.get(key, []):
                if self._add_dependency_edge(
                    f,
                    d,
                    reader,
                    f"Read-before-write ordering for {self._key_display(key)}",
                ):
                    deps.append(self.dep_refs[id(reader)])
            self._readers_since_write[key] = []
        # Chain predecessor dependencies (write ordering).
        for key in own_keys:
            chain = self.chains.get(key) or []
            position = self._chain_positions.get((key, id(d)))
            if position is not None and position > 0:
                predecessor = chain[position - 1][1]
                if self._add_dependency_edge(
                    f,
                    d,
                    predecessor,
                    f"Writer ordering for {self._key_display(key)}",
                ):
                    deps.append(self.dep_refs[id(predecessor)])
        # Reference rewriting + latest-preceding-writer dependencies. A
        # future mutation or creator must never be pulled before an earlier
        # reader merely to satisfy a generated dependency.
        self_warned = False
        future_warned: Set[TableKey] = set()
        for site in d.ref_sites:
            key = self._resolve(site.name)
            if key in own_keys:
                if d.creates_target and not self_warned:
                    assert d.target is not None
                    self._warn(
                        f,
                        d,
                        "SELF_REFERENCE",
                        f"{d.target.display()} reads itself inside "
                        "its own defining query; the reference was "
                        "left as a literal (Dataform cannot ref an "
                        "action into itself).",
                    )
                    self_warned = True
                continue
            entry = self.creators.get(key)
            preceding_writer = self._latest_preceding_writer(key, d)
            if entry is None:
                resolved_name = site.name.resolve(
                    self.opts.default_project, self.opts.default_dataset
                )
                if self.opts.declare_external and self._declarable(resolved_name):
                    declared.setdefault(key, resolved_name)
                    d.edits.append((site.start, site.end, ref_expr(declared[key])))
                    self.report.refs_rewritten += 1
                elif resolved_name.dataset is not None:
                    self._unresolved.add(resolved_name.display())
                # Even without a ref-able creator (for example an existing
                # table mutated by a script), order the reader after the most
                # recent write that precedes it in corpus order.
                if preceding_writer is not None and self._add_dependency_edge(
                    f,
                    d,
                    preceding_writer,
                    f"Read ordering for {self._key_display(key)}",
                ):
                    deps.append(self.dep_refs[id(preceding_writer)])
                continue
            creator_file, creator = entry
            if creator is d:
                continue
            assert creator.target is not None
            if creator_file is f and self._order_index[id(creator)] > self._order_index[id(d)]:
                if key not in future_warned:
                    future_warned.add(key)
                    self._warn(
                        f,
                        d,
                        "FUTURE_CREATOR",
                        f"Reference to {self._key_display(key)} was left "
                        "literal because its Dataform owner occurs later in "
                        "corpus order; generating ref() would move that future "
                        "creator ahead of this read.",
                    )
                if preceding_writer is not None and self._add_dependency_edge(
                    f,
                    d,
                    preceding_writer,
                    f"Read ordering for {self._key_display(key)}",
                ):
                    deps.append(self.dep_refs[id(preceding_writer)])
                continue
            if not self._add_dependency_edge(
                f,
                d,
                creator,
                f"Reference to {self._key_display(key)}",
            ):
                continue
            d.edits.append((site.start, site.end, ref_expr(creator.target)))
            self.report.refs_rewritten += 1
            if (
                preceding_writer is not None
                and preceding_writer is not creator
                and self._add_dependency_edge(
                    f,
                    d,
                    preceding_writer,
                    f"Read ordering for {self._key_display(key)}",
                )
            ):
                deps.append(self.dep_refs[id(preceding_writer)])

        for key in read_keys:
            readers = self._readers_since_write.setdefault(key, [])
            if not any(reader is d for reader in readers):
                readers.append(d)

        config: Dict[str, Any] = {"type": d.action_type.value}
        named = d.target is not None and (d.creates_target or d.config.get("hasOutput"))
        if named:
            assert d.target is not None
            if d.target.project:
                config["database"] = d.target.project
            if d.target.dataset:
                config["schema"] = d.target.dataset
            config["name"] = d.target.table
        else:
            config["name"] = self.names[id(d)]
        config.update(d.config)
        unique_deps: List[DependencyRef] = []
        seen_deps: Set[DependencyRef] = set()
        for dep in deps:
            if dep in seen_deps:
                continue
            seen_deps.add(dep)
            unique_deps.append(dep)
        if unique_deps:
            config["dependencies"] = unique_deps
        if self.opts.tags:
            config["tags"] = list(self.opts.tags)

        annotation = None
        if self.opts.annotate and d.action_type is not ActionType.DECLARATION:
            annotation = (
                f"-- source: {_comment_fragment(f.relpath)}:{d.source_line} "
                f"({d.original_kind} converted by sql2sqlx "
                f"v{__version__})"
            )
        leading = None
        spans = [(a, b) for a, b in self._comments(f) if prev_end <= a and b <= d.stmt_start]
        if spans:
            leading = "\n".join(normalize_sqlx_comment(f.text[a:b]) for a, b in spans)
        trailing = None
        if is_last_in_file:
            tail_spans = [(a, b) for a, b in self._comments(f) if a >= d.stmt_end]
            if tail_spans:
                trailing = "\n".join(normalize_sqlx_comment(f.text[a:b]) for a, b in tail_spans)
        body = None
        if d.action_type is not ActionType.DECLARATION:
            # Semantic rewrites own their complete source spans. In
            # particular, a ref()/self() edit can cover a multi-part path
            # whose first backtick token also needs literal-SQLX escaping;
            # applying that narrower escape first would suppress the ref.
            escape_edits = [
                escape
                for escape in d.sqlx_escape_edits
                if not any(
                    semantic[0] < escape[1] and escape[0] < semantic[1] for semantic in d.edits
                )
            ]
            comment_edits = []
            for a, b in self._comments(f):
                if d.body_start <= a and b <= d.body_end:
                    normalized = normalize_sqlx_comment(f.text[a:b])
                    if normalized != f.text[a:b]:
                        comment_edits.append((a, b, normalized))
            body = apply_edits_escaped(
                f.text,
                d.body_start,
                d.body_end,
                d.edits + escape_edits + comment_edits,
            )
        content = build_sqlx(config, body, annotation, leading, trailing)

        stem = sanitize_filename(
            d.target.table if named and d.target is not None else self.names[id(d)]
        )
        if self.opts.layout is Layout.MIRROR:
            parent = PurePosixPath(f.relpath).parent.as_posix()
            rel = f"{parent}/{stem}.sqlx" if parent != "." else f"{stem}.sqlx"
        else:
            rel = f"{stem}.sqlx"
        rel = self._dedupe_path(rel, paths)

        lines = self._lines(f)
        for code, message, offset in d.warnings:
            self.report.warnings.append(
                ReportWarning(code, message, f.relpath, lines.locate(offset)[0])
            )
        return SqlxFile(rel, content, d.action_type, config["name"], f.relpath, d.source_line)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _finalize(
    parsed: List[ParsedFile], opts: ConversionOptions, started: float
) -> ConversionResult:
    """Run the linker over parsed files and assemble the result."""
    report = ConversionReport()
    good: List[ParsedFile] = []
    for pf in parsed:
        report.files_read += 1
        report.statements += pf.statements
        report.input_bytes += pf.input_bytes
        if pf.error is not None:
            report.failures[pf.relpath] = pf.error
        else:
            good.append(pf)
    files = _Linker(good, opts, report).run()
    report.elapsed_seconds = round(time.perf_counter() - started, 3)
    return ConversionResult(files=files, report=report)


def convert_string(
    sql: str, options: Optional[ConversionOptions] = None, name: str = "input.sql"
) -> ConversionResult:
    """Convert a SQL string to Dataform SQLX files.

    Args:
        sql: BigQuery SQL text (one or many statements).
        options: Conversion options (defaults used when ``None``).
        name: Virtual file name used for layout, ordering and reports.

    Returns:
        The :class:`~sql2sqlx.model.ConversionResult`.

    Example:
        >>> result = convert_string(
        ...     "CREATE TABLE ds.t AS SELECT 1 AS x;")
        >>> result.files[0].action_type.value
        'table'
    """
    opts = options or ConversionOptions()
    started = time.perf_counter()
    parsed = [parse_source(name, name, sql, opts)]
    return _finalize(parsed, opts, started)


def convert_file(path: str, options: Optional[ConversionOptions] = None) -> ConversionResult:
    """Convert a single ``.sql`` file.

    Args:
        path: Path to the SQL file.
        options: Conversion options (defaults used when ``None``).

    Returns:
        The :class:`~sql2sqlx.model.ConversionResult`.
    """
    opts = options or ConversionOptions()
    started = time.perf_counter()
    p = Path(path)
    parsed = [_parse_worker((str(p), p.name, opts.encoding, opts))]
    return _finalize(parsed, opts, started)


def convert_directory(
    input_dir: str, output_dir: Optional[str] = None, options: Optional[ConversionOptions] = None
) -> ConversionResult:
    """Convert every matching SQL file under a directory tree.

    Phase 1 (read + lex + split + classify) runs across a process pool
    sized by ``options.jobs`` (``0`` = one worker per CPU); linking and
    emission are a fast single-process metadata pass.

    Args:
        input_dir: Root directory scanned recursively with
            ``options.include_glob`` (default ``*.sql``).
        output_dir: When given, generated files are written beneath it
            (directories created as needed).
        options: Conversion options (defaults used when ``None``).

    Returns:
        The :class:`~sql2sqlx.model.ConversionResult`; per-file failures
        are reported in ``result.report.failures`` without aborting.

    Raises:
        NotADirectoryError: If ``input_dir`` is not a directory.
    """
    opts = options or ConversionOptions()
    started = time.perf_counter()
    root = Path(input_dir)
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {input_dir}")
    sql_files = sorted(p for p in root.rglob(opts.include_glob) if p.is_file())
    jobs = opts.jobs if opts.jobs > 0 else (os.cpu_count() or 1)
    jobs = max(1, min(jobs, len(sql_files) or 1))
    tasks = [(str(p), p.relative_to(root).as_posix(), opts.encoding, opts) for p in sql_files]
    if jobs > 1 and len(tasks) > 1:
        chunk = max(1, len(tasks) // (jobs * 4))
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            parsed = list(pool.map(_parse_worker, tasks, chunksize=chunk))
    else:
        parsed = [_parse_worker(t) for t in tasks]
    result = _finalize(parsed, opts, started)
    if output_dir is not None:
        write_result(result, output_dir)
    return result


def write_result(result: ConversionResult, output_dir: str) -> None:
    """Write every generated file beneath ``output_dir``.

    Args:
        result: A conversion result.
        output_dir: Destination root (created if missing).

    Raises:
        ConversionError: If a generated relative path would escape the
            destination (including through an existing symlink).
    """
    out_root = Path(output_dir).resolve()
    destinations: List[Tuple[Path, SqlxFile]] = []
    for sqlx in result.files:
        dest = (out_root / sqlx.relpath).resolve()
        try:
            dest.relative_to(out_root)
        except ValueError:
            raise ConversionError(
                f"generated output path escapes the destination: {sqlx.relpath!r}"
            ) from None
        destinations.append((dest, sqlx))
    for dest, sqlx in destinations:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="") as handle:
            handle.write(sqlx.content)
