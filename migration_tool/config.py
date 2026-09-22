from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


GRANULARITY_SUFFIX = {
    "SECOND": "second",
    "SECOND_OF_DAY": "secondOfDay",
    "MINUTE": "minute",
    "MINUTE_OF_DAY": "minuteOfDay",
    "HOUR": "hour",
    "HOUR_OF_DAY": "hourOfDay",
    "DAY": "day",
    "DAY_OF_WEEK": "dayOfWeek",
    "WEEK": "week",
    "MONTH": "month",
    "QUARTER": "quarter",
    "YEAR": "year",
}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReplacementRule:
    source_attribute: str
    target_date_dimension: str
    visual_default_granularity: str
    visual_default_title: str
    day_reuse_enabled: bool
    day_reuse_granularity: str | None
    day_reuse_title: str | None
    metric_granularity: str
    source_label: str | None = None
    visual_default_label: str | None = None
    day_reuse_label: str | None = None
    metric_label: str | None = None

    def _label(self, explicit: str | None, granularity: str | None) -> str:
        if explicit:
            return explicit.removeprefix("label/")
        if not granularity:
            raise ConfigError(
                f"Rule {self.source_attribute}: cannot derive label without granularity"
            )
        try:
            suffix = GRANULARITY_SUFFIX[granularity]
        except KeyError as exc:
            raise ConfigError(
                f"Rule {self.source_attribute}: unknown granularity {granularity!r}; "
                "add an explicit label column to replacement-rules.csv"
            ) from exc
        return f"{self.target_date_dimension}.{suffix}"

    @property
    def source_label_id(self) -> str:
        """Exact GoodData display-form/label ID used in visualizations and filters.

        `source_attribute` remains the logical whitelist/mapping key. Most legacy attributes
        use the same identifier for their primary label, but some do not.
        """
        return (self.source_label or self.source_attribute).removeprefix("label/")

    @property
    def visual_default_label_id(self) -> str:
        return self._label(self.visual_default_label, self.visual_default_granularity)

    @property
    def day_reuse_label_id(self) -> str:
        return self._label(self.day_reuse_label, self.day_reuse_granularity)

    @property
    def metric_label_id(self) -> str:
        return self._label(self.metric_label, self.metric_granularity)


@dataclass(frozen=True)
class ScopeRow:
    row_number: int
    source_workspace_id: str
    source_attribute: str
    object_title: str
    legacy_object_id: str
    legacy_object_type: str
    category: str

    @property
    def object_identity(self) -> str:
        """Stable key for grouping one analytical object across scope rows.

        Prefer ``legacy_object_id`` when present (Cloud Entity ID from discover, or
        Platform provenance ID from exports). Fall back to title only when the ID
        column is empty.
        """
        return self.legacy_object_id or self.object_title

    @property
    def object_key(self) -> tuple[str, str]:
        return self.category, self.object_identity


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y"}


def load_rules(path: Path) -> dict[str, ReplacementRule]:
    if not path.exists():
        raise ConfigError(f"Replacement rules file not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        required = {
            "source_attribute",
            "target_date_dimension",
            "visual_default_granularity",
            "visual_default_title",
            "day_reuse_enabled",
            "day_reuse_granularity",
            "day_reuse_title",
            "metric_granularity",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ConfigError(
                f"Replacement rules file is missing columns: {sorted(missing)}"
            )

        out: dict[str, ReplacementRule] = {}
        for n, row in enumerate(reader, start=2):
            source = (row.get("source_attribute") or "").strip()
            if not source:
                raise ConfigError(f"Replacement rules row {n}: source_attribute is empty")
            if source in out:
                raise ConfigError(f"Duplicate replacement rule for {source!r}")

            rule = ReplacementRule(
                source_attribute=source,
                source_label=(row.get("source_label") or "").strip() or None,
                target_date_dimension=(row.get("target_date_dimension") or "").strip(),
                visual_default_granularity=(row.get("visual_default_granularity") or "").strip(),
                visual_default_title=(row.get("visual_default_title") or "").strip(),
                day_reuse_enabled=_truthy(row.get("day_reuse_enabled") or ""),
                day_reuse_granularity=(row.get("day_reuse_granularity") or "").strip() or None,
                day_reuse_title=(row.get("day_reuse_title") or "").strip() or None,
                metric_granularity=(row.get("metric_granularity") or "").strip(),
                visual_default_label=(row.get("visual_default_label") or "").strip() or None,
                day_reuse_label=(row.get("day_reuse_label") or "").strip() or None,
                metric_label=(row.get("metric_label") or "").strip() or None,
            )
            if not rule.target_date_dimension:
                raise ConfigError(f"Replacement rules row {n}: target_date_dimension is empty")
            if rule.day_reuse_enabled and (
                not rule.day_reuse_granularity or not rule.day_reuse_title
            ):
                raise ConfigError(
                    f"Replacement rules row {n}: day_reuse_enabled requires "
                    "day_reuse_granularity and day_reuse_title"
                )
            # Force derivation at load time so bad mappings fail early.
            _ = rule.visual_default_label_id
            _ = rule.metric_label_id
            if rule.day_reuse_enabled:
                _ = rule.day_reuse_label_id
            out[source] = rule

    return out


def _normalize_source_attribute(value: str) -> str:
    """Normalize the source ID used by the migration rules.

    Raw GoodData Platform exports prefix logical attribute IDs with ``attr.``.
    The replacement-rules file intentionally stores the logical ID without that
    transport/export prefix.
    """
    source = value.strip()
    return source.removeprefix("attr.")


_PLATFORM_OBJECT_TYPE_TO_CATEGORY = {
    "metric": "metric",
    "visualizationobject": "visualization",
    "analyticaldashboard": "dashboard",
}


def _scope_row_from_values(
    *,
    row_number: int,
    source_workspace_id: str,
    source_attribute: str,
    object_title: str,
    legacy_object_id: str,
    legacy_object_type: str,
    category: str,
    rules: dict[str, ReplacementRule],
    seen: set[tuple[str, str, str]],
) -> ScopeRow:
    source = _normalize_source_attribute(source_attribute)
    title = object_title.strip()
    normalized_category = category.strip().lower()
    object_id = legacy_object_id.strip()

    if source not in rules:
        raise ConfigError(
            f"Workspace scope row {row_number}: no replacement rule for {source!r}"
        )
    if normalized_category not in {"metric", "visualization", "dashboard"}:
        raise ConfigError(
            f"Workspace scope row {row_number}: unsupported category {normalized_category!r}"
        )
    if not title:
        raise ConfigError(f"Workspace scope row {row_number}: object_title is empty")

    # Deduplicate per concrete object (ID when present) + source, so multiple
    # Cloud clones that share a title can all be whitelisted.
    identity = object_id or title
    key = (normalized_category, identity, source)
    if key in seen:
        raise ConfigError(
            f"Workspace scope row {row_number}: duplicate whitelist pair {key!r}"
        )
    seen.add(key)

    return ScopeRow(
        row_number=row_number,
        source_workspace_id=source_workspace_id.strip(),
        source_attribute=source,
        object_title=title,
        legacy_object_id=object_id,
        legacy_object_type=legacy_object_type.strip(),
        category=normalized_category,
    )


def _load_normalized_scope(
    path: Path,
    rules: dict[str, ReplacementRule],
) -> list[ScopeRow]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        fields = set(reader.fieldnames or [])
        category_column = (
            "object_category"
            if "object_category" in fields
            else "aac_lookup_category"
            if "aac_lookup_category" in fields
            else None
        )
        required = {
            "source_attribute",
            "object_title",
            "legacy_object_id",
            "legacy_object_type",
        }
        missing = required - fields
        if missing or not category_column:
            if not category_column:
                missing.add("object_category OR aac_lookup_category")
            raise ConfigError(f"Workspace scope file is missing columns: {sorted(missing)}")

        out: list[ScopeRow] = []
        seen: set[tuple[str, str, str]] = set()
        for n, row in enumerate(reader, start=2):
            out.append(
                _scope_row_from_values(
                    row_number=n,
                    source_workspace_id=row.get("source_workspace_id") or "",
                    source_attribute=row.get("source_attribute") or "",
                    object_title=row.get("object_title") or "",
                    legacy_object_id=row.get("legacy_object_id") or "",
                    legacy_object_type=row.get("legacy_object_type") or "",
                    category=row.get(category_column) or "",
                    rules=rules,
                    seen=seen,
                )
            )
        return out


def _load_platform_export_scope(
    path: Path,
    rules: dict[str, ReplacementRule],
) -> list[ScopeRow]:
    """Load the headerless six-column GoodData Platform Used-By export.

    Expected columns, in order:

    1. source Platform workspace ID
    2. source attribute ID (normally prefixed with ``attr.``)
    3. object title
    4. legacy Platform object ID
    5. legacy object type (metric / visualizationObject / analyticalDashboard)
    6. legacy audit/status value (yes/no/n/a) -- intentionally ignored

    Only columns 1-5 participate in migration configuration. Column 6 is accepted
    so the original Platform export can be used unchanged, but its value has no
    effect on discovery, planning, transformation, or apply.
    """
    out: list[ScopeRow] = []
    seen: set[tuple[str, str, str]] = set()

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        for n, row in enumerate(reader, start=1):
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(row) != 6:
                raise ConfigError(
                    f"Workspace scope row {n}: headerless Platform export must have exactly "
                    f"6 semicolon-separated columns, got {len(row)}"
                )

            (
                source_workspace_id,
                source_attribute,
                object_title,
                legacy_object_id,
                legacy_object_type,
                _ignored_platform_value,
            ) = row

            category = _PLATFORM_OBJECT_TYPE_TO_CATEGORY.get(
                legacy_object_type.strip().lower()
            )
            if category is None:
                raise ConfigError(
                    f"Workspace scope row {n}: unsupported Platform object type "
                    f"{legacy_object_type.strip()!r}; expected metric, visualizationObject, "
                    "or analyticalDashboard"
                )

            out.append(
                _scope_row_from_values(
                    row_number=n,
                    source_workspace_id=source_workspace_id,
                    source_attribute=source_attribute,
                    object_title=object_title,
                    legacy_object_id=legacy_object_id,
                    legacy_object_type=legacy_object_type,
                    category=category,
                    rules=rules,
                    seen=seen,
                )
            )

    if not out:
        raise ConfigError(f"Workspace scope file is empty: {path}")
    return out


def load_scope(path: Path, rules: dict[str, ReplacementRule]) -> list[ScopeRow]:
    if not path.exists():
        raise ConfigError(f"Workspace scope file not found: {path}")

    # Auto-detect the existing normalized/headered scope vs the unchanged
    # headerless GoodData Platform export. This keeps V1.3/V1.4 inputs working
    # while allowing the Platform export to be passed directly to --scope.
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        first_nonempty: list[str] | None = None
        for row in reader:
            if row and any(cell.strip() for cell in row):
                first_nonempty = [cell.strip() for cell in row]
                break

    if first_nonempty is None:
        raise ConfigError(f"Workspace scope file is empty: {path}")

    header_fields = set(first_nonempty)
    if "source_attribute" in header_fields and "object_title" in header_fields:
        return _load_normalized_scope(path, rules)

    return _load_platform_export_scope(path, rules)


def group_scope(rows: Iterable[ScopeRow]) -> dict[tuple[str, str], list[ScopeRow]]:
    out: dict[tuple[str, str], list[ScopeRow]] = {}
    for row in rows:
        out.setdefault(row.object_key, []).append(row)
    return out
