"""Modal 端 AV1 轉檔：切段 → 平行編碼 → 合併。

部署： modal deploy encode.py
送件： python submit.py <檔名>  或啟動 web/server.py

刻意不提供 local_entrypoint —— `modal run` 開的是 ephemeral app，
本機一斷線容器就跟著死，全片轉到一半白做。一律走 submit.py / 網頁。
"""

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
def split(job_id: str, name: str, chunk_sec: int) -> list:
    """依來源的關鍵影格切段（-c copy，不重新編碼，零損失）。

    只取視訊：段落較小，而且音軌完全不碰、留到 merge 才從原片 mux 回去，
    避免逐段複製音訊累積出同步漂移。
    """
    work = "/data/work/{}".format(job_id)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    _run(["ffmpeg", "-hide_banner", "-y", "-i", "/data/in/{}".format(name),
          "-map", "0:v:0", "-c", "copy", "-f", "segment",
          "-segment_time", str(chunk_sec), "-reset_timestamps", "1",
          "{}/src_%04d.mkv".format(work)])
    vol.commit()

    parts = sorted(f for f in os.listdir(work) if f.startswith("src_"))
    print("切成 {} 段".format(len(parts)))
    return parts


@app.function(image=image, volumes={"/data": vol}, cpu=8, memory=8192,
              timeout=12 * HOUR, retries=2)
def encode_chunk(arg: dict) -> str:
    """編碼一段。被搶佔時只有這一段要重來。"""
    job_id, idx, src, p = arg["job_id"], arg["idx"], arg["src"], arg["params"]
    work = "/data/work/{}".format(job_id)
    vol.reload()

    total = _duration("{}/{}".format(work, src))
    out_name = src.replace("src_", "enc_")
    tmp = "/tmp/{}".format(out_name)

    # 重跑時 pct 歸零，網頁上就看得出這段被搶佔過
    _put_chunk(job_id, idx, state="encoding", pct=0.0, out_time=0.0, fps=0.0, total=total)

    _encode_with_progress(
        _encode_cmd("{}/{}".format(work, src), tmp, p), total,
        lambda pct, secs, fps: _put_chunk(job_id, idx, state="encoding",
                                          pct=pct, out_time=secs, fps=fps, total=total))

    shutil.copy(tmp, "{}/{}".format(work, out_name))
    vol.commit()
    _put_chunk(job_id, idx, state="done", pct=100.0, out_time=total, fps=0.0,
               total=total, size=os.path.getsize(tmp))
    return out_name


@app.function(image=image, volumes={"/data": vol}, cpu=4, memory=4096,
              timeout=2 * HOUR, retries=2)
def merge(job_id: str, name: str, enc_files: list, out_name: str) -> dict:
    """concat 各段，再把原片的音軌/字幕 mux 回去。"""
    vol.reload()
    work = "/data/work/{}".format(job_id)

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
    shutil.copy("/tmp/final.mkv", "/data/out/{}".format(out_name))
    vol.commit()

    size = os.path.getsize("/tmp/final.mkv")
    shutil.rmtree(work, ignore_errors=True)   # 成功後才清掉，失敗重跑時 work 還在
    vol.commit()

    print("完成：out/{} {:.1f} MiB".format(out_name, size / 2 ** 20))
    return {"out": "out/{}".format(out_name), "size": size,
            "src_duration": src_d, "out_duration": out_d, "parts_duration": parts_d,
            "chunks_merged": len(enc_files), "warning": warning}


@app.function(image=image, volumes={"/data": vol}, cpu=8, memory=8192,
              timeout=6 * HOUR, retries=2)
def encode_clip(job_id: str, name: str, p: dict, out_name: str) -> dict:
    """測試片段：單一容器，真的只轉 start 起算的 dur 秒。

    舊版的 --test 只改輸出檔名、完全沒切片段，結果每次「測試」都在轉全片。
    """
    tmp = "/tmp/{}".format(out_name)
    total = float(p["dur"])
    _put_chunk(job_id, 0, state="encoding", pct=0.0, out_time=0.0, fps=0.0, total=total)

    _encode_with_progress(
        _encode_cmd("/data/in/{}".format(name), tmp, p, seek=(p["start"], str(p["dur"]))),
        total,
        lambda pct, secs, fps: _put_chunk(job_id, 0, state="encoding",
                                          pct=pct, out_time=secs, fps=fps, total=total))

    os.makedirs("/data/out", exist_ok=True)
    shutil.copy(tmp, "/data/out/{}".format(out_name))
    vol.commit()

    size = os.path.getsize(tmp)
    out_d = _duration(tmp)
    _put_chunk(job_id, 0, state="done", pct=100.0, out_time=total, fps=0.0,
               total=total, size=size)
    print("完成：out/{} {:.1f} MiB {:.2f}s".format(out_name, size / 2 ** 20, out_d))
    return {"out": "out/{}".format(out_name), "size": size,
            "src_duration": total, "out_duration": out_d, "warning": ""}


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

    def meta(**kw):
        cur = jobs.get(key) or {}
        cur.update(kw)
        cur["updated"] = time.time()
        jobs.put(key, cur)

    try:
        cpu = p.get("cpu") or 8
        mem = p.get("mem") or int(cpu) * 1024

        if p.get("test"):
            meta(state="encoding", total_chunks=1)
            res = encode_clip.with_options(cpu=cpu, memory=mem).remote(
                job_id, name, p, p["out_name"])
            meta(state="done", **res)
            return res

        meta(state="splitting")
        parts = split.remote(job_id, name, p["chunk_sec"])
        meta(state="encoding", total_chunks=len(parts))

        args = [{"job_id": job_id, "idx": i, "src": s, "params": p}
                for i, s in enumerate(parts)]
        results = list(encode_chunk.with_options(cpu=cpu, memory=mem).map(
            args, return_exceptions=True))

        failed = [(i, r) for i, r in enumerate(results) if isinstance(r, Exception)]
        if failed:
            raise RuntimeError("有 {} 段失敗：{}".format(
                len(failed), "；".join("第 {} 段 {!r}".format(i, r) for i, r in failed[:3])))

        meta(state="merging")
        res = merge.remote(job_id, name, results, p["out_name"])
        meta(state="done", **res)
        return res

    except Exception as e:
        meta(state="error", error=repr(e)[:2000])
        raise
