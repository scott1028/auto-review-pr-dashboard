**繁體中文** | [English](guide.en.md)

# auto-review-pr-dashboard 指南

[回到 README](../README.md)

## 安裝細節

`make install` 會跑 `uv tool install .`,把 `auto-review-pr-dashboard` 放上 `PATH`;再在
`~/.bashrc.d/auto-review-pr-dashboard` 留一個 symlink,定義同名的 shell function,用
`BASH_FUNC_<name>%%` 把你選的 AI function 匯出去。這是因為你的 `ai_cli`(要用的 AI CLI,
例如 `codex`)可能是 bash function,不是執行檔。裝完請開新 shell。

每個 PR 的 AI 都跑在 `bash -ic` 裡(`tests/test_config_parity.py` 會檢查),先載入
`~/.bashrc` 再叫 `ai_cli`,所以繞過 wrapper 時 `~/.bashrc` 裡的 function 一樣能用。要記得:
`~/.bashrc` 若自己定義了同名的,那份會蓋掉匯出的;shell 啟動時印的東西也會進 PR log。

| Target | 做什麼 |
|---|---|
| `make` | `uv sync`:只把依賴裝進本地的 `.venv` |
| `make install` | 裝 CLI 和 shell wrapper |
| `make uninstall` | 移除上面兩個,連 daemon 的 socket 目錄一起刪。daemon 還活著會拒絕執行(先在 dashboard 按 `q`,或加 `FORCE=1` 強制);不動 `.tmp/auto-review-pr-dashboard/`;symlink 只在還指向這個 repo 時才刪 |
| `make test` | 在 uv 環境跑測試 |
| `make clean` | 刪掉 `.venv`、`__pycache__` 和產生出來的 `requirements.txt` |
| `make requirements` | 選用:從 `uv.lock` 產生版本鎖死的 `requirements.txt` |

## daemon 和 socket 的細節

- client 收太慢也由 daemon 處理:tick 不附 snapshot,buffer 超過 1 MiB 的 client 直接斷線。
- (mode `0700`);旁邊的 `.lock` 讓啟動排隊,啟動、recycle、停止時都會清掉沒人用的
  `.lock`/`.sock`。handshake 核對 protocol version、run id、 cwd;
- **長跑會自己換班(recycle)。** `run_forever` 在換班時返回;舊 daemon 拿住 `.lock`、清空
  `.tmp/`,把 loop 狀態從 stdin 交給接班人;TUI 等 `daemon.json` 出現新 `run_id` 再接回。

## 每一輪 loop 做什麼

1. **探索** — 每個範圍軸(一個篩選條件)各呼叫一次 `gh pr list`:每個 `--author` 一次;
   每個 reviewer 各一次 `review-requested:` 和 `reviewed-by:`,取聯集再去重。GitHub 會把
   同一個 `--search` 裡的條件 AND 起來,所以要分開呼叫;兩種範圍都給時,符合任一邊都留下。
2. **判決** — Draft 或標題有 `[WIP]` / `WIP:`,判 `Skip (WIP)`。其他的用 `gh pr view
   --json headRefOid,comments` 找最新的 `<!-- review-pr-branches: head=<sha> -->` marker:
   沒有就 `Full`,SHA 不同就 `Incremental`,相同就 `Skip`;`Skip` 的 PR 看得到但不派 agent。
3. **審查** — 剩下的每個 PR 派一個 agent,同時最多 `--parallel` 個,指令來自 `prompt.md`。
4. **Diff** — 每次跑之前、之後各記一次 comment id;「到底貼了什麼」以 GitHub 為準,
   不去解析 agent 的輸出。只有帶這個 skill marker 的評論才算,隊友中途貼的不記帳。

## agent 撞到用量上限時

撞到用量上限後每個 PR 都會很快失敗,不停下來,剩下的佇列幾秒內就燒光。偵測只看**失敗**的
run,正常審查不會誤觸:失敗輸出的尾端對上 quota 樣式(`usage limit`、`429`、`quota`,…),
或不分措辭連續失敗兩次,後者也能抓到登入失效、CLI 壞了、網路斷了。

那個 PR 會以 `queued` 回到佇列**最前面**,整輪 loop 停在 banner 後面等 `--cooldown` 分鐘;
已在跑的 `--parallel` agents 會先跑完,失敗也併進同一次 cooldown。已經貼過評論的 run,或
comment 快照失敗、沒人知道貼了沒的,改標 `failed`,因為重跑可能重複貼。同一輪 cooldown
滿 3 次就放棄,等 `--interval`。每次失敗都會把 agent 最後的輸出記進該 PR 的 `error` 欄位。

## 其他限制

- daemon 沒清乾淨就死掉,它的 agents 會繼續跑。下次啟動會照 `daemon.json` 的
  `agent_groups` 殺掉那些 process group(pid 已被重用的跳過);自己呼叫過 `setsid` 的
  子孫 process 就管不到了。
- log 面板只是記憶體有上限的顯示,完整內容在 `.log`:超過 2000 字元的行只顯示前 2000 字元、
  標 `(+N chars)`;面板最多留最後 2000 行,只有跑中的 PR 持續 buffer;檔案裡超過 64KiB
  沒換行的輸出會被硬折。

## 跑測試、發新版

```bash
make test                               # 145 個測試,不連網
python3 -m unittest discover -s tests   # 同上,不用 uv
```

要發新版:改 `pyproject.toml` 的 `[project].version` 並 push 到 `master`;Release workflow
只在版本高於已發布的時才發布:跑測試 → 蓋 Linux AppImage(x86_64/aarch64)與 macOS bundle
(aarch64/x86_64)→ 發 GitHub Release。手動發:Actions → Release → Run workflow,不檢查版本;
tag 是 `v<version>-<commit 前 8 字>`。改版本記得 `uv lock` 並 commit `uv.lock`。
