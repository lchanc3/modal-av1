"""在終端機看雲端 log。包一層 `modal app logs`，省得記參數。

Modal 一次最多取 20000 行、時間範圍不能超過 35 天，而且舊的 log 留不久
（2026-10-25 實測只查得到最後一次部署之後的），要查的話趁早。
--slow 讀的是 Dict 裡的 job 紀錄，不受 log 保留期限影響。
"""

import argparse
import datetime
import os
import subprocess
import sys
import time

import jobspec

PREEMPT = "terminated due to preemption"
RETRY = "次嘗試（前一次被中斷）"   # encode_chunk 重跑時印的

p = argparse.ArgumentParser(description="看 av1-encode 的雲端 log，預設即時跟著看（Ctrl+C 結束）")
p.add_argument("job", nargs="?",
               help="只看某個 job：job id 或 last（最新一個）。以送件到結束的時間篩選，"
                    "同時段有別的 job 也會混進來；還在跑就直接即時看")
p.add_argument("--since", help="查過去一段時間，例如 2h、1d，或 ISO 時間")
p.add_argument("--until", help="時間範圍的結尾，格式同 --since")
p.add_argument("--search", help="只留含這段文字的行")
p.add_argument("--preempt", action="store_true", help="只列出被搶佔與重跑的紀錄，最後統計次數")
p.add_argument("--slow", action="store_true",
               help="從 job 紀錄找出比同一個 job 其他段慢很多的段（不看 log，舊 job 也查得到）")
args = p.parse_args()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def modal_logs(*extra) -> list:
    cmd = [sys.executable, "-m", "modal", "app", "logs", jobspec.APP, "--timestamps", *extra]
    return cmd + (["--search", args.search] if args.search else [])


def follow(cmd: list) -> None:
    """即時跟著看，斷線就重連。

    -f 每次連上都會先補印最近一百行左右，所以用時間戳記跳過已經印過的。
    """
    last, seen = "", set()
    try:
        while True:
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE)
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                ts = line[:25]   # 2026-10-25 00:42:27+08:00
                if ts < last or (ts == last and line in seen):
                    continue
                if ts > last:
                    last, seen = ts, set()
                seen.add(line)
                print(line, flush=True)
            proc.wait()
            print("（雲端 log 串流中斷，5 秒後重連）", flush=True)
            time.sleep(5)
    except KeyboardInterrupt:
        pass


env = dict(os.environ, PYTHONIOENCODING="utf-8")
since, until, live = args.since, args.until, False

job = None
if args.job or args.slow:
    d = jobspec.get_dict()
    snap = jobspec.snapshot(d)
    index = snap.get("jobs:index") or []
    if args.job:
        job_id = index[-1] if args.job == "last" and index else args.job
        job = jobspec.read_job(d, job_id, snap)
        if not job["created"]:
            sys.exit("找不到 job {}".format(args.job))
        start = iso(job["created"] - 5)
        print("job {}　{}　{}　{} 起".format(job_id, job["name"], job["state"],
                                          start[:19].replace("T", " ")), flush=True)

if args.slow:
    from encode import stragglers
    n = 0
    for r in [job] if job else [jobspec.read_job(d, j, snap) for j in index]:
        rows = [(c["idx"], c.get("total"), c.get("elapsed")) for c in r["chunks"]
                if c.get("state") == "done" and not c.get("reused")]
        s = stragglers(rows)
        if s:
            n += 1
            print("{}　{}　{}".format(r["job_id"], r["name"], s))
    print("{} 個 job 有特別慢的段".format(n) if n else "沒有特別慢的段")
    sys.exit()

if job:
    since = start
    if job["state"] in jobspec.TERMINAL:
        until = iso((job["updated"] or job["created"]) + 60)
    elif not args.preempt:
        # -f 不能指定起點，但開頭會先補印最近一百行左右。這裡的 log 很稀疏，
        # 通常已經涵蓋整個 job；先抓一次再跟的話，那一段會重複印兩次
        since, live = None, True

if not since and (args.preempt or args.until):
    since = "1d"

if since:
    cmd = modal_logs("--since", since, "--tail", "20000", *(["--until", until] if until else []))
    if not args.preempt:
        subprocess.run(cmd, env=env)
    else:
        out = subprocess.run(cmd, env=env, capture_output=True).stdout.decode("utf-8", "replace")
        hits = [l for l in out.splitlines() if PREEMPT in l or RETRY in l]
        for l in hits:
            print(l.replace("Container terminated due to preemption. Your Function will be "
                            "restarted with the same input. For more details, see "
                            "https://modal.com/docs/guide/preemption", "容器被搶佔，Modal 自動重跑"))
        n = sum(PREEMPT in l for l in hits)
        when = since[:19].replace("T", " ") if job else since
        print("被搶佔 {} 次（{} 起）".format(n, when) if n else "這段時間沒有被搶佔（{} 起）".format(when))

if live or not since:
    follow(modal_logs("-f"))
