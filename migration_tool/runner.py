from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .api import (
    COLLECTION_BY_CATEGORY,
    TYPE_BY_CATEGORY,
    ApiError,
    GoodDataApi,
    canonical_json,
    entity_attributes,
    entity_content,
    entity_id,
    entity_title,
    entity_type,
    put_payload_from_entity,
)
from .config import ConfigError, ReplacementRule, ScopeRow, group_scope, load_rules, load_scope
from .discovery import (
    scan_dashboard_filter_context_occurrences,
    scan_metric_occurrences,
    scan_visualization_occurrences,
    unscoped_occurrences,
)
from .transform import (
    TransformError,
    dashboard_filter_context_id,
    transform_dashboard_filter_context,
    transform_metric,
    transform_visualization,
    verify_checks,
)


TOOL_VERSION = "1.4.1"


class RunnerError(RuntimeError):
    pass


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def attributes_hash(entity: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(entity_attributes(entity)).encode("utf-8")).hexdigest()


def normalize_host(host: str) -> str:
    return host.rstrip("/")


def require_connection() -> tuple[str, str, str]:
    host = normalize_host(os.environ.get("GD_HOST", ""))
    workspace = os.environ.get("GD_WORKSPACE", "")
    token = os.environ.get("GD_TOKEN", "")
    if not host:
        raise RunnerError("GD_HOST is not set")
    if not workspace:
        raise RunnerError("GD_WORKSPACE is not set")
    if not token:
        raise RunnerError("GD_TOKEN is not set")
    return host, workspace, token


def timestamp() -> str:
    return dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S%z")


def latest_run(runs_dir: Path, *, require_plan: bool = True) -> Path:
    if not runs_dir.exists():
        raise RunnerError(f"Runs directory does not exist: {runs_dir}")
    candidates = sorted(
        p for p in runs_dir.iterdir()
        if p.is_dir() and ((p / "plan.json").exists() if require_plan else True)
    )
    if not candidates:
        raise RunnerError(f"No migration runs found in {runs_dir}")
    return candidates[-1]


def object_id_from_data(data: dict[str, Any]) -> str:
    return str(data.get("id") or "")


def object_title_from_data(data: dict[str, Any]) -> str:
    attrs = data.get("attributes")
    return str(attrs.get("title") or "") if isinstance(attrs, dict) else ""


def exact_title_matches(items: list[dict[str, Any]], title: str) -> list[dict[str, Any]]:
    return [x for x in items if object_title_from_data(x) == title]


def index_by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        object_id = object_id_from_data(item)
        if object_id:
            out[object_id] = item
    return out


def resolve_listed_object(
    *,
    listed: list[dict[str, Any]],
    title: str,
    legacy_object_id: str,
) -> tuple[str, dict[str, Any] | None, str]:
    """Resolve a scoped object from the listed collection.

    Preference order:
    1. ``legacy_object_id`` when it matches a live Entity ID (handles duplicate titles)
    2. exact unique title match
    3. NOT_FOUND / AMBIGUOUS

    Returns ``(status, listed_item_or_none, detail)``.
    """
    by_id = index_by_id(listed)
    if legacy_object_id and legacy_object_id in by_id:
        return (
            "FOUND_BY_ID",
            by_id[legacy_object_id],
            f"Resolved by legacy_object_id={legacy_object_id}",
        )

    matches = exact_title_matches(listed, title)
    if len(matches) == 1:
        detail = "Exact title+category match"
        if legacy_object_id:
            detail = (
                f"Exact title+category match "
                f"(legacy_object_id={legacy_object_id!r} not present in workspace list)"
            )
        return "FOUND_BY_TITLE", matches[0], detail
    if len(matches) == 0:
        if legacy_object_id:
            return (
                "NOT_FOUND",
                None,
                f"No object with id={legacy_object_id!r} and no exact title+category match",
            )
        return "NOT_FOUND", None, "No exact title+category match"
    return (
        "AMBIGUOUS",
        None,
        f"{len(matches)} exact title+category matches; provide a live Cloud "
        f"legacy_object_id to disambiguate",
    )


def _target_exists(api: GoodDataApi, collection: str, object_id: str) -> bool:
    return api.try_get_entity(collection, object_id) is not None


def validate_targets(
    api: GoodDataApi,
    checks: list[dict[str, Any]],
    rows: list[ScopeRow],
    rules: dict[str, ReplacementRule],
) -> list[str]:
    issues: list[str] = []
    datasets: set[str] = set()
    labels: set[str] = set()

    ready_sources = {r.source_attribute for r in rows}
    for source in ready_sources:
        datasets.add(rules[source].target_date_dimension)

    for check in checks:
        if check.get("kind") in {"metric_maql", "visual_field"}:
            label = check.get("target_label_id")
            if label:
                labels.add(str(label))
        if check.get("kind") == "unrestricted_date_filter":
            ds = check.get("target_date_dimension")
            if ds:
                datasets.add(str(ds))

    for dataset in sorted(datasets):
        try:
            exists = _target_exists(api, "datasets", dataset)
        except ApiError as exc:
            issues.append(f"Cannot validate target dataset {dataset}: {exc}")
            continue
        if not exists:
            issues.append(f"Target dataset does not exist: {dataset}")

    for label in sorted(labels):
        try:
            exists = _target_exists(api, "labels", label)
        except ApiError as exc:
            issues.append(f"Cannot validate target label {label}: {exc}")
            continue
        if not exists:
            issues.append(f"Target label does not exist: {label}")

    return issues


def _full_dashboard_entities(api: GoodDataApi, listed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for data in listed:
        object_id = object_id_from_data(data)
        if not object_id:
            continue
        # List responses normally include content, but GET is the safe source of truth.
        out.append(api.get_entity("analyticalDashboards", object_id))
    return out


def _dashboard_context_usage(dashboards: list[dict[str, Any]]) -> dict[str, list[str]]:
    usage: dict[str, list[str]] = {}
    for dashboard in dashboards:
        try:
            context_id = dashboard_filter_context_id(dashboard)
        except Exception:
            continue
        usage.setdefault(context_id, []).append(entity_id(dashboard))
    return usage


def _write_entry(
    *,
    run_dir: Path,
    collection: str,
    backup: dict[str, Any],
    proposed: dict[str, Any],
    checks: list[dict[str, Any]],
    scoped_category: str,
    scoped_object_id: str,
    scoped_title: str,
    source_rows: list[ScopeRow],
) -> dict[str, Any]:
    object_id = entity_id(backup)
    backup_path = run_dir / "backups" / collection / f"{object_id}.json"
    proposed_path = run_dir / "proposed" / collection / f"{object_id}.json"
    save_json(backup_path, backup)
    save_json(proposed_path, proposed)
    return {
        "collection": collection,
        "id": object_id,
        "type": entity_type(backup),
        "title": entity_title(backup),
        "backup_path": str(backup_path),
        "proposed_path": str(proposed_path),
        "backup_attributes_sha256": attributes_hash(backup),
        "checks": checks,
        "scoped_category": scoped_category,
        "scoped_object_id": scoped_object_id,
        "scoped_title": scoped_title,
        "scope_rows": [r.row_number for r in source_rows],
    }


def plan(
    *,
    scope_path: Path,
    rules_path: Path,
    runs_dir: Path,
) -> Path:
    host, workspace, token = require_connection()
    rules = load_rules(rules_path)
    scope = load_scope(scope_path, rules)
    grouped = group_scope(scope)
    api = GoodDataApi(host, workspace, token)

    run_dir = runs_dir / timestamp()
    run_dir.mkdir(parents=True, exist_ok=False)

    needed_categories = sorted({row.category for row in scope})
    listed_by_category: dict[str, list[dict[str, Any]]] = {}
    print("PLAN — READ + BACKUP + PROPOSED TRANSFORMATIONS (NO WRITES)")
    print(f"Host:      {host}")
    print(f"Workspace: {workspace}")
    print(f"Scope rows:{len(scope):>4}")
    print(f"Run dir:   {run_dir}")
    print()

    for category in needed_categories:
        collection = COLLECTION_BY_CATEGORY[category]
        print(f"Loading {collection} ...")
        listed_by_category[category] = api.list_entities(collection)
        print(f"  {len(listed_by_category[category])} effective object(s)")

    dashboard_usage: dict[str, list[str]] = {}
    if "dashboard" in needed_categories:
        print("Resolving dashboard filter-context ownership ...")
        dashboards_full = _full_dashboard_entities(api, listed_by_category["dashboard"])
        dashboard_usage = _dashboard_context_usage(dashboards_full)
        print(f"  {len(dashboard_usage)} filter context reference(s) discovered")

    plan_objects: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []
    used_write_keys: set[tuple[str, str]] = set()

    row_counts = {
        "READY": 0,
        "NOT_FOUND": 0,
        "AMBIGUOUS": 0,
        "NO_OCCURRENCE": 0,
        "BLOCKED": 0,
    }

    for index, ((category, identity), rows) in enumerate(grouped.items(), start=1):
        collection = COLLECTION_BY_CATEGORY[category]
        title = rows[0].object_title
        # Groups are keyed by object identity (ID when present); all rows in a
        # group share the same legacy_object_id when discover/Platform export
        # provided one.
        legacy_ids = {row.legacy_object_id for row in rows if row.legacy_object_id}
        if len(legacy_ids) > 1:
            raise RunnerError(
                f"Scope group {category!r}/{identity!r} mixes multiple legacy_object_id "
                f"values: {sorted(legacy_ids)}"
            )
        legacy_object_id = next(iter(legacy_ids)) if legacy_ids else ""

        lookup_status, match, lookup_detail = resolve_listed_object(
            listed=listed_by_category[category],
            title=title,
            legacy_object_id=legacy_object_id,
        )
        obj_report: dict[str, Any] = {
            "category": category,
            "title": title,
            "object_identity": identity,
            "scope_rows": [r.row_number for r in rows],
            "lookup_status": None,
            "lookup_detail": lookup_detail,
            "id": None,
            "row_outcomes": [],
            "write_keys": [],
        }
        print(f"[{index:03d}/{len(grouped):03d}] {category:<13} {title}")
        if legacy_object_id:
            print(f"  identity: {legacy_object_id}")

        if lookup_status == "NOT_FOUND":
            obj_report["lookup_status"] = "NOT_FOUND"
            for row in rows:
                outcome = {
                    "row_number": row.row_number,
                    "legacy_object_id": row.legacy_object_id,
                    "source_attribute": row.source_attribute,
                    "status": "NOT_FOUND",
                    "detail": lookup_detail,
                }
                obj_report["row_outcomes"].append(outcome)
                row_counts["NOT_FOUND"] += 1
            plan_objects.append(obj_report)
            print("  NOT FOUND")
            continue

        if lookup_status == "AMBIGUOUS":
            matches = exact_title_matches(listed_by_category[category], title)
            obj_report["lookup_status"] = "AMBIGUOUS"
            obj_report["candidate_ids"] = [object_id_from_data(x) for x in matches]
            for row in rows:
                outcome = {
                    "row_number": row.row_number,
                    "legacy_object_id": row.legacy_object_id,
                    "source_attribute": row.source_attribute,
                    "status": "AMBIGUOUS",
                    "detail": lookup_detail,
                }
                obj_report["row_outcomes"].append(outcome)
                row_counts["AMBIGUOUS"] += 1
            plan_objects.append(obj_report)
            print(f"  AMBIGUOUS: {lookup_detail}")
            continue

        assert match is not None
        object_id = object_id_from_data(match)
        obj_report["lookup_status"] = "FOUND"
        obj_report["id"] = object_id
        if lookup_status == "FOUND_BY_ID":
            print(f"  FOUND by id ({object_id})")
        else:
            print(f"  FOUND by title ({object_id})")

        try:
            entity = api.get_entity(collection, object_id)
            if entity_id(entity) != object_id:
                raise RunnerError("Entity identity mismatch after lookup")
            # Scope titles are normalized with strip(); live titles may keep
            # accidental leading/trailing spaces (seen in customer clones).
            live_title = entity_title(entity)
            if live_title.strip() != title.strip():
                raise RunnerError(
                    f"Entity title mismatch after lookup: scope title {title!r} "
                    f"!= live title {live_title!r} for id {object_id}"
                )
            if entity_type(entity) != TYPE_BY_CATEGORY[category]:
                raise RunnerError(
                    f"Entity type mismatch: expected {TYPE_BY_CATEGORY[category]}, got {entity_type(entity)}"
                )

            if category == "metric":
                result = transform_metric(entity, rows, rules)
                write_collection = collection
                write_backup = entity
            elif category == "visualization":
                result = transform_visualization(entity, rows, rules)
                write_collection = collection
                write_backup = entity
            else:
                context_id = dashboard_filter_context_id(entity)
                obj_report["filter_context_id"] = context_id
                users = dashboard_usage.get(context_id, [])
                if len(users) != 1 or users[0] != object_id:
                    raise TransformError(
                        f"Dashboard filter context {context_id!r} is shared/referenced by {len(users)} dashboards: {users}"
                    )
                context_entity = api.try_get_entity("filterContexts", context_id)
                if context_entity is None:
                    raise TransformError(
                        f"Dashboard filter context {context_id!r} was not found at Entity API collection 'filterContexts'. "
                        "This dashboard payload shape must be tested before enabling writes."
                    )
                result = transform_dashboard_filter_context(entity, context_entity, rows, rules)
                write_collection = "filterContexts"
                write_backup = context_entity

            # V1.4 diagnostic only: scan the already-loaded live object against every known
            # replacement rule and report known legacy sources that are present but NOT
            # authorized by this object's scope. This never broadens scope and never blocks/writes.
            if category == "metric":
                known_occurrences = scan_metric_occurrences(entity, rules)
            elif category == "visualization":
                known_occurrences = scan_visualization_occurrences(entity, rules)
            else:
                known_occurrences = scan_dashboard_filter_context_occurrences(context_entity, rules)
            unscoped = unscoped_occurrences(
                known_occurrences,
                {row.source_attribute for row in rows},
            )
            if unscoped:
                obj_report["unscoped_known_legacy_occurrences"] = unscoped
                for item in unscoped:
                    kinds = ", ".join(item["occurrence_types"])
                    print(
                        f"  WARNING UNSCOPED LEGACY: {item['source_attribute']} "
                        f"({item['occurrence_count']} occurrence(s): {kinds})"
                    )

            ready_rows = [
                r for r, outcome in zip(rows, result.outcomes)
                if outcome.status == "READY"
            ]
            target_issues = []
            if ready_rows and not result.blocked:
                target_issues = validate_targets(api, result.checks, ready_rows, rules)

            if target_issues:
                reason = "; ".join(target_issues)
                result.blocked = True
                result.block_reason = reason
                result.proposed = None
                result.changed = False
                result.outcomes = [
                    type(outcome)(
                        row_number=outcome.row_number,
                        source_attribute=outcome.source_attribute,
                        status="BLOCKED",
                        detail=f"Target validation failed: {reason}",
                        legacy_object_id=outcome.legacy_object_id,
                    )
                    for outcome in result.outcomes
                ]

            for outcome in result.outcomes:
                obj_report["row_outcomes"].append({
                    "row_number": outcome.row_number,
                    "legacy_object_id": outcome.legacy_object_id,
                    "source_attribute": outcome.source_attribute,
                    "status": outcome.status,
                    "detail": outcome.detail,
                })
                row_counts[outcome.status] = row_counts.get(outcome.status, 0) + 1

            if result.blocked:
                obj_report["object_status"] = "BLOCKED"
                obj_report["block_reason"] = result.block_reason
                print(f"  BLOCKED: {result.block_reason}")
            elif result.changed:
                obj_report["object_status"] = "READY"
                if result.planned_writes:
                    for planned in result.planned_writes:
                        write = _write_entry(
                            run_dir=run_dir,
                            collection=str(planned["collection"]),
                            backup=planned["backup"],
                            proposed=planned["proposed"],
                            checks=list(planned.get("checks") or []),
                            scoped_category=category,
                            scoped_object_id=object_id,
                            scoped_title=title,
                            source_rows=rows,
                        )
                        key = (write["collection"], write["id"])
                        if key in used_write_keys:
                            raise RunnerError(
                                f"Safety stop: multiple scoped objects would write the same entity {key}"
                            )
                        used_write_keys.add(key)
                        writes.append(write)
                        obj_report["write_keys"].append({"collection": key[0], "id": key[1]})
                        print(f"  READY -> {key[0]}/{key[1]}")
                elif result.proposed is not None:
                    write = _write_entry(
                        run_dir=run_dir,
                        collection=write_collection,
                        backup=write_backup,
                        proposed=result.proposed,
                        checks=result.checks,
                        scoped_category=category,
                        scoped_object_id=object_id,
                        scoped_title=title,
                        source_rows=rows,
                    )
                    key = (write_collection, write["id"])
                    if key in used_write_keys:
                        raise RunnerError(
                            f"Safety stop: multiple scoped objects would write the same entity {key}"
                        )
                    used_write_keys.add(key)
                    writes.append(write)
                    obj_report["write_keys"].append({"collection": key[0], "id": key[1]})
                    print(f"  READY -> {write_collection}/{write['id']}")
                else:
                    raise RunnerError("Transform reported changed=True but produced no planned write")
            else:
                obj_report["object_status"] = "NO_CHANGE"
                print("  NO CHANGE")

        except (ApiError, TransformError, RunnerError, ConfigError) as exc:
            obj_report["object_status"] = "BLOCKED"
            obj_report["block_reason"] = str(exc)
            obj_report["row_outcomes"] = []
            for row in rows:
                obj_report["row_outcomes"].append({
                    "row_number": row.row_number,
                    "legacy_object_id": row.legacy_object_id,
                    "source_attribute": row.source_attribute,
                    "status": "BLOCKED",
                    "detail": str(exc),
                })
                row_counts["BLOCKED"] += 1
            print(f"  BLOCKED: {exc}")

        plan_objects.append(obj_report)

    accounting = sum(row_counts.values())
    if accounting != len(scope):
        raise RunnerError(
            f"Internal accounting error: row statuses sum to {accounting}, scope has {len(scope)} rows"
        )

    unscoped_warning_count = sum(
        len(obj.get("unscoped_known_legacy_occurrences") or [])
        for obj in plan_objects
    )

    plan_doc = {
        "tool_version": TOOL_VERSION,
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "mode": "PLAN_NO_WRITES",
        "host": host,
        "workspace": workspace,
        "scope_path": str(scope_path.resolve()),
        "rules_path": str(rules_path.resolve()),
        "scope_rows": len(scope),
        "distinct_scoped_objects": len(grouped),
        "row_counts": row_counts,
        "write_count": len(writes),
        "unscoped_known_legacy_count": unscoped_warning_count,
        "objects": plan_objects,
        "writes": writes,
    }
    save_json(run_dir / "plan.json", plan_doc)
    (run_dir / "plan.md").write_text(render_plan_markdown(plan_doc), encoding="utf-8")

    print()
    print("SUMMARY")
    print(f"READY rows:         {row_counts['READY']}")
    print(f"NOT FOUND rows:     {row_counts['NOT_FOUND']}")
    print(f"AMBIGUOUS rows:     {row_counts['AMBIGUOUS']}")
    print(f"NO OCCURRENCE rows: {row_counts['NO_OCCURRENCE']}")
    print(f"BLOCKED rows:       {row_counts['BLOCKED']}")
    print(f"UNSCOPED known legacy warnings: {unscoped_warning_count}")
    print(f"Planned API writes: {len(writes)}")
    print(f"Plan: {run_dir / 'plan.md'}")
    return run_dir


def render_plan_markdown(plan_doc: dict[str, Any]) -> str:
    c = plan_doc["row_counts"]
    lines = [
        "# GoodData time migration plan",
        "",
        "**PLAN ONLY — no API write was executed.**",
        "",
        f"- Tool version: `{plan_doc['tool_version']}`",
        f"- Host: `{plan_doc['host']}`",
        f"- Workspace: `{plan_doc['workspace']}`",
        f"- Scope rows: **{plan_doc['scope_rows']}**",
        f"- Distinct scoped objects: **{plan_doc['distinct_scoped_objects']}**",
        f"- READY rows: **{c['READY']}**",
        f"- NOT FOUND rows: **{c['NOT_FOUND']}**",
        f"- AMBIGUOUS rows: **{c['AMBIGUOUS']}**",
        f"- NO OCCURRENCE rows: **{c['NO_OCCURRENCE']}**",
        f"- BLOCKED rows: **{c['BLOCKED']}**",
        f"- UNSCOPED known legacy warnings: **{plan_doc.get('unscoped_known_legacy_count', 0)}**",
        f"- Planned individual Entity API writes: **{plan_doc['write_count']}**",
        "",
        "## Scoped objects",
        "",
    ]
    for obj in plan_doc["objects"]:
        status = obj.get("object_status") or obj.get("lookup_status")
        lines.append(
            f"### {status} — {obj['category']} — {obj['title']}"
        )
        if obj.get("id"):
            lines.append(f"- GoodData ID: `{obj['id']}`")
        if obj.get("filter_context_id"):
            lines.append(f"- Filter context: `{obj['filter_context_id']}`")
        if obj.get("block_reason"):
            lines.append(f"- BLOCK: {obj['block_reason']}")
        for warning in obj.get("unscoped_known_legacy_occurrences") or []:
            kinds = ", ".join(warning.get("occurrence_types") or [])
            lines.append(
                f"- **WARNING — UNSCOPED KNOWN LEGACY** `{warning['source_attribute']}` — "
                f"{warning['occurrence_count']} occurrence(s): {kinds}. Not modified."
            )
        for outcome in obj.get("row_outcomes") or []:
            lines.append(
                f"- **{outcome['status']}** row {outcome['row_number']} — "
                f"`{outcome['source_attribute']}` — {outcome['detail']}"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def _load_run(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "plan.json"
    if not path.exists():
        raise RunnerError(f"Plan not found: {path}")
    return load_json(path)


def _validate_active_environment(plan_doc: dict[str, Any]) -> tuple[GoodDataApi, str, str, str]:
    host, workspace, token = require_connection()
    if normalize_host(plan_doc["host"]) != host:
        raise RunnerError(
            f"Active GD_HOST {host!r} differs from planned host {plan_doc['host']!r}"
        )
    if plan_doc["workspace"] != workspace:
        raise RunnerError(
            f"Active GD_WORKSPACE {workspace!r} differs from planned workspace {plan_doc['workspace']!r}"
        )
    return GoodDataApi(host, workspace, token), host, workspace, token


def _require_write_confirmation(
    *,
    host: str,
    workspace: str,
    confirm_host: str,
    confirm_workspace: str,
) -> None:
    if normalize_host(confirm_host) != host:
        raise RunnerError(
            f"--confirm-host must exactly match active host {host!r}"
        )
    if confirm_workspace != workspace:
        raise RunnerError(
            f"--confirm-workspace must exactly match active workspace {workspace!r}"
        )


def _rollback_written(
    api: GoodDataApi,
    written: list[dict[str, Any]],
    run_dir: Path,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for write in reversed(written):
        backup = load_json(Path(write["backup_path"]))
        payload = put_payload_from_entity(backup)
        row = {
            "collection": write["collection"],
            "id": write["id"],
            "title": write["title"],
            "status": "ROLLBACK_FAILED",
        }
        try:
            api.put_entity(write["collection"], write["id"], payload)
            after = api.get_entity(write["collection"], write["id"])
            if attributes_hash(after) != attributes_hash(backup):
                raise RunnerError("Rollback GET does not match backup attributes")
            row["status"] = "ROLLED_BACK"
        except Exception as exc:
            row["error"] = str(exc)
        results.append(row)
    save_json(run_dir / "last-rollback-report.json", {
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "results": results,
    })
    return results


def apply(
    *,
    run_dir: Path,
    confirm_host: str,
    confirm_workspace: str,
) -> None:
    plan_doc = _load_run(run_dir)
    api, host, workspace, _ = _validate_active_environment(plan_doc)
    _require_write_confirmation(
        host=host,
        workspace=workspace,
        confirm_host=confirm_host,
        confirm_workspace=confirm_workspace,
    )

    blocked = int(plan_doc["row_counts"].get("BLOCKED", 0)) + int(
        plan_doc["row_counts"].get("AMBIGUOUS", 0)
    )
    if blocked:
        raise RunnerError(
            f"Plan contains BLOCKED/AMBIGUOUS rows ({blocked}). Apply is not allowed."
        )

    writes = list(plan_doc.get("writes") or [])
    if not writes:
        print("Nothing to apply: the plan contains no API writes.")
        return

    print("PRE-WRITE CONCURRENCY CHECK — GET ALL PLANNED WRITE ENTITIES")
    mismatches: list[str] = []
    for i, write in enumerate(writes, start=1):
        print(f"[{i:03d}/{len(writes):03d}] GET {write['collection']}/{write['id']} — {write['title']}")
        live = api.get_entity(write["collection"], write["id"])
        if attributes_hash(live) != write["backup_attributes_sha256"]:
            mismatches.append(f"{write['collection']}/{write['id']}")
            print("  CHANGED SINCE PLAN")
        else:
            print("  OK")
    if mismatches:
        raise RunnerError(
            "Live state changed since plan for: " + ", ".join(mismatches) +
            ". No PUT was sent. Create a fresh plan."
        )

    print()
    print(f"APPLY — {len(writes)} INDIVIDUAL ENTITY API PUT(S)")
    written: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    failure: Exception | None = None

    for i, write in enumerate(writes, start=1):
        print(f"[{i:03d}/{len(writes):03d}] PUT {write['collection']}/{write['id']} — {write['title']}")
        row = {
            "collection": write["collection"],
            "id": write["id"],
            "title": write["title"],
            "status": "FAILED",
        }
        try:
            proposed = load_json(Path(write["proposed_path"]))
            api.put_entity(write["collection"], write["id"], proposed)
            written.append(write)
            after = api.get_entity(write["collection"], write["id"])
            issues = verify_checks(after, write.get("checks") or [])
            if issues:
                raise RunnerError("GET verification failed: " + "; ".join(issues))
            row["status"] = "SUCCESS"
            results.append(row)
            print("  SUCCESS + GET verified")
        except Exception as exc:
            row["error"] = str(exc)
            results.append(row)
            failure = exc
            print(f"  FAILED: {exc}")
            break

    rollback_results: list[dict[str, Any]] = []
    status = "SUCCESS"
    if failure is not None:
        status = "FAILED_ROLLBACK_ATTEMPTED"
        print()
        print("FIRST FAILURE — AUTOMATIC ROLLBACK OF ALL WRITTEN ENTITIES")
        rollback_results = _rollback_written(api, written, run_dir)
        if any(r["status"] != "ROLLED_BACK" for r in rollback_results):
            status = "FAILED_ROLLBACK_INCOMPLETE"
        for r in rollback_results:
            print(f"  {r['status']}: {r['collection']}/{r['id']} — {r['title']}")

    report = {
        "tool_version": TOOL_VERSION,
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "status": status,
        "host": host,
        "workspace": workspace,
        "planned_writes": len(writes),
        "results": results,
        "rollback": rollback_results,
    }
    save_json(run_dir / "deployment-report.json", report)
    (run_dir / "deployment-report.md").write_text(render_deployment_report(report), encoding="utf-8")
    print()
    print(f"Deployment report: {run_dir / 'deployment-report.md'}")
    if failure is not None:
        raise RunnerError(f"Deployment failed: {failure}")


def render_deployment_report(report: dict[str, Any]) -> str:
    lines = [
        "# GoodData time migration deployment report",
        "",
        f"- Status: **{report['status']}**",
        f"- Host: `{report['host']}`",
        f"- Workspace: `{report['workspace']}`",
        f"- Planned writes: **{report['planned_writes']}**",
        f"- Successful writes: **{sum(r['status'] == 'SUCCESS' for r in report['results'])}**",
        "",
        "## Deployment",
        "",
    ]
    for row in report["results"]:
        lines.append(
            f"- **{row['status']}** `{row['collection']}/{row['id']}` — {row['title']}"
        )
        if row.get("error"):
            lines.append(f"  - {row['error']}")
    if report.get("rollback"):
        lines.extend(["", "## Automatic rollback", ""])
        for row in report["rollback"]:
            lines.append(
                f"- **{row['status']}** `{row['collection']}/{row['id']}` — {row['title']}"
            )
            if row.get("error"):
                lines.append(f"  - {row['error']}")
    return "\n".join(lines) + "\n"


def verify(*, run_dir: Path) -> bool:
    plan_doc = _load_run(run_dir)
    api, host, workspace, _ = _validate_active_environment(plan_doc)
    writes = list(plan_doc.get("writes") or [])
    results: list[dict[str, Any]] = []
    failures = 0
    print(f"VERIFY — {host} / {workspace}")
    for i, write in enumerate(writes, start=1):
        live = api.get_entity(write["collection"], write["id"])
        issues = verify_checks(live, write.get("checks") or [])
        status = "PASS" if not issues else "FAIL"
        if issues:
            failures += 1
        results.append({
            "collection": write["collection"],
            "id": write["id"],
            "title": write["title"],
            "status": status,
            "issues": issues,
        })
        print(f"[{i:03d}/{len(writes):03d}] {status:<4} {write['collection']}/{write['id']} — {write['title']}")
        for issue in issues:
            print(f"  {issue}")
    report = {
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "host": host,
        "workspace": workspace,
        "pass": failures == 0,
        "results": results,
    }
    save_json(run_dir / "verification-report.json", report)
    lines = [
        "# GoodData time migration verification report",
        "",
        f"- Host: `{host}`",
        f"- Workspace: `{workspace}`",
        f"- Result: **{'PASS' if failures == 0 else 'FAIL'}**",
        f"- Checked write entities: **{len(writes)}**",
        f"- Failed: **{failures}**",
        "",
    ]
    for row in results:
        lines.append(f"- **{row['status']}** `{row['collection']}/{row['id']}` — {row['title']}")
        for issue in row["issues"]:
            lines.append(f"  - {issue}")
    (run_dir / "verification-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Report: {run_dir / 'verification-report.md'}")
    return failures == 0


def rollback(
    *,
    run_dir: Path,
    confirm_host: str,
    confirm_workspace: str,
) -> None:
    plan_doc = _load_run(run_dir)
    api, host, workspace, _ = _validate_active_environment(plan_doc)
    _require_write_confirmation(
        host=host,
        workspace=workspace,
        confirm_host=confirm_host,
        confirm_workspace=confirm_workspace,
    )
    deployment_path = run_dir / "deployment-report.json"
    if not deployment_path.exists():
        raise RunnerError("No deployment-report.json exists for this run")
    deployment = load_json(deployment_path)
    successful = {
        (r["collection"], r["id"])
        for r in deployment.get("results") or []
        if r.get("status") == "SUCCESS"
    }
    plan_writes = {
        (w["collection"], w["id"]): w for w in plan_doc.get("writes") or []
    }
    entries = [plan_writes[k] for k in successful if k in plan_writes]
    if not entries:
        print("No successful write entities from this run need rollback.")
        return
    results = _rollback_written(api, entries, run_dir)
    for row in results:
        print(f"{row['status']}: {row['collection']}/{row['id']} — {row['title']}")
        if row.get("error"):
            print(f"  {row['error']}")
    if any(r["status"] != "ROLLED_BACK" for r in results):
        raise RunnerError("Rollback incomplete; inspect last-rollback-report.json")
