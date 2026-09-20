# modal-av1

用 [Modal](https://modal.com) 的雲端算力跑 SVT-AV1 轉檔。長片**切成多段平行編碼**，
本機只負責上傳、下指令、下載和打分。

送件之後工作在雲端跑 —— 關掉瀏覽器、關掉 server、關機都不影響。

## 這東西解決什麼

在自己的機器上用 SVT-AV1 轉一部片要好幾個小時，而且轉檔期間機器等於被佔住。
丟到雲端可以快得多，但雲端的便宜費率是**可搶佔**的：一次搶佔就要從頭重編，
運氣差的時候帳單會變成好幾倍。

切段解決的就是這件事。每段各自一個容器，被搶佔只賠掉那一段，而且段落編好
會留著，重送時直接沿用。

以一支 62 分鐘的片實測：

| 做法 | 牆鐘時間 | 花費 |
|---|---|---|
| 切段前：單一 16 核容器，preset 4 | 約 1.8 小時 | 約 $1.6 |
| 切段前：單一 16 核容器，preset 3 | 約 3 小時 | 約 $2.7 |
| **切段前：同上，被搶佔 7 次（實際發生過）** | **10.7 小時** | **$9.4** |
| **切 7 段 × 8 核，preset 3** | **約 35 分鐘** | **約 $1.9** |

切段前每部片是 1.6–2.7 美元「但可能變成 9 美元」；切段後穩定在 2 美元左右。

## 初次設定

需要 Python 3.12 與一個 Modal 帳號。

```bat
python -m venv .venv
.venv\Scripts\activate

python -m pip install --upgrade pip
pip install -r requirements.txt
modal setup

modal volume create videos
```

- `modal setup` 會開瀏覽器登入，token 存在 `%USERPROFILE%\.modal.toml`，
  **不在這個 repo 裡**。重建 venv 不需要重新登入。
- PowerShell 啟用 venv 用 `.venv\Scripts\Activate.ps1`；若被擋，先執行
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`。
- 本機 VMAF 打分（`vmaf.py`）另外需要含 libvmaf 的 ffmpeg，那是系統相依，
  不由 pip 管理。

Volume 目錄結構：

```
videos/
├── in/    原片、ref 參考片
└── out/   轉檔輸出、測試檔
```

## 用法

```bat
start-web.bat
```

開 <http://127.0.0.1:8765>。上傳、選參數、送件、看進度、下載、清理都在這一頁。

三個分頁：

- **轉檔** —— 上傳原片、送件、進度與歷史、下載
- **測試** —— 雲端切一段無損 ref，掃一排 crf，當場 VMAF 打分，結果可一鍵帶進送件表單
- **模板** —— 各片源存一組參數，可套用與匯入匯出

CLI 也可以：`submit.py` 送件、`cancel.py` 取消、`cleanup.py` 清 Volume。
兩條路都走 `jobspec.py`，行為一致。

## 架構

```
encode.py      Modal 端：split / encode_chunk / merge / run_job / encode_clip
jobspec.py     送件的共用邏輯（CLI 與網頁都走這支）
submit.py      CLI 送件
cancel.py      用 call id 取消
cleanup.py     清理 Volume
vmaf.py        本機 VMAF 批次打分
start-web.bat  啟動本機控制台
web/
  server.py    FastAPI，只綁 127.0.0.1
  index.html   單頁介面
```

一趟 job 的形狀：

```
run_job          driver，0.25 核，nonpreemptible=True
  ├─► split      4 核，-c copy 依來源關鍵影格切段（零損失，不重新編碼）
  ├─► encode_chunk.map()   每段一個 8 核容器，可搶佔，retries=2
  └─► merge      4 核，concat 各段，再把原片的音軌/字幕 mux 回去
```

音軌完全不進切段流程，`merge` 時才從原片整條 mux 回去，避免逐段複製音訊
累積出同步漂移。

### 幾個不太直覺的設計

**每段 8 核，不是開越大越好。** SVT-AV1 超過 8 核之後執行緒效率會下降，而且
小容器在可搶佔池裡更容易排到。核心數可以在送件表單或 `--cpu` 調整，8 是
`jobspec.py` 裡的預設值。

**只有 driver 買「不被搶佔」的保險。** 它只要 0.25 核，但它一死整個 job 的進度
就斷了。真正花錢的編碼工作留在便宜的可搶佔池裡。反過來做（編碼容器也設
nonpreemptible）要 3 倍費率。

**續跑靠檔名，不靠快取表。** 工作目錄名 = hash(來源檔名 + 每段秒數)，段落檔名
內嵌 hash(preset, crf, gop, svt)。相同參數重送直接沿用已編好的段落，改了任何
編碼參數就自動不會沿用 —— 不必去記哪個快取還有效，檔名本身保證正確性。
段落檔先寫 `.part` 再 rename，容器中途死掉不會留下半個檔被誤認成完成。

**進度回報的每個 key 只有一個 writer**，完全避開 read-modify-write 競態。
driver 是否還活著不用 heartbeat 判斷（它阻塞在 `.map()` 時沒辦法更新任何東西），
改查 `FunctionCall.get(timeout=0)`。

**切段不影響畫質**，因為 CRF 是逐幀的量化目標，不是全片的位元率分配。同一段
畫面在 10 分鐘的段落裡和在 62 分鐘的整片裡，量化決策一樣。**但如果改用 2-pass
或指定總位元率，這個前提就不成立了。**

## 實測結果

來源：1080p59.94、H.264 Baseline 約 7.9 Mbps、62 分鐘。
20 秒無損 ref，`film-grain=0`、無 variance boost，gop 600。

| 參數 | 大小 | VMAF 平均 | 調和 | 最低 |
|---|---|---|---|---|
| p3 crf28 | 11.3 MiB | 97.51 | 97.47 | 92.23 |
| p3 crf30 | 10.0 MiB | 96.96 | 96.92 | 91.60 |
| p3 crf32 | 8.5 MiB | 96.09 | 96.05 | 90.95 |
| **p3 crf34** | **7.4 MiB** | **95.25** | **95.20** | **89.79** |
| p3 crf36 | 6.4 MiB | 94.23 | 94.18 | 88.99 |
| p3 vb1（crf30 + variance boost 1） | 16.2 MiB | 98.23 | 98.21 | 92.09 |
| p4 crf30 | 10.0 MiB | 96.60 | 96.55 | 91.18 |
| p4 crf34 | 7.4 MiB | 94.90 | 94.85 | 89.91 |

**採用 preset 3、crf 34**，全片約 1.45 GB，約原片（3.6 GB）的 40%。

- **省額度替代**：preset 4、crf 34 —— 體積相同、分數差距在誤差內。
- **variance boost 不划算**：體積 +62%，分數僅 +1.3。
- p3 與 p4 同 CRF 下體積幾乎相同，preset 對此片源影響很小。
- 主觀覺得「糊」主要來自原片本身（H.264 Baseline），而非 AV1 編碼。

### 切段畫質對照

同一支 20 秒 ref，p3 crf34，一次單段、一次切成 3 段（每段 7 秒）：

| 版本 | 大小 | VMAF 平均 | 調和 | 最低 |
|---|---|---|---|---|
| 單段 | 7.422 MiB | 95.2522 | 95.2021 | 89.7910 |
| 切 3 段 | 7.442 MiB | 95.2338 | 95.1840 | 90.2431 |

**平均差 0.018 分（0.02%），體積 +0.27%，最低分反而高了 0.45。** 而且這是嚴苛版
測試：20 秒內有 2 個交界，實際用 10 分鐘一段時每分鐘的交界數少約 85 倍。

## 成本

- **不會持續計費**：只有函式執行期間按秒計費，轉完容器自動關閉。
- **Volume 儲存**：每月 1 TiB 免費，放幾部片不會產生費用。
- 8 核 + 8 GiB ≈ **$0.83/小時**。`nonpreemptible=True` 是 3 倍費率，只值得用在
  driver 那種 0.25 核的小容器上。
- 這個費率是**從帳單回歸出來的**，不是 Modal 的牌價 —— 它把容器開機、縮容閒置、
  被搶佔重跑這些「函式內量不到的時間」一起吸收進來了，所以
  `elapsed × 核心數 × 費率` 直接對得上帳單。見 `encode.py` 的 `RATE_CPU_S`。
- 每個 job 跑完會顯示「實際 $X　預估 $Y　±Z%　·　容器 N 核時」，被搶佔重跑過
  也會標出次數。送件表單的估算是刻意保守的粗估（片長寫死 1 小時），
  **實際數字以完成後那一行為準**。

搶佔是主要的花錢原因：預設費率買的是可搶佔實例，容器跑得越久、要的核心越多，
被回收的機率越高。Modal 會自動重試，而且這條路徑跟你設的 `retries` 無關。
Dashboard → Apps → av1-encode → Containers 分頁裡，**狀態 `Terminated` 且帶
1 error、下一個容器在幾秒後自動接上，就是被搶佔了**。

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
| `Volume 'videos' not found` | 尚未建立，執行 `modal volume create videos` |
| 找不到 `modal` 指令 | venv 未啟用，或改用 `python -m modal ...` |
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
