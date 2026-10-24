# 開發筆記

README 只寫怎麼用，實測數據放在 [Wiki](https://github.com/lchanc3/modal-av1/wiki/實測數據)。
設計上的取捨寫在程式碼註解裡，放在它們發生的地方：

- `encode.py`：續跑靠檔名（開頭 docstring、`work_dir` / `enc_key`）、`.part` 再 rename
  （`_publish`）、音軌不進切段流程（`split`）、只有 driver 設 nonpreemptible（`run_job`）、
  費率是從帳單回歸出來的（`RATE_CPU_S`）、edit list 造成輸出比來源長（`merge`）
- `jobspec.py`：每段 8 核的理由（`DEFAULTS`）、Dict 的每個 key 只有一個 writer（開頭）、
  driver 存活不用 heartbeat 判斷（`driver_alive`）
