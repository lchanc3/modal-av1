# modal-av1

用 [Modal](https://modal.com) 的雲端算力跑 SVT-AV1 轉檔。長片**切成多段平行編碼**，
被搶佔只虧那一段，編好的段落重送時直接沿用。本機只負責上傳、下指令、下載和打分；
送件之後工作在雲端跑，關掉瀏覽器、server 或關機都不影響。

參考：約一小時的 1080p 影片，preset 3、每段 8 核（當時每段約 9 分鐘），耗時 35 分鐘、$1.9。
實測數據見 [NOTES.md](NOTES.md)。

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

## 成本

- 只有函式執行期間按秒計費；Volume 每月 1 TiB 以內免費。
- 8 核 + 8 GiB ≈ **$0.83/小時**。
- 每個 job 跑完會顯示「實際 $X　預估 $Y」。送件表單的預估是刻意保守的粗估
  （片長寫死 1 小時），**以完成後的實際數字為準**。

**搶佔是主要的花錢原因**：容器跑得越久、要的核心越多，被回收的機率越高。
Modal 會自動重試，跟你設的 `retries` 無關。Dashboard → Apps → av1-encode →
Containers 裡，**狀態 `Terminated` 帶 1 error、幾秒後下一個容器接上，就是被搶佔了**。

注意事項：不要刪除執行中任務的輸入檔，否則該次轉檔會失敗、費用照算；刪除無法
復原，正式輸出確認可播放後再刪；同時送多部片會加快額度消耗。用任何雲端平台前，
確認上傳內容符合該平台的使用條款。

殘留任務檢查：

```bat
modal app list
modal app stop av1-encode
```

## 疑難排解

| 狀況 | 原因 / 解法 |
|---|---|
| 找不到 `modal` 指令 | venv 未啟用，或改用 `python -m modal ...` |
| `Lookup failed for Function 'run_job' from the 'av1-encode' app` | 還沒 `modal deploy encode.py` |
| ffmpeg 輸出整片紅字 | 正常，進度資訊輸出在 stderr，Modal 以紅色顯示 |
| `Failed to set thread priority` | 容器限制，無影響 |
| VMAF 只有 30 幾分 | 幀沒對齊。不要各自用 `-ss` 切片比對，改用 ref 流程；並用 `setpts=N/FRAME_RATE/TB` 以幀序號對齊 |
| 測試檔互相覆蓋 | preset/crf 相同且沒給不同 `--tag` |
| `volume get` 失敗 | 本機已有同名檔，加 `--force` |
| 輸出 mp4 失敗 | 音軌格式（DTS、TrueHD、FLAC）不相容，改用 mkv |
| 網頁上某一段進度突然歸零 | 那段被搶佔了，正在重跑。只賠掉那一段，不用理它 |
| 網頁顯示「driver 已消失」 | driver 結束了但狀態沒收尾。按取消清乾淨再重送 |
| 改了 `jobspec.py` 但網頁行為沒變 | server 啟動時就載入了模組，要重啟 `start-web.bat` |
| 輸出比來源長 | 來源帶 edit list（起始裁切），`-c copy` 切段會把被隱藏的前置幀一起帶出來。不是合併出錯 |
| 送件後網頁沒反應 | 先看 `modal app list`，工作在雲端跑，與網頁無關 |
| Windows 主控台中文亂碼 | `chcp 65001` 並設 `PYTHONIOENCODING=utf-8`，`start-web.bat` 已經做了 |

## 授權

[GNU General Public License v3.0](LICENSE)　Copyright (C) 2026 shiho

可自由使用、修改、散布，但**散布修改後的版本時必須同樣以 GPL-3.0 公開原始碼**。

容器裡用的 ffmpeg 是官方 GPL 建置，在容器建置時才下載（見 `encode.py` 的
`image`），本 repo 不散布其二進位檔。
