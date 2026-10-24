"""在終端機看雲端 log。包一層 `modal app logs`，省得記參數。

Modal 一次最多取 20000 行、時間範圍不能超過 35 天，而且舊的 log 留不久
（2026-10-25 實測只查得到最後一次部署之後的），要查的話趁早。
"""

import argparse
import datetime
import os
import subprocess
import sys

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
args = p.parse_args()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def modal_logs(*extra) -> list:
    return [sys.executable, "-m", "modal", "app", "logs", jobspec.APP, "--timestamps", *extra]


env = dict(os.environ, PYTHONIOENCODING="utf-8")
since, until, follow = args.since, args.until, False

if args.job:
    d = jobspec.get_dict()
    job_id = args.job
    if job_id == "last":
        index = d.get("jobs:index") or []
        if not index:
            sys.exit("還沒有任何 job")
        job_id = index[-1]
    job = jobspec.read_job(d, job_id)
    if not job["created"]:
        sys.exit("找不到 job {}".format(job_id))
    since = start = iso(job["created"] - 5)
    if job["state"] in jobspec.TERMINAL:
        until = iso((job["updated"] or job["created"]) + 60)
    elif not args.preempt:
        # -f 不能指定起點，但開頭會先補印最近一百行左右。這裡的 log 很稀疏，
        # 通常已經涵蓋整個 job；先抓一次再跟的話，那一段會重複印兩次
        since, follow = None, True
    print("job {}　{}　{}　{} 起".format(job_id, job["name"], job["state"], start[:19].replace("T", " ")),
          flush=True)

if not since and (args.preempt or args.until):
    since = "1d"

if since:
    cmd = modal_logs("--since", since, "--tail", "20000")
    if until:
        cmd += ["--until", until]
    if args.search:
        cmd += ["--search", args.search]
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
        when = since[:19].replace("T", " ") if args.job else since
        print("被搶佔 {} 次（{} 起）".format(n, when) if n else "這段時間沒有被搶佔（{} 起）".format(when))

if follow or not since:
    cmd = modal_logs("-f")
    if args.search:
        cmd += ["--search", args.search]
    try:
        subprocess.run(cmd, env=env)
    except KeyboardInterrupt:
        pass
