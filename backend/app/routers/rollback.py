"""Schema回退与报表读取接口：回退依据、目录确认、读取路径和同步重放。

这些路径固定在 /api 下，注册顺序必须排在通用资源路由之前，
否则 /api/{module}/{entry_id} 会先匹配两段路径导致 422。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services import service_for
from app.services.report_read import report_read_service
from app.services.schema_registry import DEMO_TABLE, schema_registry

router = APIRouter(prefix="/api", tags=["Schema回退与报表读取"])


class ConfirmRollbackPayload(BaseModel):
    change_id: int


class ApplySyncPayload(BaseModel):
    table: str = DEMO_TABLE
    source_columns: dict[str, str] = Field(default_factory=dict)


@router.get("/schema_change/{entry_id}/rollback-evidence", response_model=dict)
def rollback_evidence(entry_id: int) -> dict[str, Any]:
    """回退依据：具体变更、备份列、目录待处理项和确认状态。"""
    service = service_for("schema_change")
    result = service.rollback_evidence(entry_id)
    if result.get("change_id") is None and result.get("ok") is False:
        raise HTTPException(status_code=404, detail=result.get("message", "变更记录不存在"))
    return result


@router.get("/catalog_sync/{entry_id}/pending-rollbacks", response_model=dict)
def pending_rollbacks(entry_id: int) -> dict[str, Any]:
    """目录待处理清单中的Schema回退项。"""
    service = service_for("catalog_sync")
    return service.pending_schema_rollbacks(entry_id)


@router.post("/catalog_sync/{entry_id}/confirm-rollback", response_model=ActionResult)
def confirm_rollback(entry_id: int, payload: ConfirmRollbackPayload) -> ActionResult:
    """目录确认回退：用备份列定义更新目标数据定义并正式结案。"""
    service = service_for("catalog_sync")
    result = service.confirm_schema_rollback(entry_id, payload.change_id)
    entry = service.get_entry(entry_id) if result.get("ok") else None
    return ActionResult(ok=bool(result.get("ok")), message=result.get("message", ""), entry=entry)


@router.post("/catalog_sync/{entry_id}/apply-schema", response_model=ActionResult)
def apply_sync_schema(entry_id: int, payload: ApplySyncPayload) -> ActionResult:
    """把同步（含迟到重放）的列定义落到目标数据定义，回退锁定的列保持恢复后定义。"""
    service = service_for("catalog_sync")
    result = service.apply_sync_schema(entry_id, payload.table,
                                       payload.source_columns or None)
    entry = service.get_entry(entry_id) if result.get("ok") else None
    return ActionResult(ok=bool(result.get("ok")), message=result.get("message", ""), entry=entry)


@router.get("/catalog_sync/{entry_id}/sync-usage", response_model=dict)
def sync_usage(entry_id: int, table: str = Query(default=DEMO_TABLE)) -> dict[str, Any]:
    """目录与血缘中实际使用的列定义版本和读取可见性。"""
    service = service_for("catalog_sync")
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"同步任务 {entry_id} 不存在")
    payload = service.sync_usage_report(
        table, service.get_entry(entry_id).get("目标Catalog", "aws_glue_catalog"))
    payload["task_id"] = entry_id
    return payload


@router.get("/report/amount-sum", response_model=dict)
def amount_sum(table: str = Query(default=DEMO_TABLE)) -> dict[str, Any]:
    """报表读路径：金额合计；金额回退办理中返回回退状态。"""
    return report_read_service.amount_sum(table)


@router.get("/report/channel-data", response_model=dict)
def channel_data(table: str = Query(default=DEMO_TABLE)) -> dict[str, Any]:
    """报表读路径：渠道数据；未撤销的列在回退期间保持可读。"""
    return report_read_service.channel_data(table)


@router.get("/report/column-definitions", response_model=dict)
def column_definitions(table: str = Query(default=DEMO_TABLE)) -> dict[str, Any]:
    """报表实际使用的列定义与来源。"""
    return report_read_service.column_definitions(table)


@router.get("/schema_change/{entry_id}/change-record", response_model=dict)
def change_record(entry_id: int) -> dict[str, Any]:
    """变更登记簿中的列级记录和备份列。"""
    record = schema_registry.change_record(entry_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"变更 {entry_id} 没有列级登记")
    return {
        "change_id": entry_id,
        "record": record,
        "backup_columns": schema_registry.backup_columns(entry_id),
    }
