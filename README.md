# modal-av1

用 [Modal](https://modal.com) 的雲端算力跑 SVT-AV1 轉檔。長片**切成多段平行編碼**，
被搶佔只虧那一段，編好的段落重送時直接沿用。本機只負責上傳、下指令、下載和打分；
送件之後工作在雲端跑，關掉瀏覽器、server 或關機都不影響。

參考：約一小時的 1080p 影片，preset 3、每段 8 核（當時每段約 9 分鐘），耗時 35 分鐘、$1.9。
實測數據見 [Wiki](https://github.com/lchanc3/modal-av1/wiki/實測數據)。

## 初次設定

需要 Python 3.12 與一個 Modal 帳號。

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
modal setup
modal deploy encode.py
```

- `modal setup` 會開瀏覽器登入，token 存在 `%USERPROFILE%\.modal.toml`，
  **不在這個 repo 裡**。重建 venv 不需要重新登入。
- PowerShell 啟用 venv 用 `.venv\Scripts\Activate.ps1`；若被擋，先執行
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`。
- 改了 `encode.py` 之後要重新 `modal deploy encode.py`。
- 本機 VMAF 打分（`vmaf.py`）另外需要含 libvmaf 的 ffmpeg，不由 pip 管理。

## 用法

```bat
start-web.bat
```

開 <http://127.0.0.1:8765>，三個分頁：

- **轉檔** —— 上傳原片、送件、進度與歷史、下載
- **測試** —— 雲端切一段無損 ref，掃一排 crf，當場 VMAF 打分，結果可一鍵帶進送件表單
- **模板** —— 各片源存一組參數，可套用與匯入匯出

CLI 也可以：`submit.py` 送件、`cancel.py <call id>` 取消、`cleanup.py` 清 Volume、
`vmaf.py <ref> [資料夾]` 本機打分。`submit.py` 和 `cleanup.py` 加 `-h` 看參數。

Volume `videos` 的結構：

```
in/     原片、ref 參考片
out/    轉檔輸出、測試檔
work/   切段暫存。成功的 job 會自己清掉，失敗或取消的會留著供重送沿用，
        網頁的「暫存切段」可以清理
```

## 成本與疑難排解

8 核 + 8 GiB ≈ $0.83/小時，只有函式執行期間計費。費率、搶佔、注意事項見 Wiki 的
[成本與搶佔](https://github.com/lchanc3/modal-av1/wiki/成本與搶佔)，常見狀況見
[疑難排解](https://github.com/lchanc3/modal-av1/wiki/疑難排解)。

## 授權

[GNU General Public License v3.0](LICENSE)　Copyright (C) 2026 shiho

可自由使用、修改、散布，但**散布修改後的版本時必須同樣以 GPL-3.0 公開原始碼**。

容器裡用的 ffmpeg 是官方 GPL 建置，在容器建置時才下載（見 `encode.py` 的
`image`），本 repo 不散布其二進位檔。
