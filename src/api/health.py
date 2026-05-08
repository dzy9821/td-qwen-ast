"""
健康检查端点。
"""

from fastapi import APIRouter

from src.api.connection_manager import connection_manager

router = APIRouter(prefix="/api/v1")


@router.get("/health")
async def health() -> dict:
    """服务进程存活检查。"""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict:
    """模型加载就绪状态检查（K8s Readiness Probe）。"""
    # 复用全局 asr_service 实例（由 main.py lifespan 初始化）
    from src.api.websocket import asr_service

    available = await asr_service.is_available()
    if available:
        return {"status": "ready"}
    return {"status": "not_ready", "detail": "vLLM service unreachable"}


@router.get("/connections")
async def connections() -> dict:
    """当前活跃连接数统计。"""
    return {
        "active_connections": connection_manager.active_count,
        "details": connection_manager.active_connections,
    }
