"""``/api/logs`` - interaction log queries; admin only (``X-Admin-Token``)."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from backend.config import get_settings
from backend.log_store import get_log_store

router = APIRouter(prefix="/api/logs", tags=["logs"])


async def verify_admin(x_admin_token: str | None = Header(None)) -> None:
    password = get_settings().admin_password
    if not password:
        raise HTTPException(503, "Admin password not configured")
    provided = x_admin_token or ""
    if not provided or not hmac.compare_digest(provided.encode(), password.encode()):
        raise HTTPException(403, "Unauthorized")


@router.get("/verify")
async def verify_token(x_admin_token: str | None = Header(None)):
    password = get_settings().admin_password
    if not password:
        return JSONResponse(status_code=503, content={"valid": False})
    provided = x_admin_token or ""
    if not provided or not hmac.compare_digest(provided.encode(), password.encode()):
        return JSONResponse(status_code=403, content={"valid": False})
    return {"valid": True}


@router.get("", dependencies=[Depends(verify_admin)])
async def query_logs(
    module: str | None = Query(None),
    client_ip: str | None = Query(None, description="partial match"),
    keyword: str | None = Query(None, description="search in input/output"),
    start_date: str | None = Query(None, description="YYYY-MM-DD"),
    end_date: str | None = Query(None, description="YYYY-MM-DD"),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return get_log_store().query(
        module=module, client_ip=client_ip, keyword=keyword, start_date=start_date,
        end_date=end_date, status=status, limit=limit, offset=offset,
    )


@router.get("/stats", dependencies=[Depends(verify_admin)])
async def log_stats():
    return get_log_store().stats()


@router.delete("", dependencies=[Depends(verify_admin)])
async def delete_old_logs(before: str = Query(..., description="delete logs before YYYY-MM-DD")):
    return {"deleted": get_log_store().delete_before(before), "before": before}
