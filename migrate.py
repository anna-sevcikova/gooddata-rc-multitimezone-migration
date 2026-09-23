#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import os
import sys
import tempfile
from pathlib import Path

from migration_tool.api import entity_content
from migration_tool.config import ReplacementRule, ScopeRow, load_rules, load_scope
from migration_tool.discovery import (
    aggregate_occurrences,
    analytics_candidates_from_graph,
    discover_workspace_scope,
    entry_point_identifiers,
    scan_dashboard_filter_context_occurrences,
    scan_metric_occurrences,
    scan_visualization_occurrences,
    unscoped_occurrences,
)
from migration_tool.runner import require_connection
from migration_tool.runner import RunnerError, apply, latest_run, plan, rollback, verify
from migration_tool.transform import (
    dashboard_filter_context_id,
    display_form_id,
    iter_attribute_items,
    transform_dashboard_filter_context,
    transform_metric,
    transform_visualization,
    verify_checks,
)


def synthetic_entity(object_id: str, object_type: str, title: str, content: dict) -> dict:
    return {
        "data": {
            "id": object_id,
            "type": object_type,
            "attributes": {
                "title": title,
                "description": "",
                "content": content,
            },
        }
    }


def rule(
    source: str,
    dataset: str,
    visual_granularity: str,
    visual_title: str,
    *,
    day_reuse: bool = False,
    day_granularity: str | None = None,
    day_title: str | None = None,
    metric_granularity: str | None = None,
) -> ReplacementRule:
    return ReplacementRule(
        source_attribute=source,
        target_date_dimension=dataset,
        visual_default_granularity=visual_granularity,
        visual_default_title=visual_title,
        day_reuse_enabled=day_reuse,
        day_reuse_granularity=day_granularity,
        day_reuse_title=day_title,
        metric_granularity=metric_granularity or visual_granularity,
    )


def scope_row(n: int, source: str, title: str, category: str) -> ScopeRow:
    return ScopeRow(
        row_number=n,
        source_workspace_id="",
        source_attribute=source,
        object_title=title,
        legacy_object_id=str(n),
        legacy_object_type="",
        category=category,
    )


def cmd_self_test(_: argparse.Namespace) -> None:
    # Metric
    r = rule("old.hour", "new", "HOUR_OF_DAY", "Hour")
    metric = synthetic_entity(
        "m1", "metric", "Metric",
        {"maql": "SELECT AVG({metric/x}) BY {label/old.hour}"},
    )
    result = transform_metric(metric, [scope_row(2, "old.hour", "Metric", "metric")], {"old.hour": r})
    assert result.changed and not result.blocked
    assert entity_content(result.proposed)["maql"].endswith("{label/new.hourOfDay}")
    assert not verify_checks(result.proposed, result.checks)

    # Visualization Case A
    vis = synthetic_entity(
        "v1", "visualizationObject", "Visual",
        {
            "buckets": [{
                "localIdentifier": "view",
                "items": [{
                    "attribute": {
                        "localIdentifier": "f1",
                        "displayForm": {"identifier": {"id": "old.hour", "type": "label"}},
                    }
                }],
            }],
            "filters": [],
            "sorts": [],
            "properties": {},
        },
    )
    result = transform_visualization(vis, [scope_row(2, "old.hour", "Visual", "visualization")], {"old.hour": r})
    assert result.changed and not result.blocked
    attrs = list(iter_attribute_items(entity_content(result.proposed)))
    assert display_form_id(attrs[0][4]) == "new.hourOfDay"
    assert attrs[0][4]["alias"] == "Hour"
    assert not verify_checks(result.proposed, result.checks)

    # Case C: non-DAY target is immutable; source replaced separately.
    vis_c = copy.deepcopy(vis)
    entity_content(vis_c)["buckets"][0]["items"].insert(0, {
        "attribute": {
            "localIdentifier": "dow",
            "displayForm": {"identifier": {"id": "new.dayOfWeek", "type": "label"}},
            "alias": "Day of week",
        }
    })
    result = transform_visualization(vis_c, [scope_row(2, "old.hour", "Visual", "visualization")], {"old.hour": r})
    attrs = {a[4]["localIdentifier"]: a[4] for a in iter_attribute_items(entity_content(result.proposed))}
    assert display_form_id(attrs["dow"]) == "new.dayOfWeek"
    assert display_form_id(attrs["f1"]) == "new.hourOfDay"

    # Case B DAY reuse.
    rb = rule(
        "old.time", "new", "SECOND_OF_DAY", "Old Time",
        day_reuse=True, day_granularity="SECOND", day_title="Interaction Start",
    )
    vis_b = synthetic_entity(
        "v2", "visualizationObject", "Case B",
        {
            "buckets": [{
                "localIdentifier": "view",
                "items": [
                    {"attribute": {
                        "localIdentifier": "day1",
                        "displayForm": {"identifier": {"id": "new.day", "type": "label"}},
                        "alias": "Date",
                    }},
                    {"attribute": {
                        "localIdentifier": "legacy",
                        "displayForm": {"identifier": {"id": "old.time", "type": "label"}},
                    }},
                ],
            }],
            "filters": [],
            "sorts": [{
                "attributeSortItem": {
                    "attributeIdentifier": "legacy",
                    "direction": "asc",
                }
            }],
            "properties": {
                "controls": {
                    "columnWidths": [{"attributeIdentifier": "legacy", "width": 120}],
                }
            },
        },
    )
    result = transform_visualization(vis_b, [scope_row(2, "old.time", "Case B", "visualization")], {"old.time": rb})
    assert result.changed and not result.blocked
    attrs = list(iter_attribute_items(entity_content(result.proposed)))
    assert len(attrs) == 1
    assert attrs[0][4]["localIdentifier"] == "day1"
    assert display_form_id(attrs[0][4]) == "new.second"
    assert attrs[0][4]["alias"] == "Interaction Start"
    proposed_content = entity_content(result.proposed)
    assert proposed_content["sorts"][0]["attributeSortItem"]["attributeIdentifier"] == "day1"
    assert proposed_content["properties"]["controls"]["columnWidths"][0]["attributeIdentifier"] == "day1"
    assert "legacy" not in str(proposed_content)
    assert any("remapped" in outcome.detail for outcome in result.outcomes)
    assert not verify_checks(result.proposed, result.checks)

    # Visualization filter conversion, preserving existing unrelated date filter.
    rf = rule("old.filter", "dt_callstartdate", "HOUR_OF_DAY", "Started Hour")
    vis_f = synthetic_entity(
        "v3", "visualizationObject", "Filter",
        {
            "buckets": [{
                "localIdentifier": "view",
                "items": [{"attribute": {
                    "localIdentifier": "f",
                    "displayForm": {"identifier": {"id": "old.filter", "type": "label"}},
                }}],
            }],
            "filters": [
                {"negativeAttributeFilter": {
                    "localIdentifier": "af1",
                    "displayForm": {"identifier": {"id": "old.filter", "type": "label"}},
                    "notIn": {"values": ["N/A"]},
                }},
                {"relativeDateFilter": {
                    "dataSet": {"identifier": {"id": "dt_other", "type": "dataset"}},
                    "granularity": "GDC.time.date",
                    "from": -29,
                    "to": 0,
                }},
            ],
            "sorts": [],
            "properties": {},
        },
    )
    result = transform_visualization(vis_f, [scope_row(2, "old.filter", "Filter", "visualization")], {"old.filter": rf})
    assert result.changed and not result.blocked
    filters = entity_content(result.proposed)["filters"]
    new_filter = filters[0]["relativeDateFilter"]
    assert new_filter["dataSet"]["identifier"]["id"] == "dt_callstartdate"
    assert new_filter["granularity"] == "GDC.time.year"
    assert "from" not in new_filter and "to" not in new_filter
    assert filters[1]["relativeDateFilter"]["from"] == -29
    assert not verify_checks(result.proposed, result.checks)

    # Dashboard -> dedicated filterContext conversion using the real Cloud entity shape:
    # filterContext.attributeFilter/dateFilter plus dashboard attributeFilterConfigs.
    dashboard = synthetic_entity(
        "d1", "analyticalDashboard", "Dashboard",
        {
            "filterContextRef": {"identifier": {"id": "fc1", "type": "filterContext"}},
            "attributeFilterConfigs": [
                {"localIdentifier": "daf1", "selectionType": "listOrText"},
            ],
            "layout": {"type": "IDashboardLayout", "sections": []},
            "tabs": [{
                "filterContextRef": {"identifier": {"id": "fc1", "type": "filterContext"}},
                "attributeFilterConfigs": [
                    {"localIdentifier": "daf1", "selectionType": "listOrText"},
                ],
                "layout": {"type": "IDashboardLayout", "sections": []},
            }],
        },
    )
    assert dashboard_filter_context_id(dashboard) == "fc1"
    filter_context = synthetic_entity(
        "fc1", "filterContext", "Dashboard filter context",
        {
            "filters": [
                {"dateFilter": {
                    "type": "relative",
                    "granularity": "GDC.time.month",
                    "from": 0,
                    "to": 0,
                    "localIdentifier": "0_dateFilter",
                }},
                {"attributeFilter": {
                    "attributeElements": {"uris": [None]},
                    "displayForm": {"identifier": {"id": "old.filter", "type": "label"}},
                    "negativeSelection": True,
                    "localIdentifier": "daf1",
                    "selectionMode": "multi",
                }},
            ],
            "version": "2",
        },
    )
    result = transform_dashboard_filter_context(
        dashboard,
        filter_context,
        [scope_row(2, "old.filter", "Dashboard", "dashboard")],
        {"old.filter": rf},
    )
    assert result.changed and not result.blocked
    assert len(result.planned_writes) == 2
    dashboard_write, context_write = result.planned_writes
    assert dashboard_write["collection"] == "analyticalDashboards"
    assert context_write["collection"] == "filterContexts"
    dash_content = entity_content(dashboard_write["proposed"])
    assert dash_content["attributeFilterConfigs"] == []
    assert dash_content["tabs"][0]["attributeFilterConfigs"] == []
    assert not verify_checks(dashboard_write["proposed"], dashboard_write["checks"])
    context_content = entity_content(context_write["proposed"])
    assert context_content["filters"][0]["dateFilter"]["from"] == 0
    converted = context_content["filters"][1]["dateFilter"]
    assert converted["dataSet"]["identifier"]["id"] == "dt_callstartdate"
    assert converted["granularity"] == "GDC.time.date"
    assert converted["type"] == "relative"
    assert converted["localIdentifier"] == "daf1"
    assert "from" not in converted and "to" not in converted
    assert not verify_checks(context_write["proposed"], context_write["checks"])

    # V1.4 discovery: multiple legacy sources in one visualization, including a
    # source_attribute != source_label case. This reproduces the missed-scope class
    # that motivated the discovery generator.
    rd_half = rule("old.half", "new_dt", "HOUR_OF_DAY", "Hour")
    rd_hour = rule("old.hour.logical", "new_dt", "HOUR_OF_DAY", "Hour")
    rd_hour = ReplacementRule(
        source_attribute=rd_hour.source_attribute,
        source_label="old.hour.displayform",
        target_date_dimension=rd_hour.target_date_dimension,
        visual_default_granularity=rd_hour.visual_default_granularity,
        visual_default_title=rd_hour.visual_default_title,
        day_reuse_enabled=rd_hour.day_reuse_enabled,
        day_reuse_granularity=rd_hour.day_reuse_granularity,
        day_reuse_title=rd_hour.day_reuse_title,
        metric_granularity=rd_hour.metric_granularity,
    )
    discovery_rules = {
        rd_half.source_attribute: rd_half,
        rd_hour.source_attribute: rd_hour,
    }
    discover_vis = synthetic_entity(
        "dv1", "visualizationObject", "Discovery Visual",
        {
            "buckets": [{
                "localIdentifier": "view",
                "items": [{"attribute": {
                    "localIdentifier": "half",
                    "displayForm": {"identifier": {"id": "old.half", "type": "label"}},
                }}],
            }],
            "filters": [{"negativeAttributeFilter": {
                "localIdentifier": "hour_filter",
                "displayForm": {"identifier": {"id": "old.hour.displayform", "type": "label"}},
                "notIn": {"values": []},
            }}],
            "sorts": [],
            "properties": {},
        },
    )
    occurrences = scan_visualization_occurrences(discover_vis, discovery_rules)
    aggregated = aggregate_occurrences(occurrences)
    assert set(aggregated) == {"old.half", "old.hour.logical"}
    assert aggregated["old.half"]["occurrence_types"] == {"visualization_field"}
    assert aggregated["old.hour.logical"]["occurrence_types"] == {"visualization_filter"}
    missing = unscoped_occurrences(occurrences, {"old.half"})
    assert [x["source_attribute"] for x in missing] == ["old.hour.logical"]

    discover_metric = synthetic_entity(
        "dm1", "metric", "Discovery Metric",
        {"maql": "SELECT SUM({fact/x}) BY {label/old.hour.displayform}, {attribute/old.half}"},
    )
    metric_occ = aggregate_occurrences(scan_metric_occurrences(discover_metric, discovery_rules))
    assert metric_occ["old.hour.logical"]["occurrence_count"] == 1
    assert metric_occ["old.half"]["occurrence_count"] == 1

    discover_fc = synthetic_entity(
        "dfc1", "filterContext", "Discovery FC",
        {"filters": [{"attributeFilter": {
            "displayForm": {"identifier": {"id": "old.hour.displayform", "type": "label"}},
            "localIdentifier": "f1",
            "negativeSelection": True,
            "attributeElements": {"uris": []},
        }}]},
    )
    dashboard_occ = aggregate_occurrences(
        scan_dashboard_filter_context_occurrences(discover_fc, discovery_rules)
    )
    assert set(dashboard_occ) == {"old.hour.logical"}

    # V1.4.1 input compatibility: unchanged headerless GoodData Platform export.
    # The sixth yes/no/n/a column is intentionally accepted and ignored.
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_scope = Path(tmpdir) / "platform-export.csv"
        raw_scope.write_text(
            "platform_ws;attr.old.hour;Raw Visual;123;visualizationObject;yes\n"
            "platform_ws;attr.old.hour;Raw Metric;124;metric;no\n"
            "platform_ws;attr.old.hour;Raw Dashboard;125;analyticalDashboard;n/a\n",
            encoding="utf-8",
        )
        parsed = load_scope(raw_scope, {"old.hour": r})
        assert len(parsed) == 3
        assert [x.source_attribute for x in parsed] == ["old.hour", "old.hour", "old.hour"]
        assert [x.category for x in parsed] == ["visualization", "metric", "dashboard"]
        assert [x.source_workspace_id for x in parsed] == ["platform_ws"] * 3
        assert [x.legacy_object_id for x in parsed] == ["123", "124", "125"]
        assert [x.legacy_object_type for x in parsed] == [
            "visualizationObject", "metric", "analyticalDashboard"
        ]

        # Existing normalized/headered scope remains supported.
        normalized_scope = Path(tmpdir) / "normalized.csv"
        normalized_scope.write_text(
            "source_workspace_id;source_attribute;object_title;legacy_object_id;"
            "legacy_object_type;aac_lookup_category\n"
            "platform_ws;old.hour;Normalized Visual;126;visualizationObject;visualization\n",
            encoding="utf-8",
        )
        normalized = load_scope(normalized_scope, {"old.hour": r})
        assert len(normalized) == 1
        assert normalized[0].source_attribute == "old.hour"
        assert normalized[0].category == "visualization"

        # Duplicate titles are allowed when Cloud Entity IDs differ.
        clones_scope = Path(tmpdir) / "clones.csv"
        clones_scope.write_text(
            "source_workspace_id;source_attribute;object_title;legacy_object_id;"
            "legacy_object_type;aac_lookup_category\n"
            "ws;old.hour;Clone of: Interaction Details;idAAA;visualizationObject;visualization\n"
            "ws;old.hour;Clone of: Interaction Details;idBBB;visualizationObject;visualization\n",
            encoding="utf-8",
        )
        clones = load_scope(clones_scope, {"old.hour": r})
        assert len(clones) == 2
        from migration_tool.config import group_scope
        grouped_clones = group_scope(clones)
        assert set(grouped_clones) == {
            ("visualization", "idAAA"),
            ("visualization", "idBBB"),
        }

    from migration_tool.runner import resolve_listed_object
    listed = [
        {"id": "idAAA", "attributes": {"title": "Clone of: Interaction Details"}},
        {"id": "idBBB", "attributes": {"title": "Clone of: Interaction Details"}},
    ]
    status, match, _ = resolve_listed_object(
        listed=listed,
        title="Clone of: Interaction Details",
        legacy_object_id="idBBB",
    )
    assert status == "FOUND_BY_ID"
    assert match is not None and match["id"] == "idBBB"
    status, match, _ = resolve_listed_object(
        listed=listed,
        title="Clone of: Interaction Details",
        legacy_object_id="",
    )
    assert status == "AMBIGUOUS" and match is None

    # Discover helpers: entry points + graph candidate extraction.
    dep_rules = {
        rd_half.source_attribute: rd_half,
        rd_hour.source_attribute: rd_hour,
    }
    entry_points = entry_point_identifiers(dep_rules)
    assert {"id": "old.half", "type": "attribute"} in entry_points
    assert {"id": "old.half", "type": "label"} in entry_points
    assert {"id": "old.hour.logical", "type": "attribute"} in entry_points
    assert {"id": "old.hour.displayform", "type": "label"} in entry_points
    assert len(entry_points) == 4

    candidates = analytics_candidates_from_graph({
        "graph": {
            "nodes": [
                {"id": "old.half", "type": "attribute", "title": "Half"},
                {"id": "ds1", "type": "dataset", "title": "Dataset"},
                {"id": "m1", "type": "metric", "title": "Metric"},
                {"id": "v1", "type": "visualizationObject", "title": "Visual"},
                {"id": "d1", "type": "analyticalDashboard", "title": "Dash"},
                {"id": "m1", "type": "metric", "title": "Metric duplicate node"},
            ],
            "edges": [],
        }
    })
    assert candidates == {
        "metric": {"m1"},
        "visualization": {"v1"},
        "dashboard": {"d1"},
    }

    print("SELF-TEST PASS")
    print("- metric MAQL")
    print("- visualization Case A")
    print("- visualization Case C")
    print("- visualization Case B DAY reuse")
    print("- visualization Case B leftover localIdentifier remap (sort/config)")
    print("- visualization attribute_filter -> unrestricted date filter")
    print("- dashboard filterContext conversion + dashboard config cleanup")
    print("- scope discovery + unscoped-known-legacy diagnostics")
    print("- raw Platform export + normalized scope input compatibility")
    print("- discover entry points + graph candidate extraction")
    print("- ID-based scope grouping + ambiguous-title resolution")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="GoodData direct-API time migration tool (AaC not required)."
    )
    parser.add_argument(
        "--scope",
        default="input/workspace-scope.csv",
        help="Workspace-specific strict whitelist CSV",
    )
    parser.add_argument(
        "--rules",
        default="config/replacement-rules.csv",
        help="Shared/global replacement rules CSV",
    )
    parser.add_argument(
        "--runs-dir",
        default="runs",
        help="Run backups/plans/reports directory",
    )
    parser.add_argument(
        "--run",
        help="Specific run directory for apply/verify/rollback; default is latest plan run",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test", help="Run synthetic transformation tests; no API calls")
    sub.add_parser("plan", help="Read GoodData, back up scoped objects, build proposed writes; no API writes")

    p_discover = sub.add_parser(
        "discover",
        help=(
            "Read-only: generate scope CSV via Cloud dependentEntitiesGraph "
            "+ content scan of graph candidates"
        ),
    )
    p_discover.add_argument(
        "--output",
        help="Generated scope CSV; default: input/discovered-scope-<GD_WORKSPACE>.csv",
    )
    p_discover.add_argument(
        "--native-only",
        action="store_true",
        help=(
            "Scan only objects owned by GD_WORKSPACE (origin=NATIVE); skip inherited "
            "objects. Use on child workspaces — inherited content is migrated in the parent."
        ),
    )

    p_apply = sub.add_parser("apply", help="Apply a successful plan using individual Entity API PUTs")
    p_apply.add_argument("--confirm-host", required=True)
    p_apply.add_argument("--confirm-workspace", required=True)

    sub.add_parser("verify", help="Fresh GET verification of the selected run")

    p_rollback = sub.add_parser("rollback", help="Restore successful writes from exact plan backups")
    p_rollback.add_argument("--confirm-host", required=True)
    p_rollback.add_argument("--confirm-workspace", required=True)
    return parser


def resolve_run(args: argparse.Namespace) -> Path:
    if args.run:
        return Path(args.run)
    return latest_run(Path(args.runs_dir))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "self-test":
            cmd_self_test(args)
        elif args.command == "discover":
            host, workspace, token = require_connection()
            rules = load_rules(Path(args.rules))
            output = Path(args.output) if args.output else Path("input") / f"discovered-scope-{workspace}.csv"
            from migration_tool.api import GoodDataApi
            discover_workspace_scope(
                api=GoodDataApi(host, workspace, token),
                rules=rules,
                output_path=output,
                native_only=args.native_only,
            )
            # Prove the generated file is directly consumable by the existing strict scope parser.
            load_scope(output, rules)
        elif args.command == "plan":
            plan(
                scope_path=Path(args.scope),
                rules_path=Path(args.rules),
                runs_dir=Path(args.runs_dir),
            )
        elif args.command == "apply":
            apply(
                run_dir=resolve_run(args),
                confirm_host=args.confirm_host,
                confirm_workspace=args.confirm_workspace,
            )
        elif args.command == "verify":
            ok = verify(run_dir=resolve_run(args))
            if not ok:
                raise SystemExit(1)
        elif args.command == "rollback":
            rollback(
                run_dir=resolve_run(args),
                confirm_host=args.confirm_host,
                confirm_workspace=args.confirm_workspace,
            )
        else:
            parser.error("Unknown command")
    except (RunnerError, Exception) as exc:
        # Keep CLI failures concise. Python traceback is not useful for normal safety stops.
        if isinstance(exc, SystemExit):
            raise
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
