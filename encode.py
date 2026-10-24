"""Modal 端 AV1 轉檔：切段 →（可選）GPU 前處理 → 平行編碼 → 合併。

前處理是去馬賽克（Lada）與超分（Real-ESRGAN），預設都關閉；關閉時流程與以前完全相同。

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
import re
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

# GPU 前處理（去馬賽克 Lada、超分 Real-ESRGAN）用的 image。直接拿 Lada 官方 image：
# PyTorch、模型權重、含 NVENC 的 ffmpeg 都在裡面，超分只要再補 spandrel 和權重。
# 版本釘死，latest 換版時行為才不會悄悄改變。
# image 是用 lada 使用者裝的（套件在它的 user site），Modal 以 root 執行，
# 所以要自己把套件路徑、模型位置、PATH 指回去。
LADA_SITE = "/home/lada/.local/lib/python3.13/site-packages"
SR_DIR = "/sr"
SR_MODELS = {
    "general-x4v3": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth",
    "general-wdn-x4v3": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-wdn-x4v3.pth",
}
gpu_image = (modal.Image.from_registry("ladaapp/lada:v0.11.0")
    .entrypoint([])
    .run_commands(
        "python -m pip install --no-cache-dir --no-deps --target {}/py spandrel==0.4.1 einops safetensors".format(SR_DIR),
        *["python -c \"import urllib.request as u; u.urlretrieve('{}', '{}/{}.pth')\"".format(url, SR_DIR, k)
          for k, url in SR_MODELS.items()])
    .env({"PYTHONPATH": "{}:{}/py".format(LADA_SITE, SR_DIR),
          "LADA_MODEL_WEIGHTS_DIR": "/model_weights",
          "LOCALE_DIR": LADA_SITE + "/lada/locale",
          "PATH": "/home/lada/.local/bin:/usr/local/bin:/usr/bin:/bin"}))

BASE_SVT = "tune=0:enable-overlays=1:enable-qm=1:film-grain=0"

HOUR = 3600

# Modal 的基本費率（可搶佔）。nonpreemptible 是 3 倍。
#
# 這兩個數字是「用量 → 帳單」的迴歸值，不是 Modal 的牌價：它們把容器開機、
# 縮容閒置、被搶佔重跑這些「函式內量不到的時間」一起吸收進費率裡，好讓
# elapsed × 核心數 直接算得出帳單。原本照牌價推的 0.0000131 / 0.00000222
# 只算到函式體內的時間，實測低估了近一倍。
#
# 校正來源：2026-09-19 facialabuse.e1007 全片（16 段 / 62.4 分鐘）。
# 帳單 cost_by_resource 為 CPU $2.2556、Memory $0.2404，比例 9.38:1；
# 舊常數在 8 核 8 GiB 下只有 5.90:1。沒回報的時間會同時放大 CPU 和 Memory、
# 不會動到比例，所以比例對不上就代表常數本身偏了，而且是 CPU 偏得多。
# 用這組新值回推：全片那趟算出 $2.5009、實收 $2.4960（100.2%）。
#
# 已知偏差：20 秒的 crf 掃描會低估約 13%（同一天實測 $0.1326 vs $0.1526）。
# 每個容器的固定開機成本攤在 90 秒的工作上，比攤在 10 分鐘上重得多，
# 單一費率吃不掉這段。掃描一趟才一毛多，不值得為它再開一組常數。
# 掃描選中的 crf 預估輸出達到原片這個比例以上，就警告「轉了不值得」。
# 前端 index.html 的同名常數要一起改。
SWEEP_WORTH = 0.9

RATE_CPU_S = 0.0000262      # 美元 / 實體核心 / 秒
RATE_MEM_S = 0.00000276     # 美元 / GiB / 秒

# GPU 牌價（美元 / 秒）。GPU 容器的 CPU 與記憶體沿用上面的迴歸費率，
# 實測會高估：2026-10-24 L4 + 4 核 + 16 GiB 的超分測試算出 $0.54，帳單 $0.46。
GPU_RATE_S = {"T4": 0.000164, "L4": 0.000222, "A10": 0.000306, "L40S": 0.000542}


def _usage(t0: float, cpu: float, mem_mib: int, nonpreemptible: bool = False,
           gpu: str = "") -> dict:
    """一個階段實際用掉的資源與費用。

    elapsed 只是函式體內的時間，但費率已經把容器開機與重跑攤進去了（見上面
    RATE_CPU_S 的說明），所以 cost 對得上帳單。elapsed 本身仍然只是下限，
    要看真正的牆鐘請用 driver 的 wall。
    """
    el = time.time() - t0
    rate = cpu * RATE_CPU_S + (mem_mib / 1024.0) * RATE_MEM_S + GPU_RATE_S.get(gpu, 0.0)
    return {"elapsed": round(el, 1), "cpu": cpu, "mem": mem_mib, "gpu": gpu,
            "cost": round(rate * el * (3 if nonpreemptible else 1), 5)}


# --------------------------------------------------------------------------
# 內容定址
# --------------------------------------------------------------------------

def _key(*parts) -> str:
    return hashlib.sha1("|".join(str(x) for x in parts).encode()).hexdigest()[:12]


def work_dir(name: str, chunk_sec: int) -> str:
    """切段結果只跟來源與段長有關。"""
    return "/data/work/{}".format(_key(name, chunk_sec))


def prep_on(p: dict) -> bool:
    return bool(p.get("lada") or p.get("sr"))


def prep_key(p: dict) -> str:
    """前處理結果只看前處理的參數。"""
    return _key("prep", p.get("lada") and p.get("lada_model"),
                p.get("sr") and p.get("sr_model"), p.get("sr") and p.get("sr_height"))


def enc_key(p: dict) -> str:
    """編碼結果還要看編碼參數 —— 改了 crf 就不該沿用舊的段落。

    有前處理時，餵進來的畫面不同，所以前處理的 key 也要算進去。
    沒有前處理時刻意不加，舊的段落照樣沿用得到。
    """
    parts = [p["preset"], p["crf"], p["gop"], p["svt"]]
    if prep_on(p):
        parts.append(prep_key(p))
    return _key(*parts)


# --------------------------------------------------------------------------
# 容器內的小工具
# --------------------------------------------------------------------------

def _duration(path: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def _secs(t) -> float:
    """'00:05:00'、'5:00'、'300'、300 → 秒。"""
    s = 0.0
    for part in str(t).split(":"):
        s = s * 60 + float(part)
    return s


def _src_stats(path: str, start, dur: float) -> dict:
    """原片的視訊位元率：ref 那一段的，和全片平均的。

    掃描結果要跟「同一段」的原片比才公平 —— 同一支片的段落之間位元率可以差好幾倍。
    只讀這一段的封包大小（不解碼），4K 片也只要幾秒。全片平均用來換算預估總大小。
    """
    s = _secs(start)
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-read_intervals", "{}%+{}".format(s, dur + 1),
                        "-show_entries", "packet=pts_time,size", "-of", "json", path],
                       capture_output=True, text=True, check=True)
    seg = sum(int(k["size"]) for k in json.loads(r.stdout).get("packets", [])
              if k.get("pts_time") not in (None, "N/A") and s <= float(k["pts_time"]) < s + dur)

    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=bit_rate:format=duration,size", "-of", "json", path],
                       capture_output=True, text=True, check=True)
    info = json.loads(r.stdout)
    fmt = info.get("format", {})
    total_d, size = float(fmt["duration"]), int(fmt["size"])
    # mkv 常常沒有 stream bit_rate，退回整檔平均（含音軌，所以會略微高估原片）
    br = (info.get("streams") or [{}])[0].get("bit_rate")
    kbps = int(br) / 1000 if br not in (None, "N/A") else size * 8 / total_d / 1000
    return {"src_seg_kbps": round(seg * 8 / dur / 1000, 1), "src_kbps": round(kbps, 1),
            "src_duration": total_d, "src_bytes": size}


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
    # 有前處理時 src 是 prep_*，段號另外從 part 帶進來
    out_name = "enc_{}_{}".format(key, arg.get("part") or src[len("src_"):])
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


def _run_tqdm(cmd: list, report) -> str:
    """跑一個用 tqdm 印進度的程式（lada-cli），每 5 秒回報一次百分比。

    tqdm 用 \\r 覆寫同一行，所以按位元組讀、自己切行。回傳輸出的尾巴供除錯。
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    buf, tail, last = b"", b"", 0.0
    while True:
        b = proc.stdout.read(4096)
        if not b:
            break
        buf += b
        tail = (tail + b)[-8000:]
        *lines, buf = re.split(rb"[\r\n]", buf)
        now = time.time()
        if now - last < 5:
            continue
        for line in reversed(lines):
            m = re.search(rb"(\d+)%\|", line)
            if m:
                last = now
                report(float(m.group(1)))
                break
    proc.wait()
    out = tail.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise RuntimeError("{} 失敗（{}）：\n{}".format(cmd[0], proc.returncode, out[-4000:]))
    return out


def _upscale(src: str, dst: str, model: str, out_h: int, report, batch: int = 4) -> None:
    """Real-ESRGAN 4× 放大後縮到 out_h，寫成高畫質的中間檔。

    中間檔之後還要再壓一次 AV1，所以用 x264 crf 14 盡量不丟東西；
    veryfast 是因為這一步的瓶頸在 GPU，CPU 編碼跟得上就好。
    """
    import numpy as np
    import spandrel
    import torch
    import torch.nn.functional as F

    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                        "-show_entries", "stream=width,height,r_frame_rate,nb_read_packets",
                        "-of", "csv=p=0", src], capture_output=True, text=True, check=True)
    w, h, fps, frames = r.stdout.strip().split(",")[:4]
    w, h, frames = int(w), int(h), int(frames)
    ow, oh = round(w * out_h / h / 2) * 2, out_h

    desc = spandrel.ModelLoader().load_from_file("{}/{}.pth".format(SR_DIR, model))
    desc.cuda().eval()
    half = desc.supports_half
    if half:
        desc.model.half()

    # passthrough：rawvideo 輸出預設會依時間戳補幀或丟幀，實測 1456 幀進、1458 幀出，
    # 段落長度一變，合併後就跟原片的音軌對不齊
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", src, "-fps_mode", "passthrough",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "{}x{}".format(ow, oh),
                            "-r", fps, "-i", "-", "-c:v", "libx264", "-crf", "14",
                            "-preset", "veryfast", "-pix_fmt", "yuv420p", dst],
                           stdin=subprocess.PIPE)
    size, n, last = w * h * 3, 0, 0.0
    while True:
        buf = []
        for _ in range(batch):
            b = dec.stdout.read(size)
            if len(b) < size:
                break
            buf.append(np.frombuffer(b, np.uint8).reshape(h, w, 3))
        if not buf:
            break
        x = torch.from_numpy(np.stack(buf)).cuda().permute(0, 3, 1, 2)
        x = x.half() / 255 if half else x.float() / 255
        with torch.inference_mode():
            y = F.interpolate(desc(x).float(), size=(oh, ow), mode="bicubic", antialias=True)
        y = (y.clamp(0, 1) * 255).round().byte().permute(0, 2, 3, 1).contiguous().cpu().numpy()
        enc.stdin.write(y.tobytes())
        n += len(buf)
        if time.time() - last >= 5:
            last = time.time()
            report(min(100.0, n / frames * 100) if frames else 0.0)
    enc.stdin.close()
    if enc.wait() != 0 or dec.wait() != 0:
        raise RuntimeError("超分失敗：ffmpeg 結束碼 dec={} enc={}".format(dec.returncode, enc.returncode))


@app.function(image=gpu_image, volumes={"/data": vol}, gpu="L4", cpu=4, memory=8192,
              timeout=6 * HOUR, retries=2)
def prep_chunk(arg: dict) -> dict:
    """GPU 前處理一段：去馬賽克 → 超分，兩者都可選。結果交給 encode_chunk。

    跟 encode_chunk 一樣靠檔名續跑、先寫 /tmp 再 _publish。
    進度寫進同一個 chunk key，但不寫 attempt / out_time：encode_chunk 用 attempt
    判斷自己是不是被搶佔後重跑，用 out_time 算整體進度，前處理碰了會讓兩邊都誤判。
    """
    t0 = time.time()
    job_id, idx, src, p = arg["job_id"], arg["idx"], arg["src"], arg["params"]
    work, key, part = arg["work"], arg["prep_key"], arg["part"]
    gpu = p.get("gpu") or "L4"
    vol.reload()

    src_path = "{}/{}".format(work, src)
    total = _duration(src_path)
    out_name = "prep_{}_{}".format(key, part)
    dst = "{}/{}".format(work, out_name)

    if os.path.exists(dst):
        try:
            if abs(_duration(dst) - total) < 0.1:
                print("沿用第 {} 段前處理".format(idx))
                u = _usage(t0, 4, 8192, gpu=gpu)
                _put_chunk(job_id, idx, state="prepped", pct=100.0, prep_pct=100.0, total=total)
                return {"name": out_name, "usage": u, "reused": True}
            print("第 {} 段前處理長度不符，重做".format(idx))
        except Exception as e:
            print("第 {} 段前處理檢查失敗，重做：{!r}".format(idx, e))

    # 兩步都做時各佔一半進度
    steps = [s for s in ("lada", "sr") if p.get(s)]

    def reporter(step):
        i = steps.index(step)
        state = "lada" if step == "lada" else "upscaling"

        def report(pct):
            whole = (i * 100 + pct) / len(steps)
            _put_chunk(job_id, idx, state=state, pct=whole, prep_pct=whole, total=total)
        return report

    cur = src_path
    if p.get("lada"):
        reporter("lada")(0.0)
        out = "/tmp/lada_{}.mp4".format(part)
        _run_tqdm(["lada-cli", "--input", cur, "--output", out,
                   "--mosaic-detection-model", p.get("lada_model") or "v4-accurate",
                   "--encoding-preset", "hevc-nvidia-gpu-uhq"], reporter("lada"))
        cur = out
    if p.get("sr"):
        reporter("sr")(0.0)
        out = "/tmp/sr_{}.mkv".format(part)
        _upscale(cur, out, p.get("sr_model") or "general-x4v3",
                 int(p.get("sr_height") or 1080), reporter("sr"))
        cur = out

    _publish(cur, dst)
    vol.commit()
    u = _usage(t0, 4, 8192, gpu=gpu)
    _put_chunk(job_id, idx, state="prepped", pct=100.0, prep_pct=100.0, total=total)
    return {"name": out_name, "usage": u, "reused": False}


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
    src = "/data/in/{}".format(name)
    vol.reload()

    # 量不到只是少了「相對原片」那一欄，不該讓整個掃描失敗
    try:
        stats = _src_stats(src, start, dur)
        print("原片視訊：這段 {src_seg_kbps:.0f} kbps、全片平均 {src_kbps:.0f} kbps".format(**stats))
    except Exception as e:
        print("量原片位元率失敗：", repr(e))
        stats = {}

    # 檔名由來源與長度決定，所以同樣的組合可以直接沿用，不必重切
    if os.path.exists(dst):
        try:
            d = _duration(dst)
            if abs(d - dur) < 1.0:
                print("沿用既有 ref：in/{}".format(out))
                return {"ref": "in/{}".format(out), "ref_name": out,
                        "size": os.path.getsize(dst), "out_duration": d, "reused": True,
                        "src_stats": stats, "usage": _usage(t0, 4, 4096)}
        except Exception as e:
            print("既有 ref 檢查失敗，重切：", repr(e))

    _run(["ffmpeg", "-hide_banner", "-y", "-ss", start, "-t", str(dur),
          "-i", src,
          "-map", "0:v:0", "-an", "-sn",
          "-c:v", "libx264", "-qp", "0", "-preset", "fast", "-pix_fmt", "yuv420p", tmp])

    _publish(tmp, dst)
    vol.commit()
    size = os.path.getsize(tmp)
    print("ref 完成：in/{} {:.1f} MiB".format(out, size / 2 ** 20))
    return {"ref": "in/{}".format(out), "ref_name": out,
            "size": size, "out_duration": _duration(tmp), "reused": False,
            "src_stats": stats, "usage": _usage(t0, 4, 4096)}


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
    return dict(crf=crf, size=size, duration=total, usage=u, **v)


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
        """把一個階段的用量收進來，並回傳原本的結果。

        pop 而不是 get：usage 收進 stages 之後就不該再跟著 res 走。留著的話，
        下面 meta(state="done", usage=totals(), **res) 會因為 res 裡也有一個
        usage 而炸成 TypeError（multiple values for keyword argument）。

        副作用：sweep 的 table 每列不再帶自己的 usage。那份資料本來就跟
        totals()["stages"] 裡的 "crf N" 完全重複，前端讀的也是後者。
        """
        u = (res or {}).pop("usage", None)
        if u:
            stages.append(dict(u, stage=label))
        return res

    def totals():
        """實際用量。只含成功跑完的容器 —— 被搶佔中途死掉的不會回報，
        所以核心秒是下限；費率已經把那段攤進去，cost 仍然對得上帳單。"""
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
            src = {}
            if not ref:
                meta(state="splitting", kind="sweep", total_chunks=len(crfs))
                r = account("ref", make_ref.remote(name, p["start"], p["dur"]))
                ref = r["ref"]
                src = r.get("src_stats") or {}
                meta(ref=ref, ref_reused=r.get("reused", False), **src)

            meta(state="encoding", kind="sweep", total_chunks=len(crfs), ref=ref)

            def run_crfs(todo, base_idx):
                """跑一批 crf，回傳結果列。"""
                args = [{"job_id": job_id, "idx": base_idx + i, "crf": c,
                         "params": p, "ref": ref} for i, c in enumerate(todo)]
                out = list(sweep_one.with_options(cpu=cpu, memory=mem).map(
                    args, return_exceptions=True))
                bad = [(i, r) for i, r in enumerate(out) if isinstance(r, Exception)]
                if bad:
                    raise RuntimeError("有 {} 個 crf 失敗：{}".format(
                        len(bad), "；".join("crf {} {!r}".format(todo[i], r) for i, r in bad[:3])))
                for r in out:
                    account("crf {}".format(r["crf"]), r)
                return out

            def passes(r):
                return r["vmaf_mean"] >= p["min_mean"] and r["vmaf_min"] >= p["min_low"]

            def best(rs):
                ok = [r for r in rs if passes(r)]
                return min(ok, key=lambda r: r["size"])["crf"] if ok else None

            rows = run_crfs(crfs, 0)
            pick = best(rows)

            # 補掃邊界：階梯步進 2 會跳過真正的臨界點 —— 實測 E1001 的 30 過、
            # 32 不過，但中間的 31 也過，而且比 30 小 7.7%。多跑一兩個 crf
            # 只要幾分錢，卻可能省下全片上百 MB。
            if p.get("refine", True) and pick is not None:
                worse = [r["crf"] for r in rows if r["crf"] > pick]
                gap = list(range(pick + 1, min(worse))) if worse else []
                if 0 < len(gap) <= 3:
                    print("補掃邊界：", gap)
                    meta(state="encoding", total_chunks=len(rows) + len(gap), refining=gap)
                    rows += run_crfs(gap, len(rows))
                    pick = best(rows)

            rows.sort(key=lambda r: r["crf"])
            # 邊際取捨：固定門檻只告訴你過或不過，看不到附近的性價比
            for i, r in enumerate(rows):
                if i:
                    r["d_vmaf"] = r["vmaf_mean"] - rows[i - 1]["vmaf_mean"]
                    r["d_size"] = r["size"] - rows[i - 1]["size"]
                if src.get("src_seg_kbps"):
                    r["vs_src"] = (r["size"] * 8 / (r.get("duration") or p["dur"]) / 1000
                                   / src["src_seg_kbps"])

            # 過門檻不代表值得轉：原片本來就壓得很省時，AV1 為了重現原片（連同它的
            # 壓縮痕跡）得花一樣多的位元。實測 4K60 12 Mbps 的片 crf 30 才過 VMAF 95，
            # 全片 $9.7 換來比原片還大的檔案 —— 掃描表其實早就看得出來，只是沒人比對。
            warning = ""
            row = next((r for r in rows if r["crf"] == pick), None)
            if row and row.get("vs_src", 0) >= SWEEP_WORTH:
                est = row["vs_src"] * src["src_kbps"] * 1000 * src["src_duration"] / 8
                warning = ("crf {} 預估輸出約原片的 {:.0%}（全片約 {:.1f} GB，原片 {:.1f} GB），"
                           "轉了幾乎不會變小。可以考慮保留原片、改用更高的 crf 並放寬門檻，"
                           "或縮小解析度。").format(pick, row["vs_src"], est / 1e9,
                                               src["src_bytes"] / 1e9)
                print("注意：", warning)

            res = {"table": rows, "pick": pick, "ref": ref, "warning": warning,
                   "min_mean": p["min_mean"], "min_low": p["min_low"]}
            meta(state="done", usage=totals(), **res)
            return res

        if p.get("test"):
            if prep_on(p):
                raise ValueError("測試片段還不支援去馬賽克／超分，請用全片模式")
            meta(state="encoding", total_chunks=1)
            res = account("clip", encode_clip.with_options(cpu=cpu, memory=mem).remote(
                job_id, name, p, p["out_name"]))
            meta(state="done", usage=totals(), **res)
            return res

        meta(state="splitting")
        sp = account("split", split.remote(name, p["chunk_sec"]))
        work, parts = sp["work"], sp["parts"]
        nums = [s[len("src_"):] for s in parts]

        srcs = parts
        if prep_on(p):
            meta(state="prepping", total_chunks=len(parts), split_reused=sp["reused"])
            pk = prep_key(p)
            pargs = [{"job_id": job_id, "idx": i, "src": s, "part": nums[i], "params": p,
                      "work": work, "prep_key": pk} for i, s in enumerate(parts)]
            pres = list(prep_chunk.with_options(gpu=p.get("gpu") or "L4").map(
                pargs, return_exceptions=True))
            failed = [(i, r) for i, r in enumerate(pres) if isinstance(r, Exception)]
            if failed:
                raise RuntimeError("前處理有 {} 段失敗：{}".format(
                    len(failed), "；".join("第 {} 段 {!r}".format(i, r) for i, r in failed[:3])))
            for i, r in enumerate(pres):
                account("prep {}".format(i), r)
            srcs = [r["name"] for r in pres]

        meta(state="encoding", total_chunks=len(parts), split_reused=sp["reused"])

        k = enc_key(p)
        args = [{"job_id": job_id, "idx": i, "src": s, "part": nums[i], "params": p,
                 "work": work, "enc_key": k}
                for i, s in enumerate(srcs)]
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
