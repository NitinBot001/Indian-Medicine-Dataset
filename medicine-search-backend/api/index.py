"""
Vercel Serverless Function entry point.
Exposes the FastAPI `app` object to Vercel's Python runtime.
"""

import os
import sys
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.main import app

# Export app for Vercel
__all__ = ["app"]
