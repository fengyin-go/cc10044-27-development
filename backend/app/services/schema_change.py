"""Schema变更管理领域服务：管理表Schema的变更申请、兼容性检查和回退执行。

数据链路位置：架构管理层，控制数据表结构变更的范围和影响。"""

from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.schema_registry import schema_registry
from app.store import store


class SchemaChangeService(BaseModuleService):
    MODULE = "schema_change"
    ENTITY = "变更记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["目标表", "变更类型", "变更内容", "影响下游表数", "变更时间", "回退状态"]
    REQUIRED_FIELDS = ["目标表", "变更类型", "变更内容"]
    STATUS_ORDER = ["待审批", "已执行", "已回退"]
    # 回退变更进入两段式流程（提交办理 -> 目录确认结案），由 run_action 覆写分派。
    ACTION_RULES = {"执行变更": "待审批", "回退变更": "已回退"}
    NEGATIVE_ACTIONS = []

    CHANGE_TYPES = frozenset({"ADD_COLUMN", "DROP_COLUMN", "MODIFY_TYPE", "RENAME_COLUMN", "RENAME_TABLE"})

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记变更记录：回退状态默认「未提交」，避免新记录出现在待办里。"""
        entry, missing = super().create_entry(values)
        if entry is not None:
            entry["回退状态"] = str(values.get("回退状态") or "").strip() or "未提交"
        return entry, missing

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """动作分派：执行变更保留备份，回退变更进入待目录确认的办理态。"""
        handlers = {
            "执行变更": self.execute_with_backup,
            "回退变更": self.rollback_change,
            "确认回退": self.confirm_rollback,
        }
        handler = handlers.get(action)
        if handler is None:
            return super().run_action(entry_id, action)
        result = handler(entry_id)
        entry = store.find(self.MODULE, entry_id) if result.get("ok") else None
        return entry, result.get("message", "变更已处理")

    def assess_compatibility(self, entry_id: int) -> dict[str, Any]:
        """评估Schema变更的兼容性。

        检查变更是否向下兼容（新增可空列/重命名）或是破坏性变更（删除列/修改类型）。
        破坏性变更要求列出所有受影响的依赖。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "变更记录不存在"}

        change_type = entry.get("变更类型", "")
        change_content = entry.get("变更内容", "")
        target_table = entry.get("目标表", "")

        backward_compatible = change_type in ("ADD_COLUMN", "RENAME_COLUMN")
        breaking = change_type in ("DROP_COLUMN", "MODIFY_TYPE", "RENAME_TABLE")

        lineage_rows = store.rows("lineage_graph")
        affected_downstream = [
            l for l in lineage_rows
            if l.get("目标表") == target_table or target_table in l.get("来源表列表", "").split(",")
        ]

        assessment = {
            "change_id": entry_id,
            "change_type": change_type,
            "target_table": target_table,
            "change_content": change_content,
            "backward_compatible": backward_compatible,
            "breaking_change": breaking,
            "affected_downstream_tables": len(affected_downstream),
            "affected_tables_detail": [
                {"table": l.get("目标表"), "lineage_id": l.get("id")}
                for l in affected_downstream[:10]
            ],
            "recommendation": "可以安全执行" if backward_compatible and not breaking
            else "需要在下游表协调后执行，建议先创建兼容视图",
        }
        entry["影响下游表数"] = str(len(affected_downstream))
        return assessment

    def execute_with_backup(self, entry_id: int) -> dict[str, Any]:
        """执行Schema变更并保留备份快照。

        执行前自动创建目标表的Schema快照，
        变更失败时可据此回退。快照记录在 snapshot_creator 模块。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "变更记录不存在"}
        if entry.get("status") != "待审批":
            return {"ok": False, "message": "只有待审批的变更记录才能执行"}

        assessment = self.assess_compatibility(entry_id)

        snapshot_rows = store.rows("snapshot_creator")
        snap_id = max((int(r.get("id", 0)) for r in snapshot_rows), default=0) + 1
        snapshot_rows.append({
            "id": snap_id,
            "status": "已完成",
            "目标数据集": f"{entry.get('目标表')}_schema",
            "快照时间点": "2026-11-21T08:00:00Z",
            "快照类型": "schema_backup",
            "存储路径": f"/schemas/backup/{entry.get('目标表')}_pre_change_{entry_id}",
            "文件大小": "0.01 GB",
            "pending": False,
            "abnormal": False,
        })

        entry["status"] = "已执行"
        entry["变更时间"] = "2026-11-21T08:00:00Z"
        entry["pending"] = False

        return {
            "ok": True,
            "change_id": entry_id,
            "backup_snapshot_id": snap_id,
            "assessment": assessment,
            "message": f"Schema变更已执行，备份快照ID: {snap_id}",
        }

    def rollback_change(self, entry_id: int) -> dict[str, Any]:
        """提交回退已执行的Schema变更（第一步：进入回退办理）。

        使用执行前创建的备份快照恢复原始Schema，记录回退原因和影响范围。
        回退不会直接结案：先登记到目录待处理清单，等目标目录确认后
        由 confirm_rollback 正式恢复列定义。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "变更记录不存在"}
        if entry.get("status") == "已回退":
            return {"ok": False, "message": "该变更已回退"}
        if entry.get("status") != "已执行":
            return {"ok": False, "message": "只有已执行的变更记录才能回退"}
        if entry.get("回退状态") == "待目录确认":
            return {"ok": False, "message": "回退已提交，等待目标目录确认"}

        table = entry.get("目标表", "")
        submitted = schema_registry.open_rollback(entry_id)
        if submitted is None:
            # 历史记录没有列级备份时，退回快照级回退，保持原有可回退范围。
            return self._rollback_from_snapshot(entry, entry_id)

        backup_columns = schema_registry.backup_columns(entry_id)
        entry["回退状态"] = "待目录确认"
        entry["pending"] = True
        return {
            "ok": True,
            "change_id": entry_id,
            "table": table,
            "backup_columns": backup_columns,
            "rollback": submitted,
            "catalog_pending": schema_registry.catalog_pending(),
            "message": "回退已提交，等待目标目录确认；确认前该列不可读，其它未撤销列保持可读",
        }

    def _rollback_from_snapshot(self, entry: dict[str, Any],
                                entry_id: int) -> dict[str, Any]:
        """兼容没有列级备份的历史记录：按执行前的快照级备份直接回退。"""
        snapshot_rows = store.rows("snapshot_creator")
        backup_snapshots = [
            s for s in snapshot_rows
            if f"_pre_change_{entry_id}" in s.get("存储路径", "")
        ]
        if not backup_snapshots:
            # 历史记录既无列级备份也无快照备份：按原行为登记回退结果。
            entry["status"] = "已回退"
            entry["回退状态"] = "已确认"
            entry["pending"] = False
            entry["abnormal"] = True
            return {
                "ok": True,
                "change_id": entry_id,
                "deployed_snapshot_id": None,
                "message": "变更已回退，未找到备份快照，请人工核对目标表",
            }
        entry["status"] = "已回退"
        entry["回退状态"] = "已确认"
        entry["pending"] = False
        entry["abnormal"] = True
        return {
            "ok": True,
            "change_id": entry_id,
            "deployed_snapshot_id": backup_snapshots[0]["id"],
            "message": "变更已按执行前快照回退",
        }

    def confirm_rollback(self, entry_id: int, catalog: str | None = None) -> dict[str, Any]:
        """目录确认后正式结案（第二步）：按备份列恢复定义并锁定该列。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "变更记录不存在"}
        if entry.get("status") == "已回退":
            return {"ok": False, "message": "该变更已回退"}
        if entry.get("回退状态") != "待目录确认":
            return {"ok": False, "message": "请先提交回退，再等待目录确认"}
        pending = [
            item for item in schema_registry.catalog_pending()
            if item.get("change_id") == entry_id
        ]
        if not pending:
            return {"ok": False, "message": "该变更不在目录待处理清单中"}

        restored = schema_registry.restore_column(
            entry_id, catalog=catalog or pending[0].get("catalog", ""))
        if restored is None:
            return {"ok": False, "message": "备份列恢复失败"}

        entry["status"] = "已回退"
        entry["回退状态"] = "已确认"
        entry["pending"] = False
        entry["abnormal"] = True
        entry["变更时间"] = "2026-11-23T10:00:00Z"
        action = (f"移除列 {restored['column']}" if restored.get("dropped")
                  else f"把 {restored['column']} 恢复为 {restored['restored_type']}")
        return {
            "ok": True,
            "change_id": entry_id,
            "restored": restored,
            "catalog_pending": schema_registry.catalog_pending(),
            "message": f"目录已确认，按备份列 {restored['backup_column']} {action}，该列定义已锁定",
        }

    def rollback_evidence(self, entry_id: int) -> dict[str, Any]:
        """回退依据：具体变更内容、备份列和目录确认状态，供结案前核对。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "变更记录不存在"}
        evidence = schema_registry.evidence(entry_id)
        evidence["entry"] = {
            "id": entry.get("id"),
            "status": entry.get("status"),
            "回退状态": entry.get("回退状态"),
            "目标表": entry.get("目标表"),
            "变更类型": entry.get("变更类型"),
            "变更内容": entry.get("变更内容"),
        }
        evidence["can_close"] = (
            entry.get("回退状态") == "待目录确认"
            and bool(evidence["catalog_pending"])
        )
        return evidence

    def list_pending_changes_by_table(self, table_name: str) -> dict[str, Any]:
        """查询指定表的待处理Schema变更列表。

        按变更时间排序，标注每个变更的兼容性风险等级。
        """
        all_changes = store.rows(self.MODULE)
        pending = [c for c in all_changes if c.get("目标表") == table_name and c.get("status") == "待审批"]
        return {
            "table": table_name,
            "pending_count": len(pending),
            "changes": [
                {
                    "id": c["id"],
                    "type": c.get("变更类型"),
                    "content": c.get("变更内容"),
                    "risk": "high" if c.get("变更类型") in ("DROP_COLUMN", "RENAME_TABLE") else "low",
                }
                for c in pending
            ],
        }

    def reconcile_with_catalog_sync(self, entry_id: int) -> dict[str, Any]:
        """对账Schema变更与Catalog同步状态。

        检查变更后的Schema是否已成功同步到目标Catalog，
        未同步时生成同步提醒。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "变更记录不存在"}
        if entry.get("status") != "已执行":
            return {"ok": False, "message": "只有已执行的变更才需要对账"}

        sync_rows = store.rows("catalog_sync")
        target_table = entry.get("目标表", "")
        related_syncs = [
            s for s in sync_rows
            if target_table in s.get("同步范围", "") and s.get("status") == "已同步"
        ]

        return {
            "change_id": entry_id,
            "target_table": target_table,
            "synced_to_catalogs": len(related_syncs),
            "catalogs_detail": [
                {"task_name": s.get("任务名称"), "target_catalog": s.get("目标Catalog")}
                for s in related_syncs
            ],
            "all_good": len(related_syncs) > 0,
        }