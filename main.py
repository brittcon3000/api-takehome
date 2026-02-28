from fastapi import FastAPI

app = FastAPI(title="Vendor Signal Normalization & Routing")


@app.get("/healthz")
async def healthz():
    return {"ok": True}