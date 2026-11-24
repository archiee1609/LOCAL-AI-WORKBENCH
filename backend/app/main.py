import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.logging import logger
from app.core.security import SecurityError, PathTraversalError, CommandPolicyError
from app.database.migrations import run_migrations
from app.api.router import api_router
from app.services.ollama_service import ollama_service

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {settings.app_name} v{settings.app_version} on {settings.host}:{settings.port}")
    await run_migrations()
    
    # Initialize Account Management & Workspaces (Rule 2 & 5)
    from app.services.account_service import account_service
    account_service.ensure_initial_workspaces()
    
    # Check Ollama and auto-start if needed
    if not await ollama_service.is_server_reachable():
        logger.info("Ollama is not running. Auto-starting local Ollama engine...")
        await ollama_service.start_service()

    health = await ollama_service.check_health()
    if health["reachable"]:
        models = await ollama_service.list_models()
        model_names = [m.name for m in models]
        logger.info(f"Ollama connected ({health['version']}). Models found: {model_names}")
    else:
        logger.warning(f"Ollama is currently unreachable at {settings.ollama_base_url}: {health.get('error')}")

    # Automatically start ComfyUI image generator service alongside Ollama
    try:
        from app.services.comfyui_service import comfyui_service
        if comfyui_service.comfy_main.exists():
            logger.info("Automatically launching ComfyUI service alongside Ollama...")
            asyncio.create_task(comfyui_service.start_service())
    except Exception as e:
        logger.warning(f"Failed to autostart ComfyUI during application lifespan: {e}")

    yield

    logger.info("Shutting down Local AI Workbench backend...")
    try:
        from app.services.comfyui_service import comfyui_service
        await comfyui_service.stop_service()
    except Exception:
        pass
    try:
        await ollama_service.stop_service()
    except Exception:
        pass

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Local AI Workbench Backend API",
    lifespan=lifespan
)

# CORS configuration strictly allowing loopback
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Custom exception handlers for security errors
@app.exception_handler(PathTraversalError)
async def path_traversal_exception_handler(request: Request, exc: PathTraversalError):
    logger.warning(f"Security Alert - Path traversal blocked: {exc}")
    return JSONResponse(status_code=403, content={"error": "Access Denied: Path escapes authorized workspace."})

@app.exception_handler(CommandPolicyError)
async def command_policy_exception_handler(request: Request, exc: CommandPolicyError):
    logger.warning(f"Security Alert - Command blocked by policy: {exc}")
    return JSONResponse(status_code=403, content={"error": str(exc)})

@app.exception_handler(SecurityError)
async def security_exception_handler(request: Request, exc: SecurityError):
    logger.warning(f"Security violation blocked: {exc}")
    return JSONResponse(status_code=403, content={"error": f"Security restriction: {str(exc)}"})

# Mount generated images folder for static serving
if settings.generated_images_dir.exists():
    app.mount("/static/generated", StaticFiles(directory=str(settings.generated_images_dir)), name="generated")

# Include unified API router
app.include_router(api_router)

# Mount frontend dist static assets if built
frontend_dist = settings.base_dir / "frontend" / "dist"
frontend_public = settings.base_dir / "frontend" / "public"

if (frontend_dist / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="assets")

# Mount PWA icons directory
icons_dir = frontend_dist / "icons" if (frontend_dist / "icons").exists() else frontend_public / "icons"
if icons_dir.exists():
    app.mount("/icons", StaticFiles(directory=str(icons_dir)), name="icons")

@app.get("/manifest.webmanifest")
@app.get("/manifest.json")
async def get_manifest():
    for candidate in [frontend_dist / "manifest.webmanifest", frontend_public / "manifest.webmanifest"]:
        if candidate.exists():
            from fastapi.responses import FileResponse
            return FileResponse(candidate, media_type="application/manifest+json")
    return JSONResponse(status_code=404, content={"error": "PWA Manifest not found"})

@app.get("/sw.js")
async def get_service_worker():
    for candidate in [frontend_dist / "sw.js", frontend_public / "sw.js"]:
        if candidate.exists():
            from fastapi.responses import FileResponse
            return FileResponse(
                candidate,
                media_type="application/javascript",
                headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"}
            )
    return JSONResponse(status_code=404, content={"error": "Service Worker not found"})

@app.get("/")
async def root():
    index_html = frontend_dist / "index.html"
    if index_html.exists():
        from fastapi.responses import FileResponse
        return FileResponse(index_html)
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "status": "online",
        "docs_url": "/docs",
        "api_url": "/api"
    }
