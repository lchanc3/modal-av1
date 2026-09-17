r"""本機控制台。只綁 127.0.0.1。

啟動： start-web.bat      或   .venv\Scripts\python web\server.py

網頁只是遙控器：送件之後工作在雲端的 driver 手上跑，
關掉瀏覽器、關掉這個 server、關機都不影響。
"""

import os
import subprocess
import sys
import tempfile
import threading
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import modal          # noqa: E402
import jobspec        # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = int(os.environ.get("AV1_PORT", "8765"))

ALLOWED_ORIGINS = {"http://127.0.0.1:{}".format(PORT), "http://localhost:{}".format(PORT)}

app = FastAPI(title="modal-av1")

_vol = None
_dict = None
_lock = threading.Lock()

UPLOADS = {}          # uid -> {phase, sent, total, name, error}


def vol():
    global _vol
    if _vol is None:
        _vol = modal.Volume.from_name("videos", create_if_missing=True)
    return _vol


def jd():
    global _dict
    if _dict is None:
        _dict = jobspec.get_dict()
    return _dict


# --------------------------------------------------------------------------
# 只綁 127.0.0.1 擋不住惡意網站對 localhost 發請求，所以改狀態的請求檢查 Origin。
# 沒有 Origin 的（curl 之類的非瀏覽器用戶端）放行。
# --------------------------------------------------------------------------

@app.middleware("http")
async def guard_origin(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return JSONResponse({"detail": "跨來源請求已擋下"}, status_code=403)
    return await call_next(request)


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "index.html"))


# --------------------------------------------------------------------------
# Volume 檔案
# --------------------------------------------------------------------------

def _listdir(d):
    try:
        return [{"path": e.path,
                 "name": e.path.rsplit("/", 1)[-1],
                 "size": getattr(e, "size", 0) or 0,
                 "mtime": getattr(e, "mtime", 0) or 0}
                for e in vol().listdir(d)]
    except Exception:
        return []


@app.get("/api/files")
def files():
    return {"in": sorted(_listdir("in"), key=lambda f: f["name"]),
            "out": sorted(_listdir("out"), key=lambda f: f["name"])}


@app.delete("/api/files")
def remove_file(path: str):
    if not path.startswith(("in/", "out/")):
        raise HTTPException(400, "只能刪 in/ 或 out/ 底下的檔案")
    vol().remove_file(path)
    return {"ok": True}


@app.get("/api/download")
def download(path: str):
    if not path.startswith(("in/", "out/")):
        raise HTTPException(400, "路徑不合法")
    name = path.rsplit("/", 1)[-1]
    return StreamingResponse(
        vol().read_file(path),
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="{}"'.format(name)})


# --------------------------------------------------------------------------
# 上傳：瀏覽器 PUT 原始 body → 本機暫存檔 → Modal Volume
# --------------------------------------------------------------------------

class _Counting:
    """包住檔案物件，一邊被讀一邊回報進度。"""

    def __init__(self, fp, uid):
        self._fp = fp
        self._uid = uid

    def read(self, *a, **k):
        b = self._fp.read(*a, **k)
        u = UPLOADS.get(self._uid)
        if u:
            u["sent"] = min(u["total"], u["sent"] + len(b))
        return b

    def __getattr__(self, n):
        return getattr(self._fp, n)


def _to_modal(tmp_path, remote, uid, size):
    UPLOADS[uid].update(phase="modal", sent=0, total=size)
    try:
        with open(tmp_path, "rb") as fp:
            with vol().batch_upload(force=True) as batch:
                batch.put_file(_Counting(fp, uid), remote)
        UPLOADS[uid].update(phase="done", sent=size)
    except Exception as e:
        UPLOADS[uid].update(phase="error", error=repr(e))
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


@app.put("/api/upload")
async def upload(request: Request, name: str, uid: str = ""):
    if "/" in name or "\\" in name:
        raise HTTPException(400, "檔名不合法")
    uid = uid or uuid.uuid4().hex[:8]
    total = int(request.headers.get("content-length") or 0)
    UPLOADS[uid] = {"phase": "local", "sent": 0, "total": total, "name": name, "error": ""}

    fd, tmp_path = tempfile.mkstemp(prefix="av1up_")
    written = 0
    with os.fdopen(fd, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
            written += len(chunk)
            UPLOADS[uid]["sent"] = written

    await run_in_threadpool(_to_modal, tmp_path, "in/" + name, uid, written or total)
    u = UPLOADS.get(uid, {})
    if u.get("phase") == "error":
        raise HTTPException(500, u.get("error", "上傳失敗"))
    return {"ok": True, "uid": uid, "path": "in/" + name}


@app.get("/api/upload/{uid}")
def upload_status(uid: str):
    return UPLOADS.get(uid) or {"phase": "unknown"}


# --------------------------------------------------------------------------
# 工作
# --------------------------------------------------------------------------

@app.post("/api/jobs")
async def create_job(request: Request):
    body = await request.json()
    name = body.pop("name", None)
    if not name:
        raise HTTPException(400, "沒有指定來源檔")
    params = jobspec.build(name, **body)
    return await run_in_threadpool(jobspec.submit, name, params)


@app.get("/api/jobs")
def list_jobs():
    d = jd()
    snap = jobspec.snapshot(d)          # 一次 round-trip，而不是每個 job 好幾次
    out = []
    for jid in reversed(snap.get("jobs:index") or []):
        j = jobspec.read_job(d, jid, snap)
        j.pop("chunks", None)        # 清單不需要每段明細，展開時才抓
        _mark_stale(j)
        out.append(j)
    return out


def _mark_stale(j: dict) -> None:
    """driver 不在了但狀態沒收尾＝中途死掉。只查執行中的，通常 0~1 個。"""
    if j["state"] in jobspec.TERMINAL or not j["call_id"]:
        return
    alive = jobspec.driver_alive(j["call_id"])
    j["driver_alive"] = alive
    if alive is False and j["state"] != "queued":
        j["stale"] = True


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    d = jd()
    j = jobspec.read_job(d, job_id, jobspec.snapshot(d))
    _mark_stale(j)
    return j


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    jobspec.cancel(jd(), job_id)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def forget_job(job_id: str):
    jobspec.forget(jd(), job_id)
    return {"ok": True}


@app.post("/api/stop-app")
def stop_app():
    """逃生門：直接停掉整個 App，確保沒有任何容器在燒錢。"""
    exe = os.path.join(ROOT, ".venv", "Scripts", "modal.exe")
    if not os.path.exists(exe):
        exe = "modal"
    r = subprocess.run([exe, "app", "stop", jobspec.APP],
                       capture_output=True, text=True)
    return {"ok": r.returncode == 0, "out": (r.stdout + r.stderr)[-2000:]}


if __name__ == "__main__":
    import uvicorn
    print("開啟 http://127.0.0.1:{}".format(PORT))
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
