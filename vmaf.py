import glob, json, os, re, subprocess, sys

ref = sys.argv[1] if len(sys.argv) > 1 else "ref.mkv"
folder = sys.argv[2] if len(sys.argv) > 2 else "out"
stem = os.path.splitext(os.path.basename(ref))[0]
F = "settb=AVTB,setpts=N/FRAME_RATE/TB,format=yuv420p10le"
LOG = "vmaf_tmp.json"   # 用相對路徑，避開 Windows 磁碟機代號的跳脫問題

files = sorted(glob.glob(os.path.join(folder, f"{stem}_test_*.mkv")))
if not files:
    sys.exit(f"{folder} 裡找不到 {stem}_test_*.mkv")

print(f"{'大小':>10}  {'平均':>6}  {'調和':>6}  {'最低':>6}  檔案")
for f in files:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", f, "-i", ref, "-an", "-lavfi",
         f"[0:v]{F}[d];[1:v]{F}[r];[d][r]libvmaf=n_threads=12:log_fmt=json:log_path={LOG}",
         "-f", "null", "-"],
        capture_output=True)
    try:
        with open(LOG, encoding="utf-8") as fp:
            v = json.load(fp)["pooled_metrics"]["vmaf"]
        s = f"{v['mean']:6.2f}  {v['harmonic_mean']:6.2f}  {v['min']:6.2f}"
    except Exception:
        s = f"{'失敗':>6}  {'':>6}  {'':>6}"
    print(f"{os.path.getsize(f) / 2**20:8.1f}MiB  {s}  {os.path.basename(f)}")

if os.path.exists(LOG):
    os.remove(LOG)