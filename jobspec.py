"""送件的共用邏輯，submit.py 和 web/server.py 都用這支，避免兩邊行為分岔。

Dict 的 key 配置刻意做到「每個 key 只有一個 writer」，完全避開
read-modify-write 競態：

    jobs:index              本機（送件時 append）
    job:{id}:submit         本機（送件時寫一次，含 driver 的 call id）
    job:{id}                雲端 driver（狀態機）
    job:{id}:chunk:{i}      該段自己的容器（進度）
    job:{id}:cancelled      本機（取消時間戳）
"""

import os
import time
import uuid

import modal

APP = "av1-encode"
DICT = "av1-jobs"

BASE_SVT = "tune=0:enable-overlays=1:enable-qm=1:film-grain=0"

DEFAULTS = {
    "mode": "full",     # full｜ref｜sweep
    "preset": 3,
    "crf": 34,
    "gop": 600,
    "svt": BASE_SVT,
    "chunk_sec": 600,   # 每段 10 分鐘
    "cpu": 8,           # 每段 8 核：SVT-AV1 超過 8 核執行緒效率下降，小容器也更好排
    "test": False,
    "start": "00:05:00",
    "dur": 20,
    "tag": "",
    # 參數掃描
    "crfs": [28, 30, 32, 34, 36],
    "ref": "",
    "min_mean": 95.0,   # runbook 實測出來的標準
    "min_low": 89.0,
}

TERMINAL = ("done", "error", "cancelled")


def get_dict():
    return modal.Dict.from_name(DICT, create_if_missing=True)


def build(name: str, **over) -> dict:
    """把使用者給的參數補上預設值，並算出輸出檔名。"""
    p = dict(DEFAULTS)
    p.update({k: v for k, v in over.items() if v is not None})

    p["preset"] = int(p["preset"])
    p["crf"] = int(p["crf"])
    p["gop"] = int(p["gop"])
    p["chunk_sec"] = int(p["chunk_sec"])
    p["cpu"] = int(p["cpu"])
    p["dur"] = int(p["dur"])
    p["test"] = bool(p["test"])
    p["mem"] = p["cpu"] * 1024

    stem = os.path.splitext(name)[0]

    if p["mode"] == "ref":
        p["out_name"] = "ref_{}_{}s.mkv".format(
            os.path.basename(stem).replace(" ", "_"), p["dur"])
        return p

    if p["mode"] == "sweep":
        p["crfs"] = sorted({int(c) for c in p["crfs"]})
        p["min_mean"] = float(p["min_mean"])
        p["min_low"] = float(p["min_low"])
        if not p["ref"]:
            raise ValueError("掃描需要指定 ref")
        p["out_name"] = "掃描 crf {}".format("/".join(str(c) for c in p["crfs"]))
        return p

    if p["test"]:
        tag = p["tag"] or "p{}_crf{}".format(p["preset"], p["crf"])
        p["out_name"] = "{}_test_{}.mkv".format(stem, tag)
    else:
        # 全片也能給 tag，方便同一支片用不同參數各跑一次做比較而不互相覆蓋
        p["out_name"] = ("{}_AV1_{}.mkv".format(stem, p["tag"]) if p["tag"]
                         else "{}_AV1.mkv".format(stem))
    return p


def submit(name: str, p: dict) -> dict:
    """spawn driver 並登記到 Dict。送出後本機不需要保持連線。"""
    d = get_dict()
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]

    call = modal.Function.from_name(APP, "run_job").spawn(job_id, name, p)

    d.put("job:{}:submit".format(job_id), {
        "job_id": job_id,
        "name": name,
        "params": p,
        "out_name": p["out_name"],
        "created": time.time(),
        "driver_call_id": call.object_id,
    })
    d.put("job:{}".format(job_id), {"state": "queued", "updated": time.time()})

    index = d.get("jobs:index") or []
    index.append(job_id)
    d.put("jobs:index", index[-200:])

    return {"job_id": job_id, "call_id": call.object_id, "out_name": p["out_name"]}


def driver_alive(call_id: str):
    """driver 是否還活著。

    不用 heartbeat —— driver 阻塞在 .map() 時沒辦法更新任何東西。
    FunctionCall 的狀態才是權威來源。
    回傳 True=執行中、False=已結束、None=查不到。
    """
    try:
        modal.FunctionCall.from_id(call_id).get(timeout=0)
        return False
    except TimeoutError:
        return True
    except Exception:
        return False


def snapshot(d) -> dict:
    """一次把整個 Dict 讀下來。

    單次 get 約 0.47s，而整個 Dict 一次抓完只要約 2s。逐 key 讀的話，
    9 個 job 要 40 幾次 round-trip、超過 13 秒，網頁根本輪詢不動。
    """
    return {k: v for k, v in d.items()}


def read_job(d, job_id: str, snap=None) -> dict:
    """把一個 job 的所有 key 併成前端要的形狀。

    給了 snap 就完全不碰網路。
    """
    get = snap.get if snap is not None else d.get

    sub = get("job:{}:submit".format(job_id)) or {}
    meta = get("job:{}".format(job_id)) or {}
    cancelled = get("job:{}:cancelled".format(job_id))

    state = meta.get("state", "queued")
    if cancelled and state not in TERMINAL:
        state = "cancelled"

    total = meta.get("total_chunks") or 0
    chunks = []
    for i in range(total):
        c = get("job:{}:chunk:{}".format(job_id, i))
        chunks.append(c or {"idx": i, "state": "waiting", "pct": 0.0})

    # 整體進度用各段已完成的秒數加權，比單純數「完成幾段」平滑得多
    done_secs = sum(c.get("out_time", 0.0) for c in chunks)
    all_secs = sum(c.get("total", 0.0) for c in chunks)
    if state == "done":
        pct = 100.0
    elif all_secs and total and all(c.get("total") for c in chunks):
        pct = min(100.0, done_secs / all_secs * 100)
    elif total:
        pct = sum(c.get("pct", 0.0) for c in chunks) / total
    else:
        pct = 0.0

    return {
        "job_id": job_id,
        "name": sub.get("name", "?"),
        "out_name": sub.get("out_name", ""),
        "params": sub.get("params", {}),
        "created": sub.get("created", 0),
        "call_id": sub.get("driver_call_id", ""),
        "state": state,
        "kind": meta.get("kind") or (sub.get("params", {}).get("mode") or "full"),
        "pct": round(pct, 1),
        "total_chunks": total,
        "chunks": chunks,
        "table": meta.get("table") or [],
        "pick": meta.get("pick"),
        "ref": meta.get("ref") or sub.get("params", {}).get("ref", ""),
        "min_mean": meta.get("min_mean"),
        "min_low": meta.get("min_low"),
        "out": meta.get("out", ""),
        "size": meta.get("size", 0),
        "src_duration": meta.get("src_duration", 0),
        "out_duration": meta.get("out_duration", 0),
        "warning": meta.get("warning", ""),
        "error": meta.get("error", ""),
        "updated": meta.get("updated", 0),
    }


def cancel(d, job_id: str) -> None:
    sub = d.get("job:{}:submit".format(job_id)) or {}
    call_id = sub.get("driver_call_id")
    if call_id:
        try:
            modal.FunctionCall.from_id(call_id).cancel(terminate_containers=True)
        except Exception:
            pass
    d.put("job:{}:cancelled".format(job_id), time.time())


def forget(d, job_id: str) -> None:
    """把一個 job 的所有 key 從 Dict 移除（不動 Volume 上的檔案）。"""
    meta = d.get("job:{}".format(job_id)) or {}
    for i in range(meta.get("total_chunks") or 0):
        try:
            d.pop("job:{}:chunk:{}".format(job_id, i))
        except Exception:
            pass
    for k in ("job:{}", "job:{}:submit", "job:{}:cancelled"):
        try:
            d.pop(k.format(job_id))
        except Exception:
            pass
    index = [j for j in (d.get("jobs:index") or []) if j != job_id]
    d.put("jobs:index", index)
