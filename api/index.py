"""
Vercel Serverless Function entry point at repository root.
Exposes the FastAPI `app` object to Vercel's Python runtime.
"""

import os
import sys
from pathlib import Path

# Add backend directory to sys.path
root_dir = Path(__file__).resolve().parent.parent
backend_dir = root_dir / "medicine-search-backend"

if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from app.main import app

# Export app for Vercel
__all__ = ["app"]
