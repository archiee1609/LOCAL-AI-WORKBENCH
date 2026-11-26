from typing import Optional
from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from app.core.config import settings
from app.services.audit_service import audit_service
from app.services.lenovo_kb_service import lenovo_kb_service

router = APIRouter(prefix="/kb", tags=["Lenovo Tips & Resources Knowledge Base"])


def _require_admin(actor_role: str) -> None:
    if (actor_role or "").strip().lower() != "admin":
        raise HTTPException(
            status_code=403,
            detail="Access denied: Only Admin users can modify or manually sync the Knowledge Base.",
        )


@router.get("/stats")
async def get_kb_stats():
    """Returns indexed source files, total records, confirmed QI count, and available segments."""
    return lenovo_kb_service.get_stats()


@router.get("/search")
async def search_kb(
    q: str = Query("", description="Search query (model, MT code, symptom, FRU, PD, or APTT code)"),
    source_type: Optional[str] = Query(None, description="Filter by source_type: known_issue, known_tip, leap_tip, tech_tip, document"),
    segment: Optional[str] = Query(None, description="Filter by segment (e.g. Commercial NB, Consumer NB)"),
    confirmed_only: bool = Query(False, description="Only return Confirmed QI entries"),
    limit: int = Query(15, ge=1, le=100),
):
    """Hybrid search across Lenovo Tips & Resources Knowledge Base."""
    results = lenovo_kb_service.search(
        query=q,
        limit=limit,
        source_type=source_type,
        segment=segment,
        confirmed_only=confirmed_only,
    )
    return {
        "query": q,
        "total_matches": len(results),
        "results": results,
    }


@router.post("/sync")
async def sync_kb(
    force: bool = Query(True),
    actor_role: str = Query("User"),
    actor_username: str = Query("admin"),
):
    """Admin-only endpoint to re-index files in Tips & Resources immediately."""
    _require_admin(actor_role)
    res = lenovo_kb_service.sync_knowledge_base(force=force)
    audit_service.record_event(
        username=actor_username,
        user_role=actor_role,
        event_type="tips",
        details=f"Admin synced Lenovo KB ({res.get('entries_count', 0)} records)",
    )
    return res


@router.post("/upload")
async def upload_kb_file(
    file: UploadFile = File(...),
    actor_role: str = Query("User"),
    actor_username: str = Query("admin"),
):
    """Admin-only endpoint to upload an updated workbook or document into Tips & Resources."""
    _require_admin(actor_role)
    raw = await file.read()
    if len(raw) > settings.max_attachment_size_bytes:
        raise HTTPException(status_code=413, detail="Uploaded file exceeds max allowed size (25 MB).")
    try:
        res = lenovo_kb_service.save_uploaded_resource(file.filename or "", raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    audit_service.record_event(
        username=actor_username,
        user_role=actor_role,
        event_type="tips",
        details=f"Admin uploaded KB resource: {res['saved_file']}",
    )
    return res
