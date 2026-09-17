"""CLI 送件。跟網頁走同一條路（jobspec.submit），行為完全一致。"""

import argparse

import jobspec

p = argparse.ArgumentParser(description="送出 AV1 轉檔到已部署的 av1-encode，不等待結果")
p.add_argument("name", help="Volume in/ 底下的檔名，例如 movie.mp4")
p.add_argument("--preset", type=int)
p.add_argument("--crf", type=int)
p.add_argument("--gop", type=int)
p.add_argument("--svt")
p.add_argument("--tag", default=None)
p.add_argument("--chunk", type=int, dest="chunk_sec",
               help="每段秒數（預設 600）。調大＝交界少，調小＝被搶佔時賠得少")
p.add_argument("--cpu", type=int, help="每段的核心數（預設 8）")
p.add_argument("--test", action="store_true", default=None,
               help="只轉一小段測試（真的只轉，不是只改檔名）")
p.add_argument("--start", help="測試片段起點，預設 00:05:00")
p.add_argument("--dur", type=int, help="測試片段長度（秒），預設 20")
args = p.parse_args()

params = jobspec.build(**{k: v for k, v in vars(args).items()})
r = jobspec.submit(args.name, params)

mode = "測試片段 {} 起 {} 秒".format(params["start"], params["dur"]) if params["test"] \
    else "全片，每段 {} 秒 × {} 核".format(params["chunk_sec"], params["cpu"])

print("已送出：{} → out/{}".format(args.name, r["out_name"]))
print("模式：  {}".format(mode))
print("job id：{}".format(r["job_id"]))
print("call id：{}".format(r["call_id"]))
print()
print("看進度： python -m web.server 開網頁，或 modal app logs av1-encode")
print("查結果： modal volume ls videos out")
print("要取消： python cancel.py {}".format(r["call_id"]))
