import argparse, fnmatch
import modal

p = argparse.ArgumentParser(description="清理 Modal Volume 的輸出檔")
p.add_argument("pattern", nargs="?", default="*", help='檔名篩選，例如 "*_test_*"')
p.add_argument("--dir", default="out", help="要清理的資料夾（預設 out）")
p.add_argument("-y", "--yes", action="store_true", help="不詢問直接刪除")
args = p.parse_args()

vol = modal.Volume.from_name("videos")
files = [e for e in vol.listdir(args.dir)
         if fnmatch.fnmatch(e.path.rsplit("/", 1)[-1], args.pattern)]

if not files:
    print("沒有符合的檔案")
    raise SystemExit

total = 0
for e in files:
    print(f"{e.size / 2**20:8.1f} MiB  {e.path}")
    total += e.size
print(f"共 {len(files)} 個檔案，{total / 2**20:.1f} MiB")

if not args.yes and input("確定刪除？(y/N) ").strip().lower() != "y":
    print("已取消")
    raise SystemExit

for e in files:
    vol.remove_file(e.path)
    print("已刪除", e.path)