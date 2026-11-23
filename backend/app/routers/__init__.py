"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, rollback

# rollback 里的固定路径要排在 resource 的 /api/{module}/{entry_id} 之前注册。
ROUTERS = [rollback, resource]
