"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import migration, resource

# 迁移/回放路由含更具体的固定路径，放在通用资源路由之前注册，
# 避免 /api/{module} 动态路径抢先匹配 /api/migration/... 等端点。
ROUTERS = [migration, resource]
