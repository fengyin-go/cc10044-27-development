"""报表读取领域服务：沿当前读路径取数，并报告实际使用的列定义版本。

数据链路位置：报表消费层。金额列在Schema回退办理期间不可读，
读取接口返回回退状态而不是旧值；其它未撤销列（如渠道）继续可读。"""
from __future__ import annotations

from typing import Any

from app.services.schema_registry import DEMO_TABLE, schema_registry


class ReportReadService:
    MODULE = "report_read"
    ENTITY = "报表读取"

    def __init__(self, table: str = DEMO_TABLE) -> None:
        self.table = table

    def amount_sum(self, table: str | None = None) -> dict[str, Any]:
        """金额合计：金额列回退办理中时返回回退状态，不返回不可靠数值。"""
        return schema_registry.read_amount_sum(table or self.table)

    def channel_data(self, table: str | None = None) -> dict[str, Any]:
        """渠道数据：渠道列未撤销，回退办理期间照常可读。"""
        return schema_registry.read_channels(table or self.table)

    def column_definitions(self, table: str | None = None) -> dict[str, Any]:
        """当前读路径实际使用的列定义及来源。"""
        target = table or self.table
        columns = schema_registry.table_columns(target)
        details = []
        for column in columns:
            state = schema_registry.column_read_state(target, column)
            details.append({
                "column": column,
                "defined_type": state.get("defined_type"),
                "readable": state["readable"],
                "definition_source": state.get(
                    "definition_source", state.get("state")),
                "rollback_locked": state.get("rollback_locked", False),
                "change_id": state.get("change_id"),
            })
        return {
            "table": target,
            "columns": details,
            "catalog_pending": schema_registry.catalog_pending(),
        }


report_read_service = ReportReadService()
