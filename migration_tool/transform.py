from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Iterable

from .api import entity_content, put_payload_from_entity
from .config import ReplacementRule, ScopeRow


class TransformError(RuntimeError):
    pass


@dataclass
class RowOutcome:
    row_number: int
    source_attribute: str
    status: str
    detail: str
    legacy_object_id: str = ""


@dataclass
class TransformResult:
    changed: bool
    proposed: dict[str, Any] | None
    outcomes: list[RowOutcome]
    checks: list[dict[str, Any]] = field(default_factory=list)
    blocked: bool = False
    block_reason: str | None = None
    # Dashboard migrations can legitimately require two scoped entity writes:
    # the analyticalDashboard metadata/config and its dedicated filterContext.
    # Each planned write contains: collection, backup, proposed, checks.
    planned_writes: list[dict[str, Any]] = field(default_factory=list)


def display_form_id(attribute: dict[str, Any]) -> str | None:
    value = ((((attribute.get("displayForm") or {}).get("identifier")) or {}).get("id"))
    return str(value) if value is not None else None


def set_display_form_id(attribute: dict[str, Any], label_id: str) -> None:
    display_form = attribute.setdefault("displayForm", {})
    if not isinstance(display_form, dict):
        raise TransformError("Attribute displayForm is not an object")
    identifier = display_form.setdefault("identifier", {})
    if not isinstance(identifier, dict):
        raise TransformError("Attribute displayForm.identifier is not an object")
    identifier["id"] = label_id
    identifier.setdefault("type", "label")


def iter_attribute_items(content: dict[str, Any]) -> Iterable[tuple[int, int, str, dict[str, Any], dict[str, Any]]]:
    buckets = content.get("buckets") or []
    if not isinstance(buckets, list):
        raise TransformError("Visualization content.buckets is not a list")
    for bi, bucket in enumerate(buckets):
        if not isinstance(bucket, dict):
            continue
        items = bucket.get("items") or []
        if not isinstance(items, list):
            continue
        for ii, wrapper in enumerate(items):
            if not isinstance(wrapper, dict):
                continue
            for key in ("attribute", "visualizationAttribute"):
                attr = wrapper.get(key)
                if isinstance(attr, dict):
                    yield bi, ii, key, wrapper, attr
                    break


def items_by_label(content: dict[str, Any], label_id: str) -> list[tuple[int, int, str, dict[str, Any], dict[str, Any]]]:
    return [row for row in iter_attribute_items(content) if display_form_id(row[4]) == label_id]


def items_by_local_id(content: dict[str, Any], local_id: str) -> list[tuple[int, int, str, dict[str, Any], dict[str, Any]]]:
    return [
        row for row in iter_attribute_items(content)
        if str(row[4].get("localIdentifier") or "") == local_id
    ]


def target_dimension_items(content: dict[str, Any], dataset_id: str) -> list[tuple[int, int, str, dict[str, Any], dict[str, Any]]]:
    prefix = f"{dataset_id}."
    return [
        row for row in iter_attribute_items(content)
        if (display_form_id(row[4]) or "").startswith(prefix)
    ]


def remove_item(content: dict[str, Any], bucket_index: int, item_index: int) -> None:
    buckets = content.get("buckets")
    if not isinstance(buckets, list):
        raise TransformError("Visualization content.buckets is not a list")
    bucket = buckets[bucket_index]
    items = bucket.get("items") if isinstance(bucket, dict) else None
    if not isinstance(items, list):
        raise TransformError("Visualization bucket.items is not a list")
    del items[item_index]


def apply_alias(attribute: dict[str, Any], title: str) -> None:
    attribute["alias"] = title


def json_contains_token(value: Any, token: str) -> bool:
    if isinstance(value, dict):
        return any(
            json_contains_token(k, token) or json_contains_token(v, token)
            for k, v in value.items()
        )
    if isinstance(value, list):
        return any(json_contains_token(v, token) for v in value)
    if isinstance(value, str):
        return token in value
    return False


def remap_exact_string_tokens(value: Any, old: str, new: str) -> int:
    """Replace exact string values ``old`` with ``new`` anywhere in a JSON tree.

    Dict keys are left unchanged. Returns the number of replaced values.
    """
    if not old or old == new:
        return 0
    replaced = 0
    if isinstance(value, dict):
        for key, child in list(value.items()):
            if isinstance(child, str):
                if child == old:
                    value[key] = new
                    replaced += 1
            else:
                replaced += remap_exact_string_tokens(child, old, new)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if isinstance(child, str):
                if child == old:
                    value[index] = new
                    replaced += 1
            else:
                replaced += remap_exact_string_tokens(child, old, new)
    return replaced


def filter_rows(content: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    filters = content.get("filters") or []
    if not isinstance(filters, list):
        raise TransformError("content.filters is not a list")
    return [(i, f) for i, f in enumerate(filters) if isinstance(f, dict)]


def attribute_filter_source(wrapper: dict[str, Any]) -> tuple[str | None, str | None]:
    for key in ("negativeAttributeFilter", "positiveAttributeFilter"):
        body = wrapper.get(key)
        if not isinstance(body, dict):
            continue
        return display_form_id(body), (
            str(body.get("localIdentifier")) if body.get("localIdentifier") is not None else None
        )
    return None, None


def date_filter_dataset(wrapper: dict[str, Any]) -> tuple[str | None, str | None, bool]:
    body = wrapper.get("relativeDateFilter")
    if not isinstance(body, dict):
        return None, None, False
    ds = ((((body.get("dataSet") or {}).get("identifier")) or {}).get("id"))
    if ds is None:
        return None, None, False
    unrestricted = "from" not in body and "to" not in body
    gran = body.get("granularity")
    return str(ds), str(gran) if gran is not None else None, unrestricted


def make_unrestricted_date_filter(dataset_id: str) -> dict[str, Any]:
    # Verified against a GoodData UI-created unrestricted filter in this migration:
    # API entity form uses GDC.time.year and omits from/to.
    return {
        "relativeDateFilter": {
            "dataSet": {
                "identifier": {
                    "id": dataset_id,
                    "type": "dataset",
                }
            },
            "granularity": "GDC.time.year",
        }
    }


def _row_outcome(row: ScopeRow, status: str, detail: str) -> RowOutcome:
    return RowOutcome(
        row_number=row.row_number,
        source_attribute=row.source_attribute,
        status=status,
        detail=detail,
        legacy_object_id=row.legacy_object_id,
    )


def transform_metric(
    entity: dict[str, Any],
    rows: list[ScopeRow],
    rules: dict[str, ReplacementRule],
) -> TransformResult:
    proposed = put_payload_from_entity(entity)
    content = entity_content(proposed)
    maql = content.get("maql")
    if not isinstance(maql, str):
        outcomes = [_row_outcome(r, "BLOCKED", "Metric content.maql is not a string") for r in rows]
        return TransformResult(False, None, outcomes, blocked=True, block_reason="Invalid metric MAQL")

    outcomes: list[RowOutcome] = []
    checks: list[dict[str, Any]] = []
    changed = False

    for row in rows:
        rule = rules[row.source_attribute]
        source_label_id = rule.source_label_id
        source_forms = [
            "{" + f"label/{source_label_id}" + "}",
            "{" + f"attribute/{row.source_attribute}" + "}",
        ]
        count = sum(maql.count(x) for x in source_forms)
        if count == 0:
            outcomes.append(_row_outcome(row, "NO_OCCURRENCE", "No exact source reference in metric MAQL"))
            continue

        target = "{" + f"label/{rule.metric_label_id}" + "}"
        for source_form in source_forms:
            maql = maql.replace(source_form, target)
        changed = True
        outcomes.append(
            _row_outcome(
                row,
                "READY",
                f"Replace {count} MAQL reference(s) with {target}",
            )
        )
        checks.append({
            "kind": "metric_maql",
            "source_attribute": row.source_attribute,
            "source_label_id": rule.source_label_id,
            "target_label_id": rule.metric_label_id,
            "expected_maql": maql,
        })

    content["maql"] = maql
    return TransformResult(changed, proposed if changed else None, outcomes, checks)


def _convert_source_filter(
    content: dict[str, Any],
    row: ScopeRow,
    rule: ReplacementRule,
    *,
    dependent_container: dict[str, Any] | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    matching: list[tuple[int, dict[str, Any], str | None]] = []
    existing_same_dataset_dates: list[tuple[int, str | None, bool]] = []

    for index, wrapper in filter_rows(content):
        src, local_id = attribute_filter_source(wrapper)
        if src == rule.source_label_id:
            matching.append((index, wrapper, local_id))
        ds, gran, unrestricted = date_filter_dataset(wrapper)
        if ds == rule.target_date_dimension:
            existing_same_dataset_dates.append((index, gran, unrestricted))

    if not matching:
        return False, "No source attribute filter", None
    if len(matching) != 1:
        raise TransformError(
            f"{row.source_attribute}: found {len(matching)} source attribute filters; expected exactly one"
        )

    # Avoid duplicate/same-dataset date-filter semantics that we have not tested.
    if existing_same_dataset_dates:
        raise TransformError(
            f"{row.source_attribute}: target date dataset {rule.target_date_dimension} already has "
            "a date filter in this object; existing date filters are immutable and duplicate semantics are unsafe"
        )

    index, _, local_id = matching[0]

    configs = content.get("attributeFilterConfigs") or {}
    if local_id and isinstance(configs, dict) and local_id in configs:
        raise TransformError(
            f"{row.source_attribute}: source filter {local_id} has attributeFilterConfigs"
        )
    if local_id and dependent_container is not None and json_contains_token(dependent_container, local_id):
        raise TransformError(
            f"{row.source_attribute}: source dashboard filter {local_id} is referenced by dashboard content/config"
        )

    content["filters"][index] = make_unrestricted_date_filter(rule.target_date_dimension)
    check = {
        "kind": "unrestricted_date_filter",
        "source_attribute": row.source_attribute,
        "source_label_id": rule.source_label_id,
        "target_date_dimension": rule.target_date_dimension,
        "api_granularity": "GDC.time.year",
    }
    return True, (
        f"Convert attribute filter to unrestricted date filter on {rule.target_date_dimension}"
    ), check


def _preflight_reuse_claims(
    original_content: dict[str, Any],
    rows: list[ScopeRow],
    rules: dict[str, ReplacementRule],
) -> None:
    claims: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        rule = rules[row.source_attribute]
        if not rule.day_reuse_enabled:
            continue
        source_fields = items_by_label(original_content, rule.source_label_id)
        if not source_fields:
            continue
        day_fields = items_by_label(original_content, f"{rule.target_date_dimension}.day")
        if len(day_fields) > 1:
            raise TransformError(
                f"{row.source_attribute}: more than one existing {rule.target_date_dimension}.day field"
            )
        if len(day_fields) == 1:
            local_id = str(day_fields[0][4].get("localIdentifier") or "")
            key = (rule.target_date_dimension, local_id)
            claims.setdefault(key, []).append(row.source_attribute)
    collisions = {k: v for k, v in claims.items() if len(v) > 1}
    if collisions:
        detail = "; ".join(f"{k}: {v}" for k, v in collisions.items())
        raise TransformError(f"Multiple scoped sources would reuse the same DAY field: {detail}")


def _transform_visual_field(
    content: dict[str, Any],
    original_content: dict[str, Any],
    row: ScopeRow,
    rule: ReplacementRule,
) -> tuple[bool, str, dict[str, Any] | None]:
    source_fields = items_by_label(content, rule.source_label_id)
    if not source_fields:
        return False, "No source visualization field", None

    target_label = rule.visual_default_label_id
    title = rule.visual_default_title

    if not rule.day_reuse_enabled:
        for _, _, _, _, attr in source_fields:
            set_display_form_id(attr, target_label)
            apply_alias(attr, title)
        return True, (
            f"Replace {len(source_fields)} field occurrence(s) with label/{target_label}"
        ), {
            "kind": "visual_field",
            "source_attribute": row.source_attribute,
            "source_label_id": rule.source_label_id,
            "target_label_id": target_label,
            "target_local_ids": [str(x[4].get("localIdentifier") or "") for x in source_fields],
        }

    original_days = items_by_label(original_content, f"{rule.target_date_dimension}.day")
    if len(original_days) > 1:
        raise TransformError(
            f"{row.source_attribute}: more than one existing {rule.target_date_dimension}.day field"
        )

    if len(original_days) == 1:
        # Case B. Multiple source fields collapsing into one DAY field is intentionally blocked
        # until a real workspace demonstrates that this is desired.
        if len(source_fields) != 1:
            raise TransformError(
                f"{row.source_attribute}: Case B has {len(source_fields)} source fields; "
                "automatic many-to-one collapse is unsafe"
            )
        day_local_id = str(original_days[0][4].get("localIdentifier") or "")
        if not day_local_id:
            raise TransformError(f"{row.source_attribute}: reusable DAY field has no localIdentifier")
        current_day_rows = items_by_local_id(content, day_local_id)
        if len(current_day_rows) != 1:
            raise TransformError(
                f"{row.source_attribute}: reusable DAY localIdentifier {day_local_id!r} is not unique"
            )
        day_attr = current_day_rows[0][4]
        set_display_form_id(day_attr, rule.day_reuse_label_id)
        apply_alias(day_attr, rule.day_reuse_title or "")

        source_attr = source_fields[0][4]
        source_local_id = str(source_attr.get("localIdentifier") or "")
        if not source_local_id:
            raise TransformError(f"{row.source_attribute}: source field has no localIdentifier")
        source_rows = items_by_local_id(content, source_local_id)
        source_rows = [x for x in source_rows if display_form_id(x[4]) == rule.source_label_id]
        if len(source_rows) != 1:
            raise TransformError(
                f"{row.source_attribute}: source localIdentifier {source_local_id!r} is not uniquely removable"
            )
        bi, ii, _, _, _ = source_rows[0]
        remove_item(content, bi, ii)
        # Case B collapses two fields into one localIdentifier. Sorts / columnWidths /
        # properties often still point at the removed source id — remap those exact
        # references onto the reused DAY field instead of blocking.
        remapped = remap_exact_string_tokens(content, source_local_id, day_local_id)
        if json_contains_token(content, source_local_id):
            raise TransformError(
                f"{row.source_attribute}: removed source field localIdentifier {source_local_id} "
                "is still referenced elsewhere (sort/config/bucket dependency) after remap "
                f"to {day_local_id}"
            )
        detail = (
            f"Case B: reuse {rule.target_date_dimension}.day as label/{rule.day_reuse_label_id}; "
            f"remove source field {source_local_id}"
        )
        if remapped:
            detail += (
                f"; remapped {remapped} leftover reference(s) "
                f"{source_local_id} -> {day_local_id}"
            )
        return True, detail, {
            "kind": "visual_field",
            "source_attribute": row.source_attribute,
            "source_label_id": rule.source_label_id,
            "target_label_id": rule.day_reuse_label_id,
            "target_local_ids": [day_local_id],
            "remapped_local_id_references": remapped,
        }

    # Case A or Case C: source is replaced in place. Existing non-DAY target usages remain untouched.
    for _, _, _, _, attr in source_fields:
        set_display_form_id(attr, target_label)
        apply_alias(attr, title)
    existing_non_day = [
        display_form_id(x[4]) for x in target_dimension_items(original_content, rule.target_date_dimension)
    ]
    case = "Case C" if existing_non_day else "Case A"
    return True, (
        f"{case}: replace {len(source_fields)} source field occurrence(s) with label/{target_label}"
    ), {
        "kind": "visual_field",
        "source_attribute": row.source_attribute,
        "target_label_id": target_label,
        "target_local_ids": [str(x[4].get("localIdentifier") or "") for x in source_fields],
    }


def transform_visualization(
    entity: dict[str, Any],
    rows: list[ScopeRow],
    rules: dict[str, ReplacementRule],
) -> TransformResult:
    original_content = copy.deepcopy(entity_content(entity))
    proposed = put_payload_from_entity(entity)
    content = entity_content(proposed)
    outcomes: list[RowOutcome] = []
    checks: list[dict[str, Any]] = []
    changed = False

    try:
        _preflight_reuse_claims(original_content, rows, rules)
        for row in rows:
            rule = rules[row.source_attribute]
            row_changed = False
            details: list[str] = []

            field_changed, field_detail, field_check = _transform_visual_field(
                content, original_content, row, rule
            )
            if field_changed:
                row_changed = True
                changed = True
                details.append(field_detail)
                if field_check:
                    checks.append(field_check)

            filter_changed, filter_detail, filter_check = _convert_source_filter(
                content, row, rule
            )
            if filter_changed:
                row_changed = True
                changed = True
                details.append(filter_detail)
                if filter_check:
                    checks.append(filter_check)

            if row_changed:
                outcomes.append(_row_outcome(row, "READY", "; ".join(details)))
            else:
                outcomes.append(
                    _row_outcome(
                        row,
                        "NO_OCCURRENCE",
                        "No source visualization field or attribute filter",
                    )
                )
    except TransformError as exc:
        blocked = [
            _row_outcome(r, "BLOCKED", f"Object-level safety stop: {exc}") for r in rows
        ]
        return TransformResult(False, None, blocked, blocked=True, block_reason=str(exc))

    return TransformResult(changed, proposed if changed else None, outcomes, checks)


def _collect_filter_context_ref_ids(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        ref = value.get("filterContextRef")
        if isinstance(ref, dict):
            identifier = ref.get("identifier")
            if isinstance(identifier, dict) and identifier.get("id"):
                out.append(str(identifier["id"]))
        for child in value.values():
            out.extend(_collect_filter_context_ref_ids(child))
    elif isinstance(value, list):
        for child in value:
            out.extend(_collect_filter_context_ref_ids(child))
    return out


def dashboard_filter_context_id(dashboard_entity: dict[str, Any]) -> str:
    content = entity_content(dashboard_entity)
    refs = _collect_filter_context_ref_ids(content)
    unique = sorted(set(refs))
    if not unique:
        raise TransformError(
            "Dashboard has no filterContextRef in its content/tabs. This dashboard payload shape "
            "is intentionally blocked until tested on DEV."
        )
    if len(unique) != 1:
        raise TransformError(
            f"Dashboard references multiple filter contexts {unique}; automatic migration is ambiguous"
        )
    return unique[0]


def _dashboard_attribute_filter_source(wrapper: dict[str, Any]) -> tuple[str | None, str | None]:
    # GoodData Cloud dashboard filterContext entity shape.
    body = wrapper.get("attributeFilter")
    if isinstance(body, dict):
        return display_form_id(body), (
            str(body.get("localIdentifier")) if body.get("localIdentifier") is not None else None
        )
    # Compatibility fallback for filter-context payloads that use the visualization-style wrappers.
    return attribute_filter_source(wrapper)


def _dashboard_date_filter_dataset(wrapper: dict[str, Any]) -> tuple[str | None, str | None, bool]:
    # GoodData Cloud dashboard filterContext entity shape.
    body = wrapper.get("dateFilter")
    if isinstance(body, dict):
        ds = ((((body.get("dataSet") or {}).get("identifier")) or {}).get("id"))
        if ds is None:
            return None, None, False
        unrestricted = "from" not in body and "to" not in body
        gran = body.get("granularity")
        return str(ds), str(gran) if gran is not None else None, unrestricted
    # Compatibility fallback.
    return date_filter_dataset(wrapper)


def _make_unrestricted_dashboard_date_filter(dataset_id: str, local_id: str) -> dict[str, Any]:
    # Verified from the DEV dashboard filterContext supplied for this migration:\n    # an All-time dashboard date filter is `dateFilter`, type=relative, GDC.time.date, no from/to.
    return {
        "dateFilter": {
            "type": "relative",
            "granularity": "GDC.time.date",
            "dataSet": {
                "identifier": {
                    "id": dataset_id,
                    "type": "dataset",
                }
            },
            # Preserve the opaque local identifier to minimize dashboard/filter-context coupling changes.
            "localIdentifier": local_id,
        }
    }


def _remove_dashboard_attribute_filter_configs(value: Any, local_ids: set[str]) -> int:
    removed = 0
    if isinstance(value, dict):
        for key in list(value.keys()):
            child = value[key]
            if key == "attributeFilterConfigs":
                if isinstance(child, list):
                    kept = []
                    for item in child:
                        if isinstance(item, dict) and str(item.get("localIdentifier") or "") in local_ids:
                            removed += 1
                        else:
                            kept.append(item)
                    value[key] = kept
                elif isinstance(child, dict):
                    new_child: dict[str, Any] = {}
                    for cfg_key, cfg_value in child.items():
                        cfg_local = ""
                        if isinstance(cfg_value, dict):
                            cfg_local = str(cfg_value.get("localIdentifier") or "")
                        if str(cfg_key) in local_ids or cfg_local in local_ids:
                            removed += 1
                        else:
                            new_child[str(cfg_key)] = cfg_value
                    value[key] = new_child
                elif child is not None:
                    raise TransformError("Dashboard attributeFilterConfigs has an unsupported shape")
                continue
            removed += _remove_dashboard_attribute_filter_configs(child, local_ids)
    elif isinstance(value, list):
        for child in value:
            removed += _remove_dashboard_attribute_filter_configs(child, local_ids)
    return removed


def _dashboard_config_local_ids(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "attributeFilterConfigs":
                if isinstance(child, list):
                    for item in child:
                        if isinstance(item, dict) and item.get("localIdentifier") is not None:
                            out.append(str(item["localIdentifier"]))
                elif isinstance(child, dict):
                    for cfg_key, cfg_value in child.items():
                        out.append(str(cfg_key))
                        if isinstance(cfg_value, dict) and cfg_value.get("localIdentifier") is not None:
                            out.append(str(cfg_value["localIdentifier"]))
            else:
                out.extend(_dashboard_config_local_ids(child))
    elif isinstance(value, list):
        for child in value:
            out.extend(_dashboard_config_local_ids(child))
    return out


def _convert_dashboard_source_filter(
    content: dict[str, Any],
    row: ScopeRow,
    rule: ReplacementRule,
) -> tuple[bool, str, dict[str, Any] | None, str | None]:
    matching: list[tuple[int, dict[str, Any], str | None]] = []
    existing_same_dataset_dates: list[tuple[int, str | None, bool]] = []

    for index, wrapper in filter_rows(content):
        src, local_id = _dashboard_attribute_filter_source(wrapper)
        if src == rule.source_label_id:
            matching.append((index, wrapper, local_id))
        ds, gran, unrestricted = _dashboard_date_filter_dataset(wrapper)
        if ds == rule.target_date_dimension:
            existing_same_dataset_dates.append((index, gran, unrestricted))

    if not matching:
        return False, "No source dashboard attribute filter", None, None
    if len(matching) != 1:
        raise TransformError(
            f"{row.source_attribute}: found {len(matching)} dashboard source attribute filters; expected exactly one"
        )
    if existing_same_dataset_dates:
        raise TransformError(
            f"{row.source_attribute}: target date dataset {rule.target_date_dimension} already has "
            "a dashboard date filter; existing date filters are immutable and duplicate semantics are unsafe"
        )

    index, _, local_id = matching[0]
    if not local_id:
        raise TransformError(
            f"{row.source_attribute}: dashboard source attribute filter has no localIdentifier"
        )

    content["filters"][index] = _make_unrestricted_dashboard_date_filter(
        rule.target_date_dimension,
        local_id,
    )
    check = {
        "kind": "dashboard_unrestricted_date_filter",
        "source_attribute": row.source_attribute,
        "source_label_id": rule.source_label_id,
        "target_date_dimension": rule.target_date_dimension,
        "local_identifier": local_id,
        "api_granularity": "GDC.time.date",
    }
    return True, (
        f"Convert dashboard attribute filter to unrestricted date filter on {rule.target_date_dimension}"
    ), check, local_id


def transform_dashboard_filter_context(
    dashboard_entity: dict[str, Any],
    filter_context_entity: dict[str, Any],
    rows: list[ScopeRow],
    rules: dict[str, ReplacementRule],
) -> TransformResult:
    proposed_context = put_payload_from_entity(filter_context_entity)
    context_content = entity_content(proposed_context)
    proposed_dashboard = put_payload_from_entity(dashboard_entity)
    dashboard_content = entity_content(proposed_dashboard)
    outcomes: list[RowOutcome] = []
    context_checks: list[dict[str, Any]] = []
    converted_local_ids: set[str] = set()
    changed = False

    try:
        for row in rows:
            rule = rules[row.source_attribute]
            filter_changed, detail, check, local_id = _convert_dashboard_source_filter(
                context_content,
                row,
                rule,
            )
            if filter_changed:
                changed = True
                outcomes.append(_row_outcome(row, "READY", detail))
                if check:
                    context_checks.append(check)
                if local_id:
                    converted_local_ids.add(local_id)
            else:
                outcomes.append(
                    _row_outcome(row, "NO_OCCURRENCE", "No source attribute filter in dashboard filter context")
                )

        dashboard_changed = False
        dashboard_checks: list[dict[str, Any]] = []
        if converted_local_ids:
            removed = _remove_dashboard_attribute_filter_configs(
                dashboard_content,
                converted_local_ids,
            )
            # After removing the known attribute-filter config containers, any remaining reference
            # to a converted filter localIdentifier is an unknown dependency and must block the write.
            for local_id in sorted(converted_local_ids):
                if json_contains_token(dashboard_content, local_id):
                    raise TransformError(
                        f"Converted dashboard filter {local_id} is still referenced outside supported attributeFilterConfigs"
                    )
            if removed:
                dashboard_changed = True
                dashboard_checks.append({
                    "kind": "dashboard_attribute_filter_configs_removed",
                    "local_identifiers": sorted(converted_local_ids),
                })

        planned_writes: list[dict[str, Any]] = []
        # Write dashboard config cleanup first. If the following filterContext PUT fails,
        # automatic rollback restores this dashboard before the run exits.
        if dashboard_changed:
            planned_writes.append({
                "collection": "analyticalDashboards",
                "backup": dashboard_entity,
                "proposed": proposed_dashboard,
                "checks": dashboard_checks,
            })
        if changed:
            planned_writes.append({
                "collection": "filterContexts",
                "backup": filter_context_entity,
                "proposed": proposed_context,
                "checks": context_checks,
            })

        all_checks = dashboard_checks + context_checks
        # `proposed` remains the filter-context proposal for compatibility/debug output;
        # runner uses planned_writes for dashboard execution.
        return TransformResult(
            bool(planned_writes),
            proposed_context if changed else None,
            outcomes,
            all_checks,
            planned_writes=planned_writes,
        )
    except TransformError as exc:
        blocked = [
            _row_outcome(r, "BLOCKED", f"Object-level dashboard safety stop: {exc}") for r in rows
        ]
        return TransformResult(False, None, blocked, blocked=True, block_reason=str(exc))

def verify_checks(entity: dict[str, Any], checks: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    content = entity_content(entity)

    for check in checks:
        kind = check.get("kind")
        source = str(check.get("source_attribute") or "")
        source_label = str(check.get("source_label_id") or source)
        if kind == "metric_maql":
            maql = content.get("maql")
            if maql != check.get("expected_maql"):
                issues.append(f"{source}: live MAQL does not match planned MAQL")
            if isinstance(maql, str) and (
                f"{{label/{source_label}}}" in maql or f"{{attribute/{source}}}" in maql
            ):
                issues.append(f"{source}: legacy source remains in live MAQL")

        elif kind == "visual_field":
            if items_by_label(content, source_label):
                issues.append(f"{source}: legacy source field still exists")
            target = str(check.get("target_label_id") or "")
            local_ids = [str(x) for x in check.get("target_local_ids") or [] if x]
            for local_id in local_ids:
                rows = items_by_local_id(content, local_id)
                if len(rows) != 1 or display_form_id(rows[0][4]) != target:
                    issues.append(
                        f"{source}: local field {local_id} is not using expected target {target}"
                    )

        elif kind == "unrestricted_date_filter":
            source_count = 0
            target_count = 0
            for _, wrapper in filter_rows(content):
                src, _ = attribute_filter_source(wrapper)
                if src == source_label:
                    source_count += 1
                ds, gran, unrestricted = date_filter_dataset(wrapper)
                if (
                    ds == check.get("target_date_dimension")
                    and gran == "GDC.time.year"
                    and unrestricted
                ):
                    target_count += 1
            if source_count:
                issues.append(f"{source}: legacy source attribute filter still exists")
            if target_count != 1:
                issues.append(
                    f"{source}: expected one unrestricted target date filter, found {target_count}"
                )

        elif kind == "dashboard_unrestricted_date_filter":
            source_count = 0
            target_count = 0
            expected_local = str(check.get("local_identifier") or "")
            for _, wrapper in filter_rows(content):
                src, _ = _dashboard_attribute_filter_source(wrapper)
                if src == source_label:
                    source_count += 1
                ds, gran, unrestricted = _dashboard_date_filter_dataset(wrapper)
                body = wrapper.get("dateFilter") if isinstance(wrapper.get("dateFilter"), dict) else {}
                local_id = str(body.get("localIdentifier") or "") if isinstance(body, dict) else ""
                if (
                    ds == check.get("target_date_dimension")
                    and gran == "GDC.time.date"
                    and unrestricted
                    and body.get("type") == "relative"
                    and local_id == expected_local
                ):
                    target_count += 1
            if source_count:
                issues.append(f"{source}: legacy dashboard attribute filter still exists")
            if target_count != 1:
                issues.append(
                    f"{source}: expected one unrestricted dashboard target date filter, found {target_count}"
                )

        elif kind == "dashboard_attribute_filter_configs_removed":
            remaining = set(_dashboard_config_local_ids(content))
            for local_id in [str(x) for x in check.get("local_identifiers") or []]:
                if local_id in remaining:
                    issues.append(
                        f"Dashboard attributeFilterConfigs still contains converted localIdentifier {local_id}"
                    )
        else:
            issues.append(f"Unknown verification check kind: {kind!r}")

    return issues
