"""
Public API Gateway Service (Vercel Service: "app")

Calls internal service "medicine-search-backend" using the Vercel service binding
injected into `MEDICINE_SEARCH_BACKEND_URL`.
"""

import os
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Bound service URL injected by Vercel at runtime
# (Defaults to http://localhost:8000 for local development)
BACKEND_URL = os.getenv("MEDICINE_SEARCH_BACKEND_URL", "http://localhost:8000").rstrip("/")

app = FastAPI(
    title="Medicine Search Gateway",
    description="Vercel Multi-Service Gateway proxying to internal medicine-search-backend service.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/gateway/health")
async def gateway_health():
    """Health check for the public gateway service and its backend binding."""
    backend_status = "unknown"
    try:
        async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=3.0) as client:
            r = await client.get("/health")
            backend_status = "connected" if r.status_code == 200 else f"status_{r.status_code}"
    except Exception as e:
        backend_status = f"unreachable ({e})"

    return {
        "service": "app",
        "status": "ok",
        "bound_service": "medicine-search-backend",
        "backend_url": BACKEND_URL,
        "backend_status": backend_status,
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_to_backend(request: Request, path: str):
    """
    Transparently forward all public API requests to the internal medicine-search-backend service.
    """
    query_params = str(request.url.query)
    url_path = f"/{path}" + (f"?{query_params}" if query_params else "")

    headers = dict(request.headers)
    headers.pop("host", None)

    try:
        req_body = await request.body()
        async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=30.0) as client:
            resp = await client.request(
                method=request.method,
                url=url_path,
                content=req_body,
                headers=headers,
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=dict(resp.headers),
                media_type=resp.headers.get("content-type"),
            )
    except httpx.RequestError as e:
        return JSONResponse(
            status_code=502,
            content={
                "error": "Bad Gateway",
                "message": f"Failed to reach internal medicine-search-backend service: {e}",
                "backend_url": BACKEND_URL,
            },
        )
