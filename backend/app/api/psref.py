from fastapi import APIRouter, HTTPException, Query

from app.services.psref_service import psref_service

router = APIRouter(prefix="/psref", tags=["Lenovo PSREF Specifications"])


@router.get("/lookup")
async def lookup_psref_specs(
    code: str = Query(..., min_length=2, description="Machine Type (e.g. 83J2), MTM (e.g. 83J2003NIN), or product name"),
    refresh: bool = Query(False, description="Bypass local SQLite cache and fetch live from psref.lenovo.com"),
):
    """Fetches structured Lenovo PSREF hardware specifications for an MT, MTM, or product name."""
    cleaned = code.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="MT or MTM code cannot be empty.")
    return await psref_service.lookup(cleaned, force_refresh=refresh)


@router.get("/recent")
async def list_recent_psref_lookups(limit: int = Query(12, ge=1, le=50)):
    """Lists recently cached PSREF MT/MTM lookups."""
    return {"items": psref_service.list_recent_cached(limit=limit)}
