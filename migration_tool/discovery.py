from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .api import (
    COLLECTION_BY_CATEGORY,
    GoodDataApi,
    entity_content,
    entity_id,
    entity_title,
    entity_type,
)
from .config import ReplacementRule
from .transform import (
    TransformError,
    attribute_filter_source,
    dashboard_filter_context_id,
    display_form_id,
    filter_rows,
    iter_attribute_items,
)


class DiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Occurrence:
    source_attribute: str
    source_label: str
    kind: str
    count: int = 1


@dataclass(frozen=True)
class DiscoveredScopeRow:
    source_workspace_id: str
    source_attribute: str
    object_title: str
    legacy_object_id: str
    legacy_object_type: str
    category: str
    occurrence_types: tuple[str, ...]
    occurrence_count: int


CATEGORY_ORDER = {
    "metric": 0,
    "visualization": 1,
    "dashboard": 2,
}


def _rule_by_source_label(rules: dict[str, ReplacementRule]) -> dict[str, ReplacementRule]:
    out: dict[str, ReplacementRule] = {}
    for rule in rules.values():
        label = rule.source_label_id
        previous = out.get(label)
        if previous and previous.source_attribute != rule.source_attribute:
            raise DiscoveryError(
                f"Cannot discover safely: source label {label!r} belongs to more than one rule: "
                f"{previous.source_attribute!r}, {rule.source_attribute!r}"
            )
        out[label] = rule
    return out


def scan_metric_occurrences(
    entity: dict[str, Any],
    rules: dict[str, ReplacementRule],
) -> list[Occurrence]:
    content = entity_content(entity)
    maql = content.get("maql")
    if not isinstance(maql, str):
        return []

    out: list[Occurrence] = []
    for rule in rules.values():
        label_token = f"{{label/{rule.source_label_id}}}"
        attribute_token = f"{{attribute/{rule.source_attribute}}}"
        label_count = maql.count(label_token)
        attribute_count = maql.count(attribute_token)
        if label_count:
            out.append(Occurrence(rule.source_attribute, rule.source_label_id, "metric_maql_label", label_count))
        if attribute_count:
            out.append(Occurrence(rule.source_attribute, rule.source_label_id, "metric_maql_attribute", attribute_count))
    return out


def scan_visualization_occurrences(
    entity: dict[str, Any],
    rules: dict[str, ReplacementRule],
) -> list[Occurrence]:
    content = entity_content(entity)
    by_label = _rule_by_source_label(rules)
    counts: dict[tuple[str, str], int] = {}

    for _, _, _, _, attr in iter_attribute_items(content):
        label = display_form_id(attr)
        rule = by_label.get(label or "")
        if rule:
            key = (rule.source_attribute, "visualization_field")
            counts[key] = counts.get(key, 0) + 1

    for _, wrapper in filter_rows(content):
        label, _ = attribute_filter_source(wrapper)
        rule = by_label.get(label or "")
        if rule:
            key = (rule.source_attribute, "visualization_filter")
            counts[key] = counts.get(key, 0) + 1

    return [
        Occurrence(source, rules[source].source_label_id, kind, count)
        for (source, kind), count in sorted(counts.items())
    ]


def _dashboard_attribute_filter_source(wrapper: dict[str, Any]) -> tuple[str | None, str | None]:
    body = wrapper.get("attributeFilter")
    if isinstance(body, dict):
        return display_form_id(body), (
            str(body.get("localIdentifier")) if body.get("localIdentifier") is not None else None
        )
    return attribute_filter_source(wrapper)


def scan_dashboard_filter_context_occurrences(
    filter_context_entity: dict[str, Any],
    rules: dict[str, ReplacementRule],
) -> list[Occurrence]:
    content = entity_content(filter_context_entity)
    by_label = _rule_by_source_label(rules)
    counts: dict[str, int] = {}
    for _, wrapper in filter_rows(content):
        label, _ = _dashboard_attribute_filter_source(wrapper)
        rule = by_label.get(label or "")
        if rule:
            counts[rule.source_attribute] = counts.get(rule.source_attribute, 0) + 1
    return [
        Occurrence(source, rules[source].source_label_id, "dashboard_filter", count)
        for source, count in sorted(counts.items())
    ]


def aggregate_occurrences(occurrences: Iterable[Occurrence]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for occurrence in occurrences:
        row = out.setdefault(
            occurrence.source_attribute,
            {"occurrence_types": set(), "occurrence_count": 0},
        )
        row["occurrence_types"].add(occurrence.kind)
        row["occurrence_count"] += occurrence.count
    return out


def unscoped_occurrences(
    occurrences: Iterable[Occurrence],
    scoped_sources: set[str],
) -> list[dict[str, Any]]:
    aggregated = aggregate_occurrences(occurrences)
    out: list[dict[str, Any]] = []
    for source in sorted(aggregated):
        if source in scoped_sources:
            continue
        info = aggregated[source]
        out.append({
            "source_attribute": source,
            "occurrence_types": sorted(info["occurrence_types"]),
            "occurrence_count": int(info["occurrence_count"]),
        })
    return out


def _title_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for data in items:
        attrs = data.get("attributes")
        title = str(attrs.get("title") or "") if isinstance(attrs, dict) else ""
        counts[title] = counts.get(title, 0) + 1
    return counts


def _rows_for_entity(
    *,
    workspace: str,
    category: str,
    entity: dict[str, Any],
    occurrences: list[Occurrence],
) -> list[DiscoveredScopeRow]:
    aggregated = aggregate_occurrences(occurrences)
    rows: list[DiscoveredScopeRow] = []
    for source, info in aggregated.items():
        rows.append(DiscoveredScopeRow(
            source_workspace_id=workspace,
            source_attribute=source,
            object_title=entity_title(entity),
            legacy_object_id=entity_id(entity),
            legacy_object_type=entity_type(entity),
            category=category,
            occurrence_types=tuple(sorted(info["occurrence_types"])),
            occurrence_count=int(info["occurrence_count"]),
        ))
    return rows


def write_discovered_scope(path: Path, rows: list[DiscoveredScopeRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "source_workspace_id",
        "source_attribute",
        "object_title",
        "legacy_object_id",
        "legacy_object_type",
        "aac_lookup_category",
        "occurrence_types",
        "occurrence_count",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, delimiter=";", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "source_workspace_id": row.source_workspace_id,
                "source_attribute": row.source_attribute,
                "object_title": row.object_title,
                "legacy_object_id": row.legacy_object_id,
                "legacy_object_type": row.legacy_object_type,
                "aac_lookup_category": row.category,
                "occurrence_types": ",".join(row.occurrence_types),
                "occurrence_count": row.occurrence_count,
            })


def discover_workspace_scope(
    *,
    api: GoodDataApi,
    rules: dict[str, ReplacementRule],
    output_path: Path,
) -> tuple[list[DiscoveredScopeRow], list[str]]:
    """Read-only discovery of all known legacy occurrences in supported object types."""
    rows: list[DiscoveredScopeRow] = []
    warnings: list[str] = []
    context_cache: dict[str, dict[str, Any] | None] = {}

    print("DISCOVER — READ-ONLY WORKSPACE SCAN (NO WRITES)")
    print(f"Host:      {api.host}")
    print(f"Workspace: {api.workspace}")
    print(f"Rules:     {len(rules)} known legacy source(s)")
    print()

    for category in ("metric", "visualization", "dashboard"):
        collection = COLLECTION_BY_CATEGORY[category]
        listed = api.list_entities(collection)
        title_counts = _title_counts(listed)
        print(f"Scanning {collection}: {len(listed)} effective object(s)")

        for index, data in enumerate(listed, start=1):
            object_id = str(data.get("id") or "")
            attrs = data.get("attributes")
            list_title = str(attrs.get("title") or "") if isinstance(attrs, dict) else ""
            if not object_id:
                warnings.append(f"{category}: list item without ID was skipped")
                continue

            try:
                entity = api.get_entity(collection, object_id)
                if category == "metric":
                    occurrences = scan_metric_occurrences(entity, rules)
                elif category == "visualization":
                    occurrences = scan_visualization_occurrences(entity, rules)
                else:
                    try:
                        context_id = dashboard_filter_context_id(entity)
                    except TransformError as exc:
                        warnings.append(f"dashboard {list_title!r} ({object_id}): {exc}")
                        continue
                    if context_id not in context_cache:
                        context_cache[context_id] = api.try_get_entity("filterContexts", context_id)
                    context = context_cache[context_id]
                    if context is None:
                        warnings.append(
                            f"dashboard {entity_title(entity)!r} ({object_id}): "
                            f"filterContext {context_id!r} not found"
                        )
                        continue
                    occurrences = scan_dashboard_filter_context_occurrences(context, rules)

                if not occurrences:
                    continue

                title = entity_title(entity)
                if title_counts.get(title, 0) > 1:
                    sources = ", ".join(sorted(aggregate_occurrences(occurrences)))
                    warnings.append(
                        f"{category} title {title!r} is ambiguous ({title_counts[title]} exact objects); "
                        f"discovered source(s) {sources} were NOT emitted to scope"
                    )
                    continue

                rows.extend(_rows_for_entity(
                    workspace=api.workspace,
                    category=category,
                    entity=entity,
                    occurrences=occurrences,
                ))
            except Exception as exc:
                warnings.append(f"{category} {list_title!r} ({object_id}): scan failed: {exc}")

        print(f"  done")

    # Exact deterministic order; one row per object + logical legacy source.
    rows.sort(key=lambda r: (
        CATEGORY_ORDER.get(r.category, 99),
        r.object_title.casefold(),
        r.object_title,
        r.source_attribute,
        r.legacy_object_id,
    ))

    # A generated scope must be loadable by the strict whitelist parser.
    keys: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (row.category, row.object_title, row.source_attribute)
        if key in keys:
            raise DiscoveryError(f"Internal discovery duplicate scope key: {key!r}")
        keys.add(key)

    write_discovered_scope(output_path, rows)

    warning_path = output_path.with_suffix(output_path.suffix + ".warnings.txt")
    if warnings:
        warning_path.write_text("\n".join(warnings) + "\n", encoding="utf-8")
    elif warning_path.exists():
        warning_path.unlink()

    print()
    print("DISCOVERY SUMMARY")
    print(f"Generated scope rows: {len(rows)}")
    print(f"Warnings:             {len(warnings)}")
    print(f"Scope:                {output_path}")
    if warnings:
        print(f"Warnings file:        {warning_path}")
    print("No GoodData write was executed.")
    return rows, warnings
