**繁體中文** | [English](README.en.md)

# <img src="docs/icon.png" width="40" alt="" /> auto-review-pr-dashboard

![看板娘:薄荷青與橘色的機械人專員,舉著放大鏡,身邊飄著 PR 螢幕](docs/mascot.png)

這個工具找 AI 一個一個幫你審 GitHub PR,每個 PR 開一個獨立的 AI process。AI 不記得審到
哪裡,視窗一關進度就不見,所以工具在背景養一個小管家(daemon,常駐背景的 process),記住
哪些 PR 還沒審、審完了、審到一半。dashboard 只是它的螢幕,關掉再開一個,進度都還在。

## 安裝

```bash
make install
```

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

沒有 uv 也行:`pip install .`,再用 `python3 -m auto_review_pr_dashboard` 跑。連 Python
都不想裝,[Releases](https://github.com/scott1028/auto-review-pr-dashboard/releases) 有
打包好的執行檔:Linux 拿 AppImage(`x86_64` / `aarch64`;沒有 FUSE 檔案系統支援時,命令前
加 `APPIMAGE_EXTRACT_AND_RUN=1`),macOS 拿 tar.gz(`aarch64` / `x86_64`,解壓後跑
`bin/auto-review-pr-dashboard`)。

## 開始審 PR

一句話:prompt 決定*怎麼審*,flag 決定*審哪些*;它照 flag 一輪一輪地找 PR、派 AI 去審。

```bash
auto-review-pr-dashboard <ai_cli> [<prompt>] [options] [-- <ai_cli args>...]
auto-review-pr-dashboard --resume
auto-review-pr-dashboard -l
  scope — 至少要有 --repo-url / --pr-url 之一:
  --repo-url OWNER/REPO|URL  要搜尋的 repo(可重複;需搭配 --author)
  --pr-url URL               只審這個 PR(可重複;不需要 --author)
  --author LOGIN             依作者過濾(可重複)
  --reviewer LOGIN           依審查者過濾(可重複)

  --interval MIN      兩輪 loop 間隔分鐘(預設:60)
  --pr-timeout MIN    一個 PR 的 AI process 跑超過這麼多分鐘就殺掉(預設:30)
  --cooldown MIN      疑似撞到用量上限後等待分鐘(預設:30)
  --parallel N        同時最多審 N 個 PR(預設:1)
  --dry-run           只探索、印判決表,不審任何 PR
  -l, --list          列出有活著 daemon 可續的工作目錄
  -- ARGS...          把 ARGS 塞給 <ai_cli>:內建 flag 之後、prompt 之前

auto-review-pr-dashboard codex --repo-url owner/repo --author <login> --dry-run
auto-review-pr-dashboard claude --pr-url https://github.com/owner/repo/pull/<n>
auto-review-pr-dashboard codex --pr-url https://github.com/owner/repo/pull/<n> -- --yolo
```

範圍永遠不會從 prompt 的文字去猜,搜尋表達不了的條件(例如「label X 的 PR」)寫進 prompt
也沒用。`--repo-url` 可以給 `owner/repo` 或任何 git url,給 PR 的 url 會被擋下並提醒你改用
`--pr-url`。命令前面的 inline 環境變數會傳給 `gh` 和 agent(實際審 PR 的 AI process)。

正常啟動會替你現在的工作目錄拉起一個脫機(detached,跟終端機脫鉤)的 daemon 再接上去。
要接回來,在同一個目錄單獨跑 `--resume`,它會取代原本開著的 dashboard。daemon 掛掉只會
回報,不會自動重啟;`--dry-run` 一律在前台跑。

### prompt.md 怎麼寫

`prompt.md` 決定 agent 怎麼審,啟動時讀取;不存在就幫你建一個,內容剛好是 `code review`。
整個檔案就是 prompt(位置參數的 prompt 會被忽略,空檔案會被拒絕),系統會在後面附上自動
生成的單個 PR 範圍。daemon 只讀一次並存起來,之後改檔案或 `--resume` 都不會讓它重讀。
它永遠不會自動幫你加 skill 呼叫,要用就自己寫進去,例如:

```text
code review
/baseline-fe-code-review
```

## Dashboard 上的按鍵

| Key | 在哪 | 做什麼 |
|---|---|---|
| `↑` / `↓`、mouse click | 列表 | 選 PR(滑鼠單擊直接打開) |
| `enter` | 列表 | 打開 PR 詳情頁 |
| `c` | 列表 / 詳情頁 | 取消這個 PR:跑中的 agent 連同它開的子 process(整個 process group)一起殺掉 |
| `r` | 列表 / 詳情頁 | 把已取消的 PR 排回這一輪佇列的最後面 |
| `n` | 任何處 | 跳過倒數,兩輪之間的空等或 cooldown 都算 |
| `p` | 任何處 | 暫停:跑中的 PR 先跑完,之後先不派新的;倒數凍結 |
| `l` | 任何處 | 顯示 / 隱藏即時的 agent log 面板 |
| `d` | 任何處 | 背景化:分離並關掉 dashboard,daemon 繼續跑 |
| `f` / `esc` | 詳情頁 | 切換 log follow(開著會持續接上新行,關掉就停住不動) / 回到列表 |
| `q` | 任何處 | 停掉 daemon 並離開(每個 agent 先收 SIGTERM,過 `TERMINATE_GRACE_SEC`——5 秒,在 `runner.py`——後 SIGKILL);有 PR 在跑會先問你,預設選 No |

## Architecture: TUI → IPC → Service

TUI 是終端機畫面,IPC 是 process 間傳訊息的管道(unix socket),Service 是跑審查的 daemon。

```text
  終端機(可丟)                    脫機 daemon(工作歸它)
┌──────────────────────┐  JSON   ┌──────────────────────────────────┐
│ app.py — Textual TUI │  lines  │ session.py — SessionServer       │
│  render snapshot     │◀────────│   snapshot / log / tick / notice │
│  keypress → command  │────────▶│   cancel resume run_now pause    │
└──────────────────────┘  unix   │ runner.py — LoopRunner           │
   關掉它,什麼都不丟             │ store.py — .tmp/ artifacts       │
   --resume 接回來               └──────────────────────────────────┘
                                       └─▶ 每個跑中的 PR 一個 process group: <ai_cli>
```

- **審查活得比你的終端機久。** 一個 PR 可能審 30 分鐘,一輪 loop 跑好幾小時。agent 若是
  終端機的子 process,視窗一關、SSH 一斷就跟著死;daemon 用 `start_new_session` 拉起,
  所以按 `d` 或直接關視窗,工作照跑。
- **審到一半被殺不是免費的。** agent 可能已貼了行內評論,卻還沒貼收尾的 marker(記下審到
  哪個 commit 的隱藏評論),下一輪就會重審同一個 SHA,詳情頁會提醒你。所以 process 的
  生死,由持有佇列的那一方決定。
- **服務不該知道 UI。** `LoopRunner` 只把事件丟給 `on_event` callback,所以同一個 class
  能在 daemon 裡跑,也能在同一個 process 裡對 mock CLI 跑。
- **socket 就是契約,所以 TUI 不存狀態。** 每次接上先收到完整 snapshot(當下狀態的快照),
  之後狀態每變一次 daemon 就推一份;按鍵是一則 `command` 訊息,不是直接呼叫 runner。
  client 收太慢也由 daemon 處理:tick 不附 snapshot,buffer 超過 1 MiB 的 client 直接斷線。
- **一個工作目錄一個 daemon**:`/tmp/auto-review-pr-dashboard-<uid>/<cwd-hash>.sock`
  (mode `0700`);旁邊的 `.lock` 讓啟動排隊,啟動、recycle、停止時都會清掉沒人用的
  `.lock`/`.sock`。只在本機、不開 port、沒有認證;handshake 核對 protocol version、run id、
  cwd;同一個工作目錄第二次啟動會被拒絕。
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
或不分措辭連續失敗兩次,後者也能抓到登入失效、CLI 壞了、網路斷了。樣式清單只是盡力而為,
新措辭請加進 `config.py` 的 `USAGE_LIMIT_PATTERNS`;連續失敗的斷路器才是真正的安全網。

那個 PR 會以 `queued` 回到佇列**最前面**,整輪 loop 停在 banner 後面等 `--cooldown` 分鐘;
已在跑的 `--parallel` agents 會先跑完,失敗也併進同一次 cooldown。已經貼過評論的 run,或
comment 快照失敗、沒人知道貼了沒的,改標 `failed`,因為重跑可能重複貼。同一輪 cooldown
滿 3 次就放棄,等 `--interval`。每次失敗都會把 agent 最後的輸出記進該 PR 的 `error` 欄位。

## 檔案存在哪裡

所有東西都寫在**你執行命令的那個**工作目錄下(通常是那個放滿 repo 的父目錄):

```
.tmp/auto-review-pr-dashboard/
├── daemon.json                 # 現任 daemon 中繼資料與 socket 位置
├── daemon.log                  # daemon 診斷
├── state.json                  # run 設定 + 目前 loop 序號
├── loops/loop-0003.json        # 每輪紀錄:判決、狀態、貼文 diff、時間
└── logs/loop-0003/owner__repo__7.log
```

每 `RECYCLE_EVERY_LOOPS` 輪(10 輪,常數在 `session.py`)換班時,整個 `.tmp/` 會刪掉,
別的工具放的檔案也一起,所以 `loops/` 和 `daemon.log` 不會無限變長;狀態列會顯示成
`cleaned N×`。其間 `logs/` 只保留**最新 10 個 loop 目錄**(10 輪,不是 10 小時);想留
更多,改 `store.py` 的 `MAX_KEPT_LOOP_DIRS`。

## 幾個容易踩的坑

- daemon 沒清乾淨就死掉,它的 agents 會繼續跑。下次啟動會照 `daemon.json` 的
  `agent_groups` 殺掉那些 process group(pid 已被重用的跳過);自己呼叫過 `setsid` 的
  子孫 process 就管不到了。
- `-l` 靠讀 `/proc` 運作:只支援 Linux、只看得到目前使用者的 daemon;dashboard 要有 TTY。
  `--parallel` 的 agents 共用工作目錄,用固定檔名的暫存檔會互相撞到。
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
