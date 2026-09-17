"""Modal 端 AV1 轉檔：切段 → 平行編碼 → 合併。

部署： modal deploy encode.py
送件： python submit.py <檔名>  或啟動 web/server.py

刻意不提供 local_entrypoint —— `modal run` 開的是 ephemeral app，
本機一斷線容器就跟著死，全片轉到一半白做。一律走 submit.py / 網頁。

工作目錄用「內容」決定名字而不是 job_id：切段目錄由（來源檔名 + 每段秒數）
決定，編碼檔名再多吃（preset、crf、gop、svt）。因此取消後重送會沿用已經
編好的段落，而改了任何編碼參數就自動不會沿用 —— 不必信任誰記得清快取。
"""

import hashlib
import json
import os
import shutil
import subprocess
import time

import modal

app = modal.App("av1-encode")
vol = modal.Volume.from_name("videos", create_if_missing=True)
jobs = modal.Dict.from_name("av1-jobs", create_if_missing=True)

image = (modal.Image.debian_slim()
    .apt_install("wget", "xz-utils")
    .run_commands(
        "cd /opt && wget -q https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
        " && tar xf ffmpeg-master-latest-linux64-gpl.tar.xz"
        " && ln -s /opt/ffmpeg-master-latest-linux64-gpl/bin/ffmpeg /usr/local/bin/ffmpeg"
        " && ln -s /opt/ffmpeg-master-latest-linux64-gpl/bin/ffprobe /usr/local/bin/ffprobe"))

driver_image = modal.Image.debian_slim()

BASE_SVT = "tune=0:enable-overlays=1:enable-qm=1:film-grain=0"

HOUR = 3600

# Modal 的基本費率（可搶佔）。nonpreemptible 是 3 倍。
RATE_CPU_S = 0.0000131      # 美元 / 實體核心 / 秒
RATE_MEM_S = 0.00000222     # 美元 / GiB / 秒


def _usage(t0: float, cpu: float, mem_mib: int, nonpreemptible: bool = False) -> dict:
    """一個階段實際用掉的資源與費用。

    注意這只涵蓋「成功跑完」的容器 —— 被搶佔而中途死掉的那些不會回報，
    所以這個數字是下限，跟帳單的差額就是重跑浪費掉的部分。
    """
    el = time.time() - t0
    rate = cpu * RATE_CPU_S + (mem_mib / 1024.0) * RATE_MEM_S
    return {"elapsed": round(el, 1), "cpu": cpu, "mem": mem_mib,
            "cost": round(rate * el * (3 if nonpreemptible else 1), 5)}


# --------------------------------------------------------------------------
# 內容定址
# --------------------------------------------------------------------------

def _key(*parts) -> str:
    return hashlib.sha1("|".join(str(x) for x in parts).encode()).hexdigest()[:12]


def work_dir(name: str, chunk_sec: int) -> str:
    """切段結果只跟來源與段長有關。"""
    return "/data/work/{}".format(_key(name, chunk_sec))


def enc_key(p: dict) -> str:
    """編碼結果還要看編碼參數 —— 改了 crf 就不該沿用舊的段落。"""
    return _key(p["preset"], p["crf"], p["gop"], p["svt"])


# --------------------------------------------------------------------------
# 容器內的小工具
# --------------------------------------------------------------------------

def _duration(path: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def _run(cmd: list) -> None:
    """跑 ffmpeg，失敗時把 stderr 的尾巴帶進例外訊息。"""
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("{} 失敗（{}）：\n{}".format(cmd[0], r.returncode, r.stderr[-4000:]))


def _publish(src_tmp: str, dst: str) -> None:
    """先寫 .part 再 rename：容器中途死掉不會在 dst 留下半個檔，
    否則下一輪會把半成品當成「已完成」沿用。"""
    part = dst + ".part"
    shutil.copy(src_tmp, part)
    os.replace(part, dst)


VMAF_FILTER = "settb=AVTB,setpts=N/FRAME_RATE/TB,format=yuv420p10le"


def _vmaf(dist: str, ref: str, threads: int = 8) -> dict:
    """對整支檔案打分，兩邊都不做 seek。

    容器裡的 ffmpeg 本來就含 libvmaf（BtbN build 的 scripts.d 有 45-vmaf.sh），
    所以不必把檔案抓回本機。用 setpts=N/FRAME_RATE/TB 以幀序號對齊 ——
    各自 seek 去比對是「VMAF 只有 30 幾分」的典型原因。
    """
    log = "/tmp/vmaf.json"
    _run(["ffmpeg", "-hide_banner", "-y", "-i", dist, "-i", ref, "-an", "-lavfi",
          "[0:v]{f}[d];[1:v]{f}[r];[d][r]libvmaf=n_threads={t}:log_fmt=json:log_path={l}".format(
              f=VMAF_FILTER, t=threads, l=log),
          "-f", "null", "-"])
    with open(log, encoding="utf-8") as f:
        v = json.load(f)["pooled_metrics"]["vmaf"]
    return {"vmaf_mean": v["mean"], "vmaf_harmonic": v["harmonic_mean"], "vmaf_min": v["min"]}


def _put_chunk(job_id: str, idx: int, **kw) -> None:
    """每段只有自己會寫這個 key，不會有 read-modify-write 競態。"""
    jobs.put("job:{}:chunk:{}".format(job_id, idx),
             dict(idx=idx, updated=time.time(), **kw))


def _encode_cmd(src: str, dst: str, p: dict, seek=None) -> list:
    cmd = ["ffmpeg", "-hide_banner", "-y"]
    if seek:                      # -ss 放在 -i 前面：快速且精確的 seek
        cmd += ["-ss", seek[0], "-t", seek[1]]
    # 一律只取視訊。全片模式的音軌留到 merge 才從原片 mux 回去；
    # 測試片段則是因為 -c:a copy 沒辦法在音訊封包中間切斷，會把容器長度撐長
    # （實測要 20 秒卻得到 21.06 秒），對不齊 ref 就沒辦法打 VMAF。
    cmd += ["-i", src, "-map", "0:v:0", "-an", "-sn",
            "-c:v", "libsvtav1",
            "-preset", str(p["preset"]), "-crf", str(p["crf"]), "-g", str(p["gop"]),
            "-pix_fmt", "yuv420p10le", "-svtav1-params", p["svt"],
            "-progress", "pipe:1", "-nostats", dst]
    return cmd


def _encode_with_progress(cmd: list, total: float, report) -> None:
    """跑 ffmpeg 並解析 -progress 的輸出，每 5 秒回報一次。

    stderr 導到檔案而不是 PIPE：不讀的 PIPE 填滿後會 deadlock，
    導到 STDOUT 又會污染 -progress 的資料流。
    """
    err = open("/tmp/ffmpeg_err.log", "w+")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
    last = 0.0
    fps = 0.0
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("fps="):
            try:
                fps = float(line[4:])
            except ValueError:
                pass
        elif line.startswith("out_time_us="):
            now = time.time()
            if now - last < 5:
                continue
            last = now
            try:
                secs = int(line[12:]) / 1_000_000
            except ValueError:
                continue
            report(min(100.0, secs / total * 100) if total else 0.0, secs, fps)
    proc.wait()
    if proc.returncode != 0:
        err.seek(0)
        raise RuntimeError("ffmpeg 失敗（{}）：\n{}".format(proc.returncode, err.read()[-4000:]))


# --------------------------------------------------------------------------
# 三個階段
# --------------------------------------------------------------------------

@app.function(image=image, volumes={"/data": vol}, cpu=4, memory=4096,
              timeout=2 * HOUR, retries=2)
def split(name: str, chunk_sec: int) -> dict:
    """依來源的關鍵影格切段（-c copy，不重新編碼，零損失）。

    只取視訊：段落較小，而且音軌完全不碰、留到 merge 才從原片 mux 回去，
    避免逐段複製音訊累積出同步漂移。
    切好的結果附一份 manifest，下次同樣的來源與段長就直接沿用。
    """
    t0 = time.time()
    work = work_dir(name, chunk_sec)
    src = "/data/in/{}".format(name)
    vol.reload()

    src_size = os.path.getsize(src)
    man_path = "{}/split.json".format(work)
    if os.path.exists(man_path):
        try:
            man = json.load(open(man_path))
            if (man.get("src_size") == src_size
                    and all(os.path.exists("{}/{}".format(work, f)) for f in man["parts"])):
                print("沿用既有切段：{} 段".format(len(man["parts"])))
                return {"work": work, "parts": man["parts"], "reused": True,
                        "usage": _usage(t0, 4, 4096)}
        except Exception as e:
            print("manifest 壞了，重新切段：", repr(e))

    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    _run(["ffmpeg", "-hide_banner", "-y", "-i", src,
          "-map", "0:v:0", "-c", "copy", "-f", "segment",
          "-segment_time", str(chunk_sec), "-reset_timestamps", "1",
          "{}/src_%04d.mkv".format(work)])

    parts = sorted(f for f in os.listdir(work) if f.startswith("src_"))
    json.dump({"name": name, "chunk_sec": chunk_sec, "src_size": src_size, "parts": parts},
              open(man_path, "w"))
    vol.commit()

    print("切成 {} 段".format(len(parts)))
    return {"work": work, "parts": parts, "reused": False, "usage": _usage(t0, 4, 4096)}


@app.function(image=image, volumes={"/data": vol}, cpu=8, memory=8192,
              timeout=12 * HOUR, retries=2)
def encode_chunk(arg: dict) -> str:
    """編碼一段。被搶佔時只有這一段要重來。

    已經編好且長度正確的段落直接沿用 —— 這是取消後重送不必從頭再來的關鍵。
    """
    t0 = time.time()
    job_id, idx, src, p = arg["job_id"], arg["idx"], arg["src"], arg["params"]
    work, key = arg["work"], arg["enc_key"]
    cpu, mem = p.get("cpu") or 8, p.get("mem") or 8192
    vol.reload()

    # 被搶佔重跑會再進來一次。同一段不會同時有兩個容器，所以這裡讀了再寫是安全的。
    prev = jobs.get("job:{}:chunk:{}".format(job_id, idx)) or {}
    attempt = (prev.get("attempt") or 0) + 1
    if attempt > 1:
        print("第 {} 段第 {} 次嘗試（前一次被中斷）".format(idx, attempt))

    total = _duration("{}/{}".format(work, src))
    out_name = "enc_{}_{}".format(key, src[len("src_"):])
    dst = "{}/{}".format(work, out_name)

    if os.path.exists(dst):
        try:
            if abs(_duration(dst) - total) < 0.05:
                print("沿用第 {} 段".format(idx))
                u = _usage(t0, cpu, mem)
                _put_chunk(job_id, idx, state="done", pct=100.0, out_time=total,
                           fps=0.0, total=total, size=os.path.getsize(dst), reused=True,
                           attempt=attempt, **u)
                return {"name": out_name, "usage": u, "attempt": attempt, "reused": True}
            print("第 {} 段長度不符，重編".format(idx))
        except Exception as e:
            print("第 {} 段檢查失敗，重編：{!r}".format(idx, e))

    tmp = "/tmp/{}".format(out_name)
    # 重跑時 pct 歸零，網頁上就看得出這段被搶佔過
    _put_chunk(job_id, idx, state="encoding", pct=0.0, out_time=0.0, fps=0.0,
               total=total, attempt=attempt)

    _encode_with_progress(
        _encode_cmd("{}/{}".format(work, src), tmp, p), total,
        lambda pct, secs, fps: _put_chunk(job_id, idx, state="encoding", pct=pct,
                                          out_time=secs, fps=fps, total=total,
                                          attempt=attempt))

    _publish(tmp, dst)
    vol.commit()
    u = _usage(t0, cpu, mem)
    _put_chunk(job_id, idx, state="done", pct=100.0, out_time=total, fps=0.0,
               total=total, size=os.path.getsize(tmp), reused=False, attempt=attempt, **u)
    return {"name": out_name, "usage": u, "attempt": attempt, "reused": False}


@app.function(image=image, volumes={"/data": vol}, cpu=4, memory=4096,
              timeout=2 * HOUR, retries=2)
def merge(name: str, work: str, enc_files: list, out_name: str) -> dict:
    """concat 各段，再把原片的音軌/字幕 mux 回去。"""
    t0 = time.time()
    vol.reload()

    with open("/tmp/list.txt", "w") as f:
        for e in enc_files:
            f.write("file '{}/{}'\n".format(work, e))

    _run(["ffmpeg", "-hide_banner", "-y", "-f", "concat", "-safe", "0",
          "-i", "/tmp/list.txt", "-c", "copy", "/tmp/video.mkv"])

    # 1:a? / 1:s? 的問號：來源沒有該類串流時不報錯
    _run(["ffmpeg", "-hide_banner", "-y", "-i", "/tmp/video.mkv",
          "-i", "/data/in/{}".format(name),
          "-map", "0:v:0", "-map", "1:a?", "-map", "1:s?", "-c", "copy", "/tmp/final.mkv"])

    src_d = _duration("/data/in/{}".format(name))
    out_d = _duration("/tmp/final.mkv")
    parts_d = sum(_duration("{}/{}".format(work, e)) for e in enc_files)

    # 真正該成立的不變量：輸出 == 各段總和。不成立才是管線出錯（漏段、順序錯亂）。
    warning = ""
    if abs(parts_d - out_d) > 0.05:
        warning = "合併異常：{} 段總長 {:.3f}s、輸出 {:.3f}s".format(
            len(enc_files), parts_d, out_d)
        print("警告：", warning)
    elif abs(src_d - out_d) > 0.05:
        # 這不是管線的錯：來源帶了 edit list（起始裁切）時，-c copy 切段會把
        # 被藏起來的 pre-roll 幀一起帶出來。照實說明，不要誤報成合併失敗。
        warning = ("來源 {:.3f}s、輸出 {:.3f}s（差 {:+.3f}s）。各段總和與輸出一致，"
                   "代表合併沒問題；差異來自來源的 edit list 起始裁切，"
                   "輸出包含了原本被隱藏的前置幀。").format(src_d, out_d, out_d - src_d)
        print("注意：", warning)

    os.makedirs("/data/out", exist_ok=True)
    _publish("/tmp/final.mkv", "/data/out/{}".format(out_name))
    vol.commit()

    size = os.path.getsize("/tmp/final.mkv")
    shutil.rmtree(work, ignore_errors=True)   # 成功後才清掉，失敗重跑時才沿用得到
    vol.commit()

    print("完成：out/{} {:.1f} MiB".format(out_name, size / 2 ** 20))
    return {"out": "out/{}".format(out_name), "size": size,
            "src_duration": src_d, "out_duration": out_d, "parts_duration": parts_d,
            "chunks_merged": len(enc_files), "warning": warning,
            "usage": _usage(t0, 4, 4096)}


@app.function(image=image, volumes={"/data": vol}, cpu=8, memory=8192,
              timeout=6 * HOUR, retries=2)
def encode_clip(job_id: str, name: str, p: dict, out_name: str) -> dict:
    """測試片段：單一容器，真的只轉 start 起算的 dur 秒。

    舊版的 --test 只改輸出檔名、完全沒切片段，結果每次「測試」都在轉全片。
    """
    t0 = time.time()
    tmp = "/tmp/{}".format(out_name)
    total = float(p["dur"])
    _put_chunk(job_id, 0, state="encoding", pct=0.0, out_time=0.0, fps=0.0, total=total)

    _encode_with_progress(
        _encode_cmd("/data/in/{}".format(name), tmp, p, seek=(p["start"], str(p["dur"]))),
        total,
        lambda pct, secs, fps: _put_chunk(job_id, 0, state="encoding",
                                          pct=pct, out_time=secs, fps=fps, total=total))

    os.makedirs("/data/out", exist_ok=True)
    _publish(tmp, "/data/out/{}".format(out_name))
    vol.commit()

    size = os.path.getsize(tmp)
    out_d = _duration(tmp)
    _put_chunk(job_id, 0, state="done", pct=100.0, out_time=total, fps=0.0,
               total=total, size=size, reused=False)
    print("完成：out/{} {:.1f} MiB {:.2f}s".format(out_name, size / 2 ** 20, out_d))
    return {"out": "out/{}".format(out_name), "size": size,
            "src_duration": total, "out_duration": out_d, "warning": "",
            "usage": _usage(t0, p.get("cpu") or 8, p.get("mem") or 8192)}


# --------------------------------------------------------------------------
# 參數掃描
# --------------------------------------------------------------------------

@app.function(image=image, volumes={"/data": vol}, cpu=4, memory=4096,
              timeout=HOUR, retries=2)
def make_ref(name: str, start: str, dur: int) -> dict:
    """從 Volume 上的原片切一段無損 ref，存回 in/。

    有了它就不必在本機切好再上傳。無損（-qp 0）所以體積大，但只有 20 秒，
    而且打分時它是基準，不能有任何壓縮損失。
    """
    t0 = time.time()
    stem = os.path.splitext(os.path.basename(name))[0].replace(" ", "_")
    out = "ref_{}_{}s.mkv".format(stem, dur)
    tmp = "/tmp/" + out
    dst = "/data/in/{}".format(out)
    vol.reload()

    # 檔名由來源與長度決定，所以同樣的組合可以直接沿用，不必重切
    if os.path.exists(dst):
        try:
            d = _duration(dst)
            if abs(d - dur) < 1.0:
                print("沿用既有 ref：in/{}".format(out))
                return {"ref": "in/{}".format(out), "ref_name": out,
                        "size": os.path.getsize(dst), "out_duration": d, "reused": True,
                        "usage": _usage(t0, 4, 4096)}
        except Exception as e:
            print("既有 ref 檢查失敗，重切：", repr(e))

    _run(["ffmpeg", "-hide_banner", "-y", "-ss", start, "-t", str(dur),
          "-i", "/data/in/{}".format(name),
          "-map", "0:v:0", "-an", "-sn",
          "-c:v", "libx264", "-qp", "0", "-preset", "fast", "-pix_fmt", "yuv420p", tmp])

    _publish(tmp, dst)
    vol.commit()
    size = os.path.getsize(tmp)
    print("ref 完成：in/{} {:.1f} MiB".format(out, size / 2 ** 20))
    return {"ref": "in/{}".format(out), "ref_name": out,
            "size": size, "out_duration": _duration(tmp), "reused": False,
            "usage": _usage(t0, 4, 4096)}


@app.function(image=image, volumes={"/data": vol}, cpu=8, memory=8192,
              timeout=4 * HOUR, retries=2)
def sweep_one(arg: dict) -> dict:
    """用一個 crf 編整支 ref，然後當場打分。編碼與打分在同一個容器裡完成。"""
    t0 = time.time()
    job_id, idx, crf, p, ref = arg["job_id"], arg["idx"], arg["crf"], arg["params"], arg["ref"]
    cpu, mem = p.get("cpu") or 8, p.get("mem") or 8192
    vol.reload()

    src = "/data/{}".format(ref)
    total = _duration(src)
    tmp = "/tmp/sweep_crf{}.mkv".format(crf)
    q = dict(p, crf=crf)

    def mark(**kw):
        _put_chunk(job_id, idx, crf=crf, total=total, **kw)

    # 編碼佔進度的前 70%，打分佔後 30%（打分大約要編碼的一半時間）
    mark(state="encoding", pct=0.0, out_time=0.0, fps=0.0)
    _encode_with_progress(
        _encode_cmd(src, tmp, q), total,
        lambda pct, secs, fps: mark(state="encoding", pct=pct * 0.7, out_time=secs, fps=fps))

    mark(state="scoring", pct=70.0, out_time=total, fps=0.0)
    v = _vmaf(tmp, src)
    size = os.path.getsize(tmp)

    u = _usage(t0, cpu, mem)
    mark(state="done", pct=100.0, out_time=total, fps=0.0, size=size, **v, **u)
    print("crf {} → {:.1f} MiB, VMAF 平均 {:.2f} / 最低 {:.2f}".format(
        crf, size / 2 ** 20, v["vmaf_mean"], v["vmaf_min"]))
    return dict(crf=crf, size=size, usage=u, **v)


# --------------------------------------------------------------------------
# 協調者
# --------------------------------------------------------------------------

@app.function(image=driver_image, cpu=0.25, memory=512,
              timeout=24 * HOUR, nonpreemptible=True)
def run_job(job_id: str, name: str, p: dict) -> dict:
    """整趟的協調者。

    刻意設 nonpreemptible：它只要 0.25 核、整趟成本約 $0.03，但一旦它被搶佔，
    整個 job 的進度就斷了。真正花錢的編碼工作留在便宜的可搶佔池裡。
    """
    key = "job:{}".format(job_id)
    state = {}
    t_job = time.time()
    stages = []          # 每個成功跑完的容器回報的用量

    def account(label, res):
        """把一個階段的用量收進來，並回傳原本的結果。"""
        u = (res or {}).get("usage")
        if u:
            stages.append(dict(u, stage=label))
        return res

    def totals():
        """實際用量。只含成功跑完的容器 —— 被搶佔中途死掉的不會回報，
        所以這是下限，跟帳單的差額就是重跑浪費掉的。"""
        drv = _usage(t_job, 0.25, 512, nonpreemptible=True)
        all_stages = stages + [dict(drv, stage="driver")]
        return {
            "cost": round(sum(x["cost"] for x in all_stages), 4),
            "core_seconds": round(sum(x["cpu"] * x["elapsed"] for x in all_stages)),
            "wall": round(time.time() - t_job),
            "stages": [{"stage": x["stage"], "cpu": x["cpu"],
                        "elapsed": x["elapsed"], "cost": x["cost"]} for x in all_stages],
        }

    def meta(**kw):
        """driver 是這個 key 的唯一 writer，所以狀態留在本地、每次整份寫出去。

        原本是 get → update → put，結果讀到舊值時會把前一次寫的欄位蓋掉
        （實測掉過 kind 和 total_chunks）。不回頭讀就沒有這個問題。
        """
        state.update(kw)
        state["updated"] = time.time()
        jobs.put(key, dict(state))

    try:
        cpu = p.get("cpu") or 8
        mem = p.get("mem") or int(cpu) * 1024
        mode = p.get("mode") or "full"

        if mode == "ref":
            meta(state="encoding", kind="ref", total_chunks=0)
            res = account("ref", make_ref.remote(name, p["start"], p["dur"]))
            meta(state="done", usage=totals(), **res)
            return res

        if mode == "sweep":
            crfs = sorted(set(int(c) for c in p["crfs"]))

            # 沒給 ref 就從來源切一支（同樣的來源與長度會沿用既有的）。
            # 掃描與產生 ref 合成一個 job，按一次就能走完，不必回來按第二次。
            ref = p.get("ref")
            if not ref:
                meta(state="splitting", kind="sweep", total_chunks=len(crfs))
                r = account("ref", make_ref.remote(name, p["start"], p["dur"]))
                ref = r["ref"]
                meta(ref=ref, ref_reused=r.get("reused", False))

            meta(state="encoding", kind="sweep", total_chunks=len(crfs), ref=ref)

            args = [{"job_id": job_id, "idx": i, "crf": c, "params": p, "ref": ref}
                    for i, c in enumerate(crfs)]
            rows = list(sweep_one.with_options(cpu=cpu, memory=mem).map(
                args, return_exceptions=True))

            bad = [(i, r) for i, r in enumerate(rows) if isinstance(r, Exception)]
            if bad:
                raise RuntimeError("有 {} 個 crf 失敗：{}".format(
                    len(bad), "；".join("crf {} {!r}".format(crfs[i], r) for i, r in bad[:3])))

            for r in rows:
                account("crf {}".format(r["crf"]), r)
            rows.sort(key=lambda r: r["crf"])
            # 邊際取捨：固定門檻只告訴你過或不過，看不到附近的性價比
            for i, r in enumerate(rows):
                if i:
                    r["d_vmaf"] = r["vmaf_mean"] - rows[i - 1]["vmaf_mean"]
                    r["d_size"] = r["size"] - rows[i - 1]["size"]

            ok = [r for r in rows
                  if r["vmaf_mean"] >= p["min_mean"] and r["vmaf_min"] >= p["min_low"]]
            pick = min(ok, key=lambda r: r["size"])["crf"] if ok else None

            res = {"table": rows, "pick": pick, "ref": ref,
                   "min_mean": p["min_mean"], "min_low": p["min_low"]}
            meta(state="done", usage=totals(), **res)
            return res

        if p.get("test"):
            meta(state="encoding", total_chunks=1)
            res = account("clip", encode_clip.with_options(cpu=cpu, memory=mem).remote(
                job_id, name, p, p["out_name"]))
            meta(state="done", usage=totals(), **res)
            return res

        meta(state="splitting")
        sp = account("split", split.remote(name, p["chunk_sec"]))
        work, parts = sp["work"], sp["parts"]
        meta(state="encoding", total_chunks=len(parts), split_reused=sp["reused"])

        k = enc_key(p)
        args = [{"job_id": job_id, "idx": i, "src": s, "params": p, "work": work, "enc_key": k}
                for i, s in enumerate(parts)]
        results = list(encode_chunk.with_options(cpu=cpu, memory=mem).map(
            args, return_exceptions=True))

        failed = [(i, r) for i, r in enumerate(results) if isinstance(r, Exception)]
        if failed:
            raise RuntimeError("有 {} 段失敗：{}".format(
                len(failed), "；".join("第 {} 段 {!r}".format(i, r) for i, r in failed[:3])))

        for i, r in enumerate(results):
            account("chunk {}".format(i), r)
        retries = sum((r.get("attempt") or 1) - 1 for r in results)

        meta(state="merging")
        res = account("merge", merge.remote(
            name, work, [r["name"] for r in results], p["out_name"]))
        meta(state="done", usage=dict(totals(), retries=retries), **res)
        return res

    except Exception as e:
        # work/ 刻意留著：重送時可以沿用已經編好的段落
        meta(state="error", error=repr(e)[:2000])
        raise
