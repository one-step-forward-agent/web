from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.routes import auth_router, router
from app.core.config import settings
from app.core.database import Base, engine
from app.models import models  # noqa: F401


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.storage_path.mkdir(parents=True, exist_ok=True)
    yield
    await engine.dispose()


app = FastAPI(title="Focus Day API", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(auth_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=FileResponse)
async def index():
    return Path(__file__).parent / "templates" / "index.html"