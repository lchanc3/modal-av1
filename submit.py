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
               help="每段秒數（預設 120，開前處理時至少 240）。調大＝交界少，調小＝被搶佔時賠得少")
p.add_argument("--cpu", type=int, help="每段的核心數（預設 8）")
p.add_argument("--test", action="store_true", default=None,
               help="只轉一小段測試（真的只轉，不是只改檔名）")
p.add_argument("--start", help="測試片段起點，預設 00:05:00")
p.add_argument("--dur", type=int, help="測試片段長度（秒），預設 20")
p.add_argument("--lada", action="store_true", default=None, help="先用 Lada 去馬賽克（GPU）")
p.add_argument("--lada-model", dest="lada_model", help="偵測模型，預設 v4-accurate")
p.add_argument("--sr", action="store_true", default=None, help="先用 Real-ESRGAN 超分（GPU）")
p.add_argument("--sr-model", dest="sr_model", help="general-x4v3（預設）或 general-wdn-x4v3")
p.add_argument("--sr-height", dest="sr_height", type=int, help="超分後的高度，預設 1080")
p.add_argument("--gpu", help="前處理用的 GPU，預設 L4")
args = p.parse_args()

params = jobspec.build(**{k: v for k, v in vars(args).items()})
r = jobspec.submit(args.name, params)

mode = "測試片段 {} 起 {} 秒".format(params["start"], params["dur"]) if params["test"] \
    else "全片，每段 {} 秒 × {} 核".format(params["chunk_sec"], params["cpu"])
if params["lada"] or params["sr"]:
    mode += "；前處理（{}）：{}".format(params["gpu"], "、".join(
        x for x in ("去馬賽克 " + params["lada_model"] if params["lada"] else "",
                    "超分 {} → {}p".format(params["sr_model"], params["sr_height"]) if params["sr"] else "")
        if x))

print("已送出：{} → out/{}".format(args.name, r["out_name"]))
print("模式：  {}".format(mode))
print("job id：{}".format(r["job_id"]))
print("call id：{}".format(r["call_id"]))
print()
print("看進度： python -m web.server 開網頁，或 modal app logs av1-encode")
print("查結果： modal volume ls videos out")
print("要取消： python cancel.py {}".format(r["call_id"]))
