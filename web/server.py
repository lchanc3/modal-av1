r"""本機控制台。只綁 127.0.0.1。

啟動： start-web.bat      或   .venv\Scripts\python web\server.py

網頁只是遙控器：送件之後工作在雲端的 driver 手上跑，
關掉瀏覽器、關掉這個 server、關機都不影響。
"""

import datetime
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
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
    except FileNotFoundError:
        return []                       # 資料夾還不存在，正常
    except Exception as e:
        # 其他錯誤不要吞掉：曾經因為這裡的 except Exception 把整個清單變成空的，
        # 前端顯示「還沒有原片」，看起來像 Volume 空了，其實是連線出錯。
        print("listdir({!r}) 失敗：{!r}".format(d, e), flush=True)
        raise HTTPException(502, "讀取 Volume 失敗：{}".format(e))


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


# --------------------------------------------------------------------------
# 預算
# --------------------------------------------------------------------------

BUDGET_FILE = os.path.join(ROOT, "budget.json")
BILLING_CACHE_FILE = os.path.join(ROOT, ".billing-cache.json")


def _load_billing_cache() -> dict:
    """快取寫到磁碟：billing report 有速率限制，重啟 server 不該又去打一次。"""
    try:
        with open(BILLING_CACHE_FILE, encoding="utf-8") as f:
            c = json.load(f)
        return {"at": float(c["at"]), "data": c["data"]}
    except Exception:
        return {"at": 0.0, "data": None}


_billing_cache = _load_billing_cache()


def _allowance() -> float:
    """每月免費額度。Modal 的 API 只回報「已用掉多少」，沒有告訴你上限是多少，
    所以額度本身存在本機，預設 Starter 方案的 30 美元。"""
    try:
        with open(BUDGET_FILE, encoding="utf-8") as f:
            return float(json.load(f)["allowance"])
    except Exception:
        return 30.0


BILLING_TTL = 600     # billing report 有速率限制，不能頻繁打


def _with_allowance(data: dict) -> dict:
    """額度是本機設定，不吃快取。"""
    out = dict(data)
    out["allowance"] = _allowance()
    out["remaining"] = out["allowance"] - out["metered"]
    return out


def _fetch_billing() -> dict:
    w = modal.Workspace.from_context()
    s = w.billing.summary()

    # 報表只回傳「完整的區間」，所以用日解析度時，今天整天都會被排除 ——
    # 而花費多半就發生在今天。近 7 天改用小時解析度（那是 API 的上限），
    # 只剩當前這一小時不完整。
    now = datetime.datetime.now(datetime.timezone.utc)
    cut = max(s.start, (now - datetime.timedelta(days=6)).replace(
        minute=0, second=0, microsecond=0))
    rows = []
    if cut > s.start:
        rows += w.billing.report(start=s.start, end=cut, resolution="d")
    rows += w.billing.report(start=cut, resolution="h")

    daily, by_app = {}, {}
    for r in rows:
        # 依本地時區分桶：使用者想的是「今天花了多少」，不是 UTC 的今天
        day = r["interval_start"].astimezone().date().isoformat()
        cost = float(r["cost"])
        daily[day] = daily.get(day, 0.0) + cost
        by_app[r["description"]] = by_app.get(r["description"], 0.0) + cost

    return {
        "cycle_start": s.start.date().isoformat(),
        "cycle_end": s.end.date().isoformat(),
        "metered": float(s.metered_cost),
        "billed": float(s.billed_cost),
        "breakdown": {k: float(v) for k, v in s.metered_cost_breakdown.items() if float(v) > 0},
        "daily": [{"date": k, "cost": v} for k, v in sorted(daily.items())],
        "by_app": sorted(({"name": k, "cost": v} for k, v in by_app.items()),
                         key=lambda x: -x["cost"])[:6],
        "fetched_at": time.time(),
        "stale": False,
    }


@app.get("/api/billing")
def billing(refresh: bool = False):
    now = time.time()
    cached = _billing_cache["data"]
    if cached and not refresh and now - _billing_cache["at"] < BILLING_TTL:
        return _with_allowance(cached)

    try:
        data = _fetch_billing()
        _billing_cache.update(at=now, data=data)
        try:
            with open(BILLING_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"at": now, "data": data}, f)
        except OSError:
            pass
        return _with_allowance(data)
    except Exception as e:
        if cached:
            # 被限流或斷線時給舊數字並標記，總比整塊消失好
            stale = _with_allowance(cached)
            stale["stale"] = True
            stale["error"] = repr(e)[:200]
            return stale
        raise HTTPException(502, "讀取帳務失敗：{}".format(e))


@app.post("/api/billing/allowance")
async def set_allowance(request: Request):
    body = await request.json()
    v = float(body.get("allowance", 30))
    if not (0 < v <= 100000):
        raise HTTPException(400, "額度不合理")
    with open(BUDGET_FILE, "w", encoding="utf-8") as f:
        json.dump({"allowance": v}, f)
    return {"ok": True, "allowance": v}


@app.get("/api/work")
def work_stat():
    """work/ 底下的暫存切段。成功的 job 會自己清掉，失敗或取消的會留著供重送沿用。"""
    dirs = {}
    try:
        entries = vol().listdir("work", recursive=True)
    except FileNotFoundError:
        return {"dirs": [], "bytes": 0}
    for e in entries:
        parts = e.path.split("/")
        if len(parts) != 3:            # 只算 work/<key>/<file>，跳過目錄本身
            continue
        dirs[parts[1]] = dirs.get(parts[1], 0) + (getattr(e, "size", 0) or 0)
    return {"dirs": [{"name": k, "bytes": v} for k, v in sorted(dirs.items())],
            "bytes": sum(dirs.values())}


@app.post("/api/work/gc")
def work_gc():
    d = jd()
    snap = jobspec.snapshot(d)
    running = [j for j in (snap.get("jobs:index") or [])
               if jobspec.read_job(d, j, snap)["state"] not in jobspec.TERMINAL]
    if running:
        raise HTTPException(409, "還有 {} 個工作在進行中，清掉暫存會讓它們失敗".format(len(running)))

    removed = []
    for entry in work_stat()["dirs"]:
        vol().remove_file("work/" + entry["name"], recursive=True)
        removed.append(entry["name"])
    return {"ok": True, "removed": removed}


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
        if j["state"] in jobspec.TERMINAL:
            j.pop("chunks", None)    # 已結束的不需要每段明細
        _mark_stale(j)               # 快照已含每段資料，進行中的直接帶著，不多花 round-trip
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
