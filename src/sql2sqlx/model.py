# Copyright (c) Soumyadip Sarkar.
# All rights reserved.
#
# This source code is licensed under the Apache-style license found in the
# LICENSE file in the root directory of this source tree.

"""Core data model shared across all sql2sqlx stages.

Everything here is a plain, picklable dataclass so parsed results can be
shipped between worker processes during parallel directory conversion.
"""

from __future__ import annotations

import codecs
import dataclasses
import enum
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Table names
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableName:
    """A (possibly partial) BigQuery table path.

    Attributes:
        project: GCP project id, or ``None`` if the reference/target did
            not qualify one.
        dataset: Dataset id, or ``None`` if unqualified.
        table: Table id (always present).
    """

    project: Optional[str]
    dataset: Optional[str]
    table: str

    @staticmethod
    def from_parts(parts: List[str]) -> "TableName":
        """Build a :class:`TableName` from 1-3 decoded path parts.

        Args:
            parts: ``[table]``, ``[dataset, table]`` or
                ``[project, dataset, table]``.

        Returns:
            The corresponding :class:`TableName`.

        Raises:
            ValueError: If ``parts`` is empty or longer than 3.
        """
        if not parts or len(parts) > 3:
            raise ValueError(f"invalid table path parts: {parts!r}")
        if len(parts) == 1:
            return TableName(None, None, parts[0])
        if len(parts) == 2:
            return TableName(None, parts[0], parts[1])
        return TableName(parts[0], parts[1], parts[2])

    def resolve(
        self, default_project: Optional[str], default_dataset: Optional[str]
    ) -> "TableName":
        """Fill missing qualifiers from defaults.

        Args:
            default_project: Project to assume when unqualified.
            default_dataset: Dataset to assume when unqualified.

        Returns:
            A new :class:`TableName` with defaults applied (fields that
            have no default remain ``None``).
        """
        return TableName(
            self.project or default_project,
            self.dataset or default_dataset,
            self.table,
        )

    def key(self) -> Tuple[Optional[str], Optional[str], str]:
        """Return a hashable identity key ``(project, dataset, table)``."""
        return (self.project, self.dataset, self.table)

    def display(self) -> str:
        """Human-readable dotted form, e.g. ``proj.ds.table`` or ``ds.table``."""
        return ".".join(p for p in (self.project, self.dataset, self.table) if p)


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


class InsertStrategy(str, enum.Enum):
    """How ``INSERT INTO ... SELECT`` statements are converted."""

    #: Convert to a Dataform ``type: "incremental"`` action (opt-in).
    INCREMENTAL = "incremental"
    #: Keep the statement verbatim as an ``operations`` action (default).
    OPERATIONS = "operations"


class MergeStrategy(str, enum.Enum):
    """How ``MERGE`` statements are converted."""

    #: Keep the statement verbatim as an ``operations`` action (default;
    #: exactly preserves semantics).
    OPERATIONS = "operations"
    #: Convert a shape-proven MERGE to ``type: "incremental"`` with
    #: ``uniqueKey`` and report the required target-schema precondition;
    #: otherwise fall back to operations.
    INCREMENTAL_WHEN_SAFE = "incremental-when-safe"


class PlainCreateStrategy(str, enum.Enum):
    """How ``CREATE TABLE`` *without* ``AS SELECT`` is converted."""

    #: Keep the DDL verbatim as an ``operations`` action (default).
    OPERATIONS = "operations"
    #: Emit a Dataform ``type: "declaration"`` instead (drops the DDL;
    #: use when such tables are externally managed sources).
    DECLARATION = "declaration"


class IfNotExistsStrategy(str, enum.Enum):
    """How guarded ``CREATE TABLE/VIEW ... AS`` statements are converted."""

    #: Convert to ``type: "table"`` and record a semantics warning
    #: (Dataform always creates-or-replaces). Opt-in.
    TABLE = "table"
    #: Keep verbatim as ``operations`` (exact semantics; default).
    OPERATIONS = "operations"


class Layout(str, enum.Enum):
    """Output file layout for directory conversions."""

    #: Mirror the input directory structure under the output root.
    MIRROR = "mirror"
    #: Write all ``.sqlx`` files directly into the output root.
    FLAT = "flat"


@dataclass
class ConversionOptions:
    """All knobs controlling a conversion. Safe defaults preserve semantics.

    Attributes:
        default_project: Project assumed for unqualified table paths, used
            for cross-file reference resolution and emitted as ``database``
            only when the source SQL qualified it explicitly.
        default_dataset: Dataset assumed for unqualified paths (also used
            to resolve single-part references).
        default_location: Dataform project location used only when scaffolding
            ``workflow_settings.yaml``.
        insert_strategy: See :class:`InsertStrategy`. The semantics-preserving
            default is ``operations``; typed incremental conversion is opt-in.
        merge_strategy: See :class:`MergeStrategy`.
        plain_create_strategy: See :class:`PlainCreateStrategy`.
        if_not_exists_strategy: See :class:`IfNotExistsStrategy`. The default
            keeps guarded table and view definitions verbatim as operations.
        declare_external: If ``True``, generate ``type: "declaration"``
            actions for tables that are *referenced* but never *produced*
            by the corpus, and rewrite those references to ``ref()`` too.
            (``INFORMATION_SCHEMA`` and ``region-*`` paths are excluded.)
        protect_incrementals: Emit ``protected: true`` on incrementals
            converted from ``INSERT``/``MERGE`` so an accidental
            ``--full-refresh`` cannot drop pre-existing rows.
        annotate: Prepend a provenance comment (source file/line and the
            original statement kind) to each generated body.
        tags: Extra Dataform ``tags`` added to every generated action.
        layout: See :class:`Layout`.
        encoding: Text encoding used to read input files.
        include_glob: Filename glob for directory scans.
        jobs: Worker processes for directory conversion; ``0`` = auto
            (``os.cpu_count()``), ``1`` = no multiprocessing.
    """

    default_project: Optional[str] = None
    default_dataset: Optional[str] = None
    insert_strategy: InsertStrategy = InsertStrategy.OPERATIONS
    merge_strategy: MergeStrategy = MergeStrategy.OPERATIONS
    plain_create_strategy: PlainCreateStrategy = PlainCreateStrategy.OPERATIONS
    if_not_exists_strategy: IfNotExistsStrategy = IfNotExistsStrategy.OPERATIONS
    declare_external: bool = False
    protect_incrementals: bool = True
    annotate: bool = True
    tags: List[str] = field(default_factory=list)
    layout: Layout = Layout.MIRROR
    encoding: str = "utf-8"
    include_glob: str = "*.sql"
    jobs: int = 0
    default_location: str = "US"

    def __post_init__(self) -> None:
        """Normalize enum-like values and reject invalid runtime options.

        The public Python API is often populated from JSON or configuration
        files, so accepting the documented string enum values is useful.  A
        silent string/enum mismatch is not: the classifier compares enum
        members and would otherwise select the wrong strategy.
        """
        self.insert_strategy = InsertStrategy(self.insert_strategy)
        self.merge_strategy = MergeStrategy(self.merge_strategy)
        self.plain_create_strategy = PlainCreateStrategy(self.plain_create_strategy)
        self.if_not_exists_strategy = IfNotExistsStrategy(self.if_not_exists_strategy)
        self.layout = Layout(self.layout)
        for name in ("default_project", "default_dataset"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be a non-empty string or None")
        if not isinstance(self.default_location, str) or not self.default_location:
            raise ValueError("default_location must be a non-empty string")
        for name in ("declare_external", "protect_incrementals", "annotate"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        if isinstance(self.tags, (str, bytes)):
            raise ValueError("tags must be a sequence of non-empty strings")
        try:
            tags = list(self.tags)
        except TypeError:
            raise ValueError("tags must be a sequence of non-empty strings") from None
        if any(not isinstance(tag, str) or not tag for tag in tags):
            raise ValueError("tags must be a sequence of non-empty strings")
        self.tags = tags
        if not isinstance(self.jobs, int) or isinstance(self.jobs, bool):
            raise ValueError("jobs must be an integer")
        if self.jobs < 0:
            raise ValueError("jobs must be zero (auto) or a positive integer")
        if not isinstance(self.encoding, str) or not self.encoding:
            raise ValueError("encoding must not be empty")
        try:
            codec = codecs.lookup(self.encoding)
        except LookupError:
            raise ValueError(f"unknown encoding: {self.encoding!r}") from None
        if not getattr(codec, "_is_text_encoding", True):
            raise ValueError(f"encoding is not a text codec: {self.encoding!r}")
        if not isinstance(self.include_glob, str) or not self.include_glob:
            raise ValueError("include_glob must not be empty")
        glob_path = PurePath(self.include_glob)
        if glob_path.is_absolute() or ".." in glob_path.parts:
            raise ValueError("include_glob must stay within the input directory")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class ActionType(str, enum.Enum):
    """Dataform action types emitted by sql2sqlx."""

    TABLE = "table"
    VIEW = "view"
    INCREMENTAL = "incremental"
    OPERATIONS = "operations"
    DECLARATION = "declaration"


@dataclass
class RefSite:
    """A rewritable table reference discovered inside a statement body.

    Attributes:
        start: Character offset (in the source file) where the path begins.
        end: Character offset one past the path.
        name: The referenced :class:`TableName` (decoded, unresolved).
    """

    start: int
    end: int
    name: TableName


@dataclass
class ActionDraft:
    """A parsed statement lifted into a not-yet-linked Dataform action.

    Drafts are produced per-file in parallel workers; the linker then
    resolves cross-file references, assigns unique action names, wires
    dependency chains and hands the result to the emitter.

    Attributes:
        action_type: The Dataform action type.
        target: The table produced/written by this action (``None`` for
            pure reads or unclassifiable scripts).
        writes_target: True when this action *mutates* ``target`` without
            being its creator (UPDATE/DELETE/MERGE/INSERT-as-operations...);
            drives dependency chaining.
        creates_target: True when this action creates/replaces ``target``
            (CREATE TABLE/VIEW ``AS``, and INSERT converted to incremental).
        body_start: Source offset where the emitted SQL body begins.
        body_end: Source offset one past the emitted body.
        stmt_start: Source offset of the full original statement (used when
            a draft must be demoted back to a verbatim ``operations`` body).
        stmt_end: Source offset one past the full original statement.
        extra_write_targets: For whole-file script drafts, every table the
            script writes (INSERT/UPDATE/MERGE/CREATE/... targets found in
            its statements); the linker treats the script as a writer of
            each so downstream readers are ordered after it.
        edits: Pending span edits ``(start, end, replacement)`` on the
            source text, absolute offsets; produced by the classifier
            (e.g. select-list aliasing) and later by ref rewriting.
        sqlx_escape_edits: Non-semantic edits that quote literal ``${``
            sequences for the Dataform SQLX compiler. Kept separate so a
            typed draft can be demoted without losing required escaping.
        primary_target_span: Span of the statement's own target path, if
            the emitter should replace it with ``${self()}`` (only used
            for elected ``hasOutput`` operations).
        ref_sites: Candidate table references inside the body.
        config: Dataform ``config { ... }`` entries already known at parse
            time (type/name/schema excluded - the linker fills those).
        source_line: 1-based line of the statement in its source file.
        original_kind: Uppercase leading keyword(s) of the original
            statement (for annotations/reporting), e.g. ``"MERGE"``.
        warnings: ``(code, message, offset)`` triples raised during
            classification; the converter turns them into report entries.
        script: True when the draft wraps an entire multi-statement file.
    """

    action_type: ActionType
    target: Optional[TableName]
    writes_target: bool
    creates_target: bool
    body_start: int
    body_end: int
    stmt_start: int = 0
    stmt_end: int = 0
    extra_write_targets: List[TableName] = field(default_factory=list)
    edits: List[Tuple[int, int, str]] = field(default_factory=list)
    sqlx_escape_edits: List[Tuple[int, int, str]] = field(default_factory=list)
    primary_target_span: Optional[Tuple[int, int]] = None
    ref_sites: List[RefSite] = field(default_factory=list)
    config: Dict[str, Any] = field(default_factory=dict)
    source_line: int = 0
    original_kind: str = ""
    warnings: List[Tuple[str, str, int]] = field(default_factory=list)
    script: bool = False


@dataclass
class ParsedFile:
    """All drafts extracted from one source file, plus its raw text.

    Attributes:
        path: Path of the source file (as given; relative paths preserved).
        relpath: Path relative to the conversion root (used for layout).
        text: Full decoded file contents.
        drafts: Statement drafts in source order.
        error: Fatal per-file error message, or ``None`` on success. Files
            with an error contribute no drafts and are listed as failures
            in the report; other files are unaffected.
        statements: Number of top-level statements found in the file
            (counted before any whole-file script collapsing).
        comment_spans: ``(start, end)`` spans of every comment, captured
            during lexing so emission never re-lexes the file.
        input_bytes: Exact source-file byte size when read from disk; UTF-8
            size for in-memory input.
    """

    path: str
    relpath: str
    text: str
    drafts: List[ActionDraft] = field(default_factory=list)
    error: Optional[str] = None
    statements: int = 0
    comment_spans: List[Tuple[int, int]] = field(default_factory=list)
    input_bytes: int = 0


# ---------------------------------------------------------------------------
# Output & report
# ---------------------------------------------------------------------------


@dataclass
class SqlxFile:
    """One generated ``.sqlx`` file.

    Attributes:
        relpath: Output path relative to the output root (POSIX separators).
        content: Full file contents.
        action_type: The Dataform action type of the file.
        action_name: The Dataform action name (``config.name``).
        source_path: Source file this action came from (``""`` for
            synthesized declarations).
        source_line: 1-based source line of the originating statement.
    """

    relpath: str
    content: str
    action_type: ActionType
    action_name: str
    source_path: str = ""
    source_line: int = 0


@dataclass
class ReportWarning:
    """A single non-fatal finding.

    Attributes:
        code: Stable machine-readable code (e.g. ``FALLBACK_OPERATIONS``).
        message: Human-readable explanation.
        path: Source file the finding relates to.
        line: 1-based line number (0 when not applicable).
    """

    code: str
    message: str
    path: str = ""
    line: int = 0


@dataclass
class ConversionReport:
    """Aggregate result of a conversion run.

    Attributes:
        files_read: Number of input files parsed (including failed ones).
        statements: Number of top-level statements encountered.
        actions_by_type: Count of generated actions per Dataform type.
        refs_rewritten: Number of table references rewritten to ``ref()``.
        refs_unresolved: Distinct referenced-but-not-produced table paths
            that can be dataset-resolved, left as literals.
        warnings: All non-fatal findings.
        failures: ``path -> error`` for files that could not be converted.
        elapsed_seconds: Wall-clock duration of the run.
        input_bytes: Total size of the SQL read, in bytes.
    """

    files_read: int = 0
    statements: int = 0
    actions_by_type: Dict[str, int] = field(default_factory=dict)
    refs_rewritten: int = 0
    refs_unresolved: List[str] = field(default_factory=list)
    warnings: List[ReportWarning] = field(default_factory=list)
    failures: Dict[str, str] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    input_bytes: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation of the report."""
        d = dataclasses.asdict(self)
        d["warnings"] = [dataclasses.asdict(w) for w in self.warnings]
        return d


@dataclass
class ConversionResult:
    """Everything a conversion produces.

    Attributes:
        files: Generated ``.sqlx`` files (deterministic order: sorted by
            output path).
        report: The run's :class:`ConversionReport`.
    """

    files: List[SqlxFile]
    report: ConversionReport


_NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_]+")
_WINDOWS_RESERVED_RE = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)


def sanitize_filename(name: str) -> str:
    """Make an arbitrary action/table name safe as a file stem.

    Args:
        name: Proposed name (may contain spaces, dashes, unicode...).

    Returns:
        A non-empty stem containing only ``[A-Za-z0-9_]``.
    """
    out = _NAME_SANITIZE_RE.sub("_", name).strip("_") or "action"
    if _WINDOWS_RESERVED_RE.fullmatch(out):
        out = "_" + out
    if len(out) > 120:
        digest = hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()[:12]
        out = out[:107].rstrip("_") + "_" + digest
    return out
