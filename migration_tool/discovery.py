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

# Dependent-entities graph node types that can become migration scope rows.
GRAPH_TYPE_TO_CATEGORY = {
    "metric": "metric",
    "visualizationObject": "visualization",
    "analyticalDashboard": "dashboard",
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


def entry_point_identifiers(rules: dict[str, ReplacementRule]) -> list[dict[str, str]]:
    """Build dependentEntitiesGraph entry points from replacement rules.

    Each rule contributes its logical attribute and its display-form/label ID
    (they are often identical). Dedupes exact id+type pairs.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for rule in rules.values():
        for entity_type, entity_id in (
            ("attribute", rule.source_attribute),
            ("label", rule.source_label_id),
        ):
            key = (entity_type, entity_id)
            if not entity_id or key in seen:
                continue
            seen.add(key)
            out.append({"id": entity_id, "type": entity_type})
    out.sort(key=lambda item: (item["type"], item["id"]))
    return out


def analytics_candidates_from_graph(payload: dict[str, Any]) -> dict[str, set[str]]:
    """Extract metric/visualization/dashboard IDs from a dependentEntitiesGraph response.

    Returns ``{category: {object_id, ...}}``. Intermediate catalog nodes (datasets,
    labels, facts, …) are ignored. Callers must still content-scan candidates —
    the graph is transitive and may include objects that only depend on a source
    indirectly (e.g. dashboard → visualization → metric → attribute).
    """
    graph = payload.get("graph")
    if not isinstance(graph, dict):
        raise DiscoveryError("dependentEntitiesGraph response has no graph object")
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        raise DiscoveryError("dependentEntitiesGraph response.graph.nodes is not a list")

    out: dict[str, set[str]] = {
        "metric": set(),
        "visualization": set(),
        "dashboard": set(),
    }
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("type") or "")
        category = GRAPH_TYPE_TO_CATEGORY.get(node_type)
        if not category:
            continue
        object_id = str(node.get("id") or "")
        if object_id:
            out[category].add(object_id)
    return out


def _scan_object_occurrences(
    *,
    api: GoodDataApi,
    category: str,
    object_id: str,
    list_title: str,
    rules: dict[str, ReplacementRule],
    context_cache: dict[str, dict[str, Any] | None],
    warnings: list[str],
) -> tuple[dict[str, Any] | None, list[Occurrence]]:
    collection = COLLECTION_BY_CATEGORY[category]
    entity = api.get_entity(collection, object_id)
    if category == "metric":
        return entity, scan_metric_occurrences(entity, rules)
    if category == "visualization":
        return entity, scan_visualization_occurrences(entity, rules)

    try:
        context_id = dashboard_filter_context_id(entity)
    except TransformError as exc:
        warnings.append(f"dashboard {list_title!r} ({object_id}): {exc}")
        return None, []
    if context_id not in context_cache:
        context_cache[context_id] = api.try_get_entity("filterContexts", context_id)
    context = context_cache[context_id]
    if context is None:
        warnings.append(
            f"dashboard {entity_title(entity)!r} ({object_id}): "
            f"filterContext {context_id!r} not found"
        )
        return None, []
    return entity, scan_dashboard_filter_context_occurrences(context, rules)


def _finalize_discovered_scope(
    *,
    rows: list[DiscoveredScopeRow],
    warnings: list[str],
    output_path: Path,
) -> tuple[list[DiscoveredScopeRow], list[str]]:
    rows.sort(key=lambda r: (
        CATEGORY_ORDER.get(r.category, 99),
        r.object_title.casefold(),
        r.object_title,
        r.source_attribute,
        r.legacy_object_id,
    ))

    keys: set[tuple[str, str, str, str]] = set()
    for row in rows:
        key = (row.category, row.object_title, row.source_attribute, row.legacy_object_id)
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


def discover_workspace_scope(
    *,
    api: GoodDataApi,
    rules: dict[str, ReplacementRule],
    output_path: Path,
    native_only: bool = False,
) -> tuple[list[DiscoveredScopeRow], list[str]]:
    """Read-only discovery via Cloud dependentEntitiesGraph, then content scan.

    ``native_only`` restricts listing and scanning to objects owned by the
    workspace (``origin=NATIVE``). Use it on child workspaces: inherited objects
    are hierarchy-locked there and must be migrated in the parent instead.

    Entry points come from ``replacement-rules.csv`` (attribute + label IDs).
    Only graph candidates in metric / visualization / dashboard are GET-scanned
    with the same occurrence detectors used by plan. Transitive graph hits that
    do not contain a direct content occurrence are dropped.
    """
    rows: list[DiscoveredScopeRow] = []
    warnings: list[str] = []
    context_cache: dict[str, dict[str, Any] | None] = {}

    identifiers = entry_point_identifiers(rules)
    print("DISCOVER — READ-ONLY (dependentEntitiesGraph + content scan)")
    print(f"Host:         {api.host}")
    print(f"Workspace:    {api.workspace}")
    print(f"Objects:      {'NATIVE only (inherited skipped)' if native_only else 'ALL (native + inherited)'}")
    print(f"Rules:        {len(rules)} known legacy source(s)")
    print(f"Entry points: {len(identifiers)} attribute/label identifier(s)")
    print()

    print("Querying dependentEntitiesGraph…")
    graph_payload = api.dependent_entities_graph(identifiers)
    candidates = analytics_candidates_from_graph(graph_payload)
    total_candidates = sum(len(ids) for ids in candidates.values())
    print(
        "Graph candidates: "
        f"metrics={len(candidates['metric'])}, "
        f"visualizations={len(candidates['visualization'])}, "
        f"dashboards={len(candidates['dashboard'])} "
        f"(total {total_candidates})"
    )
    print()

    for category in ("metric", "visualization", "dashboard"):
        object_ids = sorted(candidates[category])
        if not object_ids:
            print(f"Scanning {COLLECTION_BY_CATEGORY[category]}: 0 graph candidate(s) — skip")
            continue

        # Title uniqueness still needs the full workspace list for this category
        # (plan looks up by exact title across the workspace).
        listed = api.list_entities(
            COLLECTION_BY_CATEGORY[category], origin="NATIVE" if native_only else None
        )
        title_counts = _title_counts(listed)
        if native_only:
            native_ids = {str(x.get("id") or "") for x in listed}
            skipped = len(object_ids)
            object_ids = [i for i in object_ids if i in native_ids]
            skipped -= len(object_ids)
        else:
            skipped = 0
        print(
            f"Scanning {COLLECTION_BY_CATEGORY[category]}: "
            f"{len(object_ids)} graph candidate(s) "
            f"(workspace has {len(listed)} object(s)"
            + (f", {skipped} inherited candidate(s) skipped)" if native_only else ")")
        )

        for object_id in object_ids:
            try:
                entity, occurrences = _scan_object_occurrences(
                    api=api,
                    category=category,
                    object_id=object_id,
                    list_title=object_id,
                    rules=rules,
                    context_cache=context_cache,
                    warnings=warnings,
                )
                if entity is None or not occurrences:
                    continue

                title = entity_title(entity)
                if title_counts.get(title, 0) > 1:
                    sources = ", ".join(sorted(aggregate_occurrences(occurrences)))
                    warnings.append(
                        f"{category} title {title!r} is ambiguous ({title_counts[title]} exact objects); "
                        f"emitting by object id {object_id} for source(s) {sources}"
                    )

                rows.extend(_rows_for_entity(
                    workspace=api.workspace,
                    category=category,
                    entity=entity,
                    occurrences=occurrences,
                ))
            except Exception as exc:
                warnings.append(f"{category} ({object_id}): scan failed: {exc}")

        print("  done")

    return _finalize_discovered_scope(rows=rows, warnings=warnings, output_path=output_path)
