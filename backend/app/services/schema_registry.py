"""Schema变更登记簿：保存目标表的列级定义、变更记录与备份列，供目录同步和报表读取共用。

真实项目里这些元数据存放在 Metastore / Catalog 中，这里用内存仓库模拟，
但把「列定义 → 变更 → 备份列 → 目录落地 → 回退锁定」的因果链完整保留下来，
便于核对回退期间实际生效的列定义。
"""
from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

from app.store import store

# 演示表：订单事实表。金额列经历过 DECIMAL(10,2) -> STRING 的改型，
# channel 列是后来独立发布的变更，两者必须能分别回退、分别读取。
DEMO_TABLE = "order_fact_v3"
AMOUNT_CHANGE_ID = 4
CHANNEL_CHANGE_ID = 5
DEMO_CATALOG = "aws_glue_catalog"

# 真实列定义：列名 → 类型。
TABLE_SCHEMAS: dict[str, dict[str, str]] = {
    # channel 由后来独立发布的 ADD_COLUMN 变更引入，初始定义里没有它。
    DEMO_TABLE: {
        "order_id": "BIGINT",
        "amount": "DECIMAL(10,2)",
        "dt": "STRING",
    },
}

# 演示行数据：金额以 DECIMAL 语义存储（数值），改型后由读取路径负责转换。
TABLE_DATA: dict[str, list[dict[str, Any]]] = {
    DEMO_TABLE: [
        {"order_id": 1001, "amount": 12.50, "channel": "app", "dt": "2026-11-20"},
        {"order_id": 1002, "amount": 3.25, "channel": "web", "dt": "2026-11-20"},
    ],
}


class SchemaRegistry:
    """列级Schema登记簿，兼顾变更历史、备份列、回退锁定和目录待处理清单。"""

    def __init__(self) -> None:
        self._tables: dict[str, dict[str, Any]] = {}
        self._backups: dict[int, list[dict[str, Any]]] = {}
        self._catalog_pending: list[dict[str, Any]] = []
        self._catalog_resolved: list[dict[str, Any]] = []
        self._catalog_history: list[dict[str, Any]] = []
        self._catalog_targets: dict[str, dict[str, str]] = {}
        self._pinned: dict[str, dict[str, Any]] = {}
        self._late_sync_source: dict[str, dict[str, str]] = {}
        self._change_seq = 0
        self._rollback_state: dict[int, dict[str, Any]] = {}
        self._bootstrap()

    # ---------- 初始化演示数据 ----------
    def _bootstrap(self) -> None:
        """按真实时间顺序登记两次独立发布的变更。"""
        self.ensure_table(DEMO_TABLE)
        self.register_change(
            DEMO_TABLE, AMOUNT_CHANGE_ID, "amount",
            old_type="DECIMAL(10,2)", new_type="STRING",
        )
        # channel 是后来独立发布的 ADD_COLUMN，备份里记下它新增前的「不存在」。
        self.register_change(
            DEMO_TABLE, CHANNEL_CHANGE_ID, "channel",
            old_type="", new_type="STRING", is_add=True,
            previous_absent=True,
        )
        # 目录同步已经按最新定义落地过一次，所以目标目录当前也是 STRING。
        self.set_catalog_columns(DEMO_CATALOG, DEMO_TABLE, self.table_columns(DEMO_TABLE))
        # 源端Catalog的列定义停留在改型完成后、回退之前的状态：
        # 回退后重放的迟到同步携带的就是这份定义。
        self._late_sync_source[DEMO_TABLE] = self.table_columns(DEMO_TABLE)

    # ---------- 表与列定义 ----------
    def table_columns(self, table: str) -> dict[str, str]:
        return dict(self._tables.get(table, {}).get("columns", {}))

    def table_data(self, table: str) -> list[dict[str, Any]]:
        return copy.deepcopy(TABLE_DATA.get(table, []))

    def ensure_table(self, table: str) -> dict[str, Any]:
        if table not in self._tables:
            self._tables[table] = {
                "columns": dict(TABLE_SCHEMAS.get(table, {})),
                "definitions": [],  # 列定义的版本历史
            }
        return self._tables[table]

    # ---------- 变更登记 ----------
    def register_change(self, table: str, change_id: int, column: str, old_type: str,
                        new_type: str, is_add: bool = False,
                        previous_absent: bool = False) -> int:
        self._change_seq += 1
        change_seq = self._change_seq
        table_state = self.ensure_table(table)
        snapshot = dict(table_state["columns"])
        self._backups[change_id] = [
            {
                "column": column,
                "type": old_type,
                "snapshot": snapshot,
                "backup_column": f"{column}__bak_{change_id}",
                "is_add": is_add,
                "previous_absent": previous_absent,
            },
        ]
        table_state["definitions"].append({
            "change_id": change_id,
            "column": column,
            "from": old_type,
            "to": new_type,
            "seq": change_seq,
            "state": "已执行",
        })
        table_state["columns"][column] = new_type
        return change_seq

    def backup_columns(self, change_id: int) -> list[dict[str, Any]]:
        return copy.deepcopy(self._backups.get(change_id, []))

    def change_record(self, change_id: int) -> dict[str, Any] | None:
        for table_state in self._tables.values():
            for item in table_state.get("definitions", []):
                if item.get("change_id") == change_id:
                    return copy.deepcopy(item)
        return None

    # ---------- 回退办理 ----------
    def open_rollback(self, change_id: int) -> dict[str, Any] | None:
        """提交回退：进入回退办理态，等待目录确认。该列在办理期间不可读。"""
        backups = self._backups.get(change_id, [])
        if not backups:
            return None
        backup = backups[0]
        for table, state in self._tables.items():
            if backup["column"] not in state["columns"]:
                continue
            record = self.change_record(change_id) or {}
            if record.get("state") == "已恢复":
                return None
            rolling = state.setdefault("rolling_back", {})
            if rolling.get(backup["column"]) == change_id:
                return None
            rolling[backup["column"]] = change_id
            item = {
                "change_id": change_id,
                "catalog": DEMO_CATALOG,
                "table": table,
                "column": backup["column"],
                "backup_column": backup["backup_column"],
                "restore_to": backup["type"],
                "current": state["columns"].get(backup["column"], ""),
                "state": "待目录确认",
            }
            self.add_catalog_pending(item)
            self._rollback_state[change_id] = item
            return {"table": table, **item}
        return None

    def restore_column(self, change_id: int, catalog: str = DEMO_CATALOG) -> dict[str, Any] | None:
        """目录确认后按备份列恢复该列定义；其它列（如后来新增的 channel）不受影响。"""
        backups = self._backups.get(change_id, [])
        if not backups:
            return None
        backup = backups[0]
        for table, state in self._tables.items():
            if backup["column"] not in state["columns"]:
                continue
            # 使用备份列定义更新目标数据定义：ADD_COLUMN 的回退意味着该列重新不存在。
            if backup.get("previous_absent"):
                state["columns"].pop(backup["column"], None)
            else:
                state["columns"][backup["column"]] = backup["type"]
            state.get("rolling_back", {}).pop(backup["column"], None)
            for item in state["definitions"]:
                if item.get("change_id") == change_id:
                    item["state"] = "已恢复"
            # 目录侧只改这一列，保留目录中其它已同步列（例如 channel）
            catalog_columns = self._catalog_targets.setdefault(catalog, {}).setdefault(table, {})
            if backup.get("previous_absent"):
                catalog_columns.pop(backup["column"], None)
            else:
                catalog_columns[backup["column"]] = backup["type"]
            # 回退确认后给该列上锁：迟到的同步不能把它改回去
            self._pinned[f"{table}.{backup['column']}"] = {
                "change_id": change_id,
                "type": backup["type"],
                "catalog": catalog,
                "backup_column": backup["backup_column"],
            }
            self.resolve_catalog_pending(change_id, catalog)
            self._rollback_state.pop(change_id, None)
            self.refresh_lineage_versions(table)
            return {
                "table": table,
                "column": backup["column"],
                "restored_type": backup["type"],
                "dropped": bool(backup.get("previous_absent")),
                "backup_column": backup["backup_column"],
                "columns_after": dict(state["columns"]),
                "catalog_columns": dict(catalog_columns),
            }
        return None

    def rollback_status(self, change_id: int) -> dict[str, Any] | None:
        return copy.deepcopy(self._rollback_state.get(change_id))

    def column_locked(self, table: str, column: str) -> dict[str, Any] | None:
        return copy.deepcopy(self._pinned.get(f"{table}.{column}"))

    def pinned_columns(self, table: str) -> dict[str, Any]:
        prefix = f"{table}."
        return {
            key[len(prefix):]: copy.deepcopy(value)
            for key, value in self._pinned.items()
            if key.startswith(prefix)
        }

    # ---------- 读取路径 ----------
    def rollback_column(self, table: str) -> str | None:
        """返回该表当前处于回退办理中的列名。"""
        rolling = self._tables.get(table, {}).get("rolling_back", {})
        return next(iter(rolling), None)

    def column_read_state(self, table: str, column: str) -> dict[str, Any]:
        """列的可读状态：回退办理期间被撤销的列不可读，其它列照常可读。"""
        columns = self._tables.get(table, {}).get("columns", {})
        if column not in columns:
            return {"readable": False, "column": column, "state": "列不存在",
                    "reason": f"{table}.{column} 不在当前列定义中"}
        rolling = self._tables.get(table, {}).get("rolling_back", {})
        if column in rolling:
            change_id = rolling[column]
            return {
                "readable": False,
                "column": column,
                "defined_type": columns[column],
                "state": "回退办理中",
                "change_id": change_id,
                "catalog_pending": True,
                "reason": f"列 {column} 正在回退，目标目录尚未确认，暂不可读",
            }
        lock = self._pinned.get(f"{table}.{column}")
        return {
            "readable": True,
            "column": column,
            "defined_type": columns[column],
            "definition_source": "backup_column" if lock else "current",
            "rollback_locked": bool(lock),
            "change_id": lock.get("change_id") if lock else None,
        }

    def read_amount_sum(self, table: str, column: str = "amount") -> dict[str, Any]:
        """报表读路径：按当前列定义读取金额合计；列不可读时返回回退状态。"""
        state = self.column_read_state(table, column)
        if not state["readable"]:
            reason = state["reason"]
            if column == "amount":
                reason += "，金额暂不可读"
            return {
                "ok": False,
                "table": table,
                "column": column,
                "state": state["state"],
                "change_id": state.get("change_id"),
                "rollback": self._rollback_state.get(state.get("change_id") or -1),
                "catalog_pending": self.catalog_pending(),
                "message": reason,
            }
        total = Decimal("0")
        rows = 0
        for row in TABLE_DATA.get(table, []):
            value = row.get(column)
            if value is None or value == "":
                continue
            total += Decimal(str(value))
            rows += 1
        return {
            "ok": True,
            "table": table,
            "column": column,
            "defined_type": state["defined_type"],
            "definition_source": state["definition_source"],
            "rollback_locked": state["rollback_locked"],
            "row_count": rows,
            "sum": f"{total:.2f}",
        }

    def read_channels(self, table: str, column: str = "channel") -> dict[str, Any]:
        """报表读路径：读取渠道维度；未撤销的列在回退办理期间仍可读。"""
        state = self.column_read_state(table, column)
        if not state["readable"]:
            return {"ok": False, "table": table, "column": column,
                    "state": state["state"], "message": state["reason"]}
        values = [row.get(column) for row in TABLE_DATA.get(table, []) if row.get(column)]
        return {
            "ok": True,
            "table": table,
            "column": column,
            "defined_type": state["defined_type"],
            "definition_source": state["definition_source"],
            "row_count": len(values),
            "values": values,
            "distinct": sorted(set(values)),
        }

    # ---------- 同步落地 ----------
    def refresh_lineage_versions(self, table: str) -> None:
        """把当前实际生效的列定义写回血缘，目录和血缘查到同一个版本。"""
        columns = self.table_columns(table)
        version = ", ".join(f"{name}:{typ}" for name, typ in columns.items())
        for row in store.rows("lineage_graph"):
            if row.get("目标表") == table:
                row["列定义版本"] = version
                row["读取路径"] = "report_order_amount_sum"

    def apply_sync(self, table: str, source_columns: dict[str, str] | None = None,
                   catalog: str = DEMO_CATALOG) -> dict[str, Any]:
        """把一次同步的列定义落到目标数据定义。

        回退确认后被锁定的列只保留恢复后的定义：迟到的同步携带旧定义时
        会被跳过并记录冲突，不会把该列改回改型后的类型。
        """
        state = self.ensure_table(table)
        rolling = state.setdefault("rolling_back", {})
        source_columns = source_columns or self._late_sync_source.get(
            table, self.table_columns(table))
        applied: dict[str, str] = {}
        skipped: list[dict[str, Any]] = []
        for column, column_type in source_columns.items():
            if column in rolling:
                skipped.append({
                    "column": column,
                    "incoming": column_type,
                    "kept": state["columns"].get(column, ""),
                    "reason": "该列正在回退办理，暂不接受同步定义",
                    "change_id": rolling[column],
                })
                continue
            lock = self._pinned.get(f"{table}.{column}")
            if lock is not None and column_type != lock["type"]:
                skipped.append({
                    "column": column,
                    "incoming": column_type,
                    "kept": lock["type"],
                    "reason": "回退已确认，该列锁定为恢复后的定义",
                    "change_id": lock["change_id"],
                })
                continue
            state["columns"][column] = column_type
            applied[column] = column_type
        target = self._catalog_targets.setdefault(catalog, {}).setdefault(table, {})
        target.update(applied)
        record = {
            "table": table,
            "catalog": catalog,
            "applied": applied,
            "skipped": skipped,
            "catalog_columns": dict(target),
        }
        self._catalog_history.append(record)
        self.refresh_lineage_versions(table)
        return record

    def catalog_history(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._catalog_history)

    # ---------- 回退依据 ----------
    def evidence(self, change_id: int) -> dict[str, Any]:
        record = self.change_record(change_id)
        return {
            "change_id": change_id,
            "change": record,
            "backup_columns": self.backup_columns(change_id),
            "rollback": self.rollback_status(change_id),
            "catalog_pending": [
                item for item in self.catalog_pending() if item.get("change_id") == change_id
            ],
            "catalog_history": [
                item for item in self._catalog_history
                if any(s.get("change_id") == change_id for s in item.get("skipped", []))
                or record and record.get("column") in item.get("applied", {})
            ],
        }

    # ---------- 目录待处理清单 ----------
    def catalog_pending(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._catalog_pending)

    def add_catalog_pending(self, item: dict[str, Any]) -> None:
        item.setdefault("state", "待目录确认")
        self._catalog_pending.append(item)

    def resolve_catalog_pending(self, change_id: int, catalog: str) -> list[dict[str, Any]]:
        """目录确认后出清对应待处理项，返回仍待处理的部分。"""
        remaining = [
            item for item in self._catalog_pending
            if not (item.get("change_id") == change_id and item.get("catalog") == catalog)
        ]
        resolved = [
            item for item in self._catalog_pending
            if item.get("change_id") == change_id and item.get("catalog") == catalog
        ]
        for item in resolved:
            self._catalog_resolved.append({**item, "state": "目录已确认"})
        self._catalog_pending = remaining
        return remaining

    def catalog_resolved(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._catalog_resolved)

    # ---------- 目标目录定义 ----------
    def catalog_columns(self, catalog: str, table: str) -> dict[str, str]:
        return dict(self._catalog_targets.get(catalog, {}).get(table, {}))

    def set_catalog_columns(self, catalog: str, table: str, columns: dict[str, str]) -> None:
        self._catalog_targets.setdefault(catalog, {})[table] = dict(columns)


schema_registry = SchemaRegistry()
