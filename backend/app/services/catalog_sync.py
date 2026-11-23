"""目录同步领域服务：同步不同Catalog或Metastore之间的元数据和表结构。

数据链路位置：元数据同步层，保持多个数据目录的结构一致性。"""

from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.schema_registry import DEMO_TABLE, schema_registry
from app.store import store


class CatalogSyncService(BaseModuleService):
    MODULE = "catalog_sync"
    ENTITY = "同步任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务名称", "源Catalog", "目标Catalog", "同步范围", "同步状态"]
    REQUIRED_FIELDS = ["任务名称", "源Catalog", "目标Catalog"]
    STATUS_ORDER = ["待同步", "同步中", "已同步", "同步失败"]
    ACTION_RULES = {"启动同步": "待同步", "取消同步": "同步中"}
    NEGATIVE_ACTIONS = []

    # ---------- Schema变更回退的目录环节 ----------
    def pending_schema_rollbacks(self, entry_id: int | None = None) -> dict[str, Any]:
        """目录待处理清单里的Schema变更回退项。

        值班员据此看到哪一列正在回退、备份列是什么、要恢复成什么定义。
        """
        items = schema_registry.catalog_pending()
        if entry_id is not None:
            items = [item for item in items if item.get("change_id") == entry_id]
        return {
            "scope": "catalog_sync",
            "pending_count": len(items),
            "items": items,
            "resolved": schema_registry.catalog_resolved(),
        }

    def confirm_schema_rollback(self, entry_id: int, change_id: int) -> dict[str, Any]:
        """目录确认回退：使用备份列定义更新目标数据定义。

        只更新回退涉及的列；同表其它已独立发布的列（如 channel）
        保持目录中现有定义继续可读。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "同步任务不存在"}
        pending = [
            item for item in schema_registry.catalog_pending()
            if item.get("change_id") == change_id
        ]
        if not pending:
            return {"ok": False, "message": "该变更不在目录待处理清单中"}
        item = pending[0]
        catalog = item.get("catalog", entry.get("目标Catalog", ""))
        # 目录确认与变更结案走同一条路径，避免两处状态各自推进。
        # 局部导入，避免 services 注册表与本模块互相依赖。
        from app.services import service_for
        schema_service = service_for("schema_change")
        result = schema_service.confirm_rollback(change_id, catalog=catalog)
        if not result.get("ok"):
            return result
        restored = result["restored"]
        entry["同步状态"] = f"回退已确认: {restored['column']}"
        entry["pending"] = bool(schema_registry.catalog_pending())
        return {
            "ok": True,
            "task_id": entry_id,
            "change_id": change_id,
            "catalog": catalog,
            "table": restored["table"],
            "column": restored["column"],
            "backup_column": restored["backup_column"],
            "target_columns": restored["catalog_columns"],
            "pending_after": schema_registry.catalog_pending(),
            "message": result.get("message", ""),
        }

    def apply_sync_schema(self, entry_id: int, table: str = DEMO_TABLE,
                          source_columns: dict[str, str] | None = None) -> dict[str, Any]:
        """按同步来源的列定义更新目标数据定义（含迟到同步的重放）。

        回退已确认的列会忽略同步携带的旧定义，只保留恢复后的定义。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "同步任务不存在"}
        catalog = entry.get("目标Catalog", "")
        # 不指定列定义时，使用源端滞留的旧定义，这正是迟到同步会携带的内容。
        record = schema_registry.apply_sync(table, source_columns, catalog=catalog)
        entry["同步状态"] = "已完成" if not record["skipped"] else "部分应用-列定义已锁定"
        entry["pending"] = bool(record["skipped"])
        entry["abnormal"] = bool(record["skipped"])
        skipped_columns = "、".join(item["column"] for item in record["skipped"])
        return {
            "ok": True,
            "task_id": entry_id,
            "table": table,
            "catalog": catalog,
            "applied": record["applied"],
            "skipped": record["skipped"],
            "message": "同步已落地" if not record["skipped"]
            else f"{skipped_columns} 的同步定义被跳过：回退办理/锁定期间只保留恢复后的定义",
        }

    def sync_usage_report(self, table: str = DEMO_TABLE,
                          catalog: str = "aws_glue_catalog") -> dict[str, Any]:
        """目录与血缘里实际使用的列定义版本，以及读取可见性。"""
        columns = schema_registry.table_columns(table)
        usage = []
        for column, column_type in columns.items():
            state = schema_registry.column_read_state(table, column)
            usage.append({
                "column": column,
                "defined_type": column_type,
                "definition_source": state.get("definition_source", state.get("state")),
                "readable": state["readable"],
                "rollback_locked": state.get("rollback_locked", False),
            })
        catalog_lineage = [
            row for row in store.rows("lineage_graph")
            if row.get("目标表") == table
        ]
        return {
            "table": table,
            "catalog_columns": schema_registry.catalog_columns(catalog, table),
            "columns": usage,
            "lineage_versions": [
                {
                    "lineage_id": row.get("id"),
                    "column_version": row.get("列定义版本", ""),
                    "read_path": row.get("读取路径", ""),
                }
                for row in catalog_lineage
            ],
            "catalog_pending": schema_registry.catalog_pending(),
            "sync_history": schema_registry.catalog_history(),
        }

    def start_sync(self, entry_id: int, incremental: bool = True) -> dict[str, Any]:
        """启动目录同步任务。

        incremental=True 时只同步上次同步以来发生变更的表，
        incremental=False 时全量重新同步。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "同步任务不存在"}
        if entry.get("status") != "待同步":
            return {"ok": False, "message": "只能启动待同步状态的同步任务"}

        source_catalog = entry.get("源Catalog", "")
        target_catalog = entry.get("目标Catalog", "")
        sync_scope = entry.get("同步范围", "")

        entry["status"] = "同步中"
        tables_to_sync = self._resolve_sync_tables(source_catalog, sync_scope)
        return {
            "ok": True,
            "task_id": entry_id,
            "source_catalog": source_catalog,
            "target_catalog": target_catalog,
            "incremental": incremental,
            "tables_to_sync": len(tables_to_sync),
            "estimated_duration_minutes": 12,
            "tables": tables_to_sync[:20],
        }

    def validate_sync_completeness(self, entry_id: int) -> dict[str, Any]:
        """验证同步完整性。

        对比源和目标Catalog中的表列表、Schema和分区定义，
        检测遗漏、不一致或版本差异。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "同步任务不存在"}

        checks = [
            {"check": "table_list_match", "source_count": 320, "target_count": 320, "matched": True, "mismatched_tables": []},
            {"check": "schema_match", "tables_with_diff": 2, "diff_details": [
                {"table": "order_fact_v3", "source_columns": 48, "target_columns": 47,
                 "missing_in_target": ["promotion_code"]},
                {"table": "user_behavior_log", "source_columns": 22, "target_columns": 22,
                 "type_diff": ["event_timestamp: TIMESTAMP vs STRING"]},
            ], "matched": False},
            {"check": "partition_match", "total_tables": 320, "mismatched": 0, "matched": True},
            {"check": "table_properties_match", "mismatched": 1, "details": [
                {"table": "dashboard_cache", "prop": "format-version", "source": "2", "target": "1"}
            ], "matched": False},
        ]
        all_ok = all(c["matched"] for c in checks)
        if all_ok:
            entry["status"] = "已同步"
            entry["同步状态"] = "已完成"
            entry["pending"] = False
        else:
            entry["abnormal"] = True

        return {
            "ok": all_ok,
            "task_id": entry_id,
            "checks": checks,
            "verdict": "同步完整" if all_ok else "存在差异需要修复",
        }

    def detect_schema_conflicts(self, entry_id: int) -> dict[str, Any]:
        """检测目录同步中的Schema冲突。

        当源和目标Catalog对同一表的Schema定义不一致时，
        列出冲突详情并提供解决建议。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "同步任务不存在"}

        return {
            "task_id": entry_id,
            "conflict_count": 3,
            "conflicts": [
                {
                    "table": "user_events",
                    "conflict_type": "column_type_mismatch",
                    "source": "event_data: MAP<STRING,STRING>",
                    "target": "event_data: STRING",
                    "resolution": "建议以源端类型为准，目标端已过期",
                },
                {
                    "table": "dim_customer",
                    "conflict_type": "missing_column",
                    "column": "loyalty_tier",
                    "resolution": "在目标端添加该列，默认值设为'standard'",
                },
                {
                    "table": "sales_summary",
                    "conflict_type": "partition_diff",
                    "source_partition": "dt",
                    "target_partition": "date_str",
                    "resolution": "按源端分区键重新创建目标表",
                },
            ],
            "auto_resolvable": 2,
            "manual_intervention_required": 1,
        }

    def sync_with_lineage(self, entry_id: int) -> dict[str, Any]:
        """同步完成后更新血缘关系。

        将同步范围内的表在目标Catalog的位置注册到血缘图谱，
        建立源→目标的跨Catalog血缘链路。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "同步任务不存在"}

        lineage_rows = store.rows("lineage_graph")
        start_id = max((int(r.get("id", 0)) for r in lineage_rows), default=0) + 1
        new_lineages = []
        for i in range(3):
            lid = start_id + i
            lineage_rows.append({
                "id": lid,
                "status": "已建立",
                "目标表": f"{entry.get('目标Catalog')}_{entry.get('同步范围', 'table')}_{i}",
                "来源表列表": f"{entry.get('源Catalog')}_{entry.get('同步范围', 'table')}_{i}",
                "转换逻辑": f"catalog_sync_{entry_id}",
                "中间层级": "1",
                "血缘深度": "1",
                "pending": False,
                "abnormal": False,
            })
            new_lineages.append(lid)

        return {
            "ok": True,
            "task_id": entry_id,
            "new_lineage_ids": new_lineages,
            "message": f"已注册 {len(new_lineages)} 条血缘关系",
        }

    def _resolve_sync_tables(self, catalog: str, scope: str) -> list[str]:
        base_tables = [
            "order_fact", "user_dim", "product_dim", "sales_agg_daily",
            "inventory_snapshot", "marketing_campaign", "web_traffic_log",
            "customer_360", "supplier_master", "finance_ledger",
        ]
        if scope and scope != "全部":
            return [t for t in base_tables if scope in t]
        return base_tables