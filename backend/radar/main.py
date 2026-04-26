from fastapi import FastAPI
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

app = FastAPI(title="AI Policy Radar", version="0.1.0")


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}
