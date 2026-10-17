**繁體中文** | [English](README.en.md)

# <img src="docs/icon.png" width="40" alt="" /> auto-review-pr-dashboard

![看板娘:薄荷青橘色機械人專員在控制台前舉著放大鏡](docs/mascot.png)

一個 PR 審查迴圈,佇列歸 dashboard 管、不歸 AI 管:它用 `gh` 找出 PR,每個 PR 開
**一個 AI process** 去審,所以有進度、有每個 PR 的 log,能取消也能續審;要*怎麼*審,
永遠由你的 `prompt.md` 決定。把它想成櫃檯:跑腿的 AI 不見了沒關係,櫃檯(daemon)記得
「還有哪些 PR、審到哪」,你隨時開個新視窗接回來,像根本沒離開過。

## Install

```bash
make install
```

`uv tool install .` 把 `auto-review-pr-dashboard` 執行檔放上 `PATH`,並在
`~/.bashrc.d/auto-review-pr-dashboard` 留一個 symlink 定義同名 shell function,
它用 `BASH_FUNC_<name>%%` 把你選的 AI function 匯出去;裝完請開新 shell。每個 PR 跑在
`bash -ic`(`tests/test_config_parity.py` 會斷言),它先載入 `~/.bashrc` 再叫 `ai_cli`,
所以 wrapper 被繞過時 `~/.bashrc` 裡的 function 還是用得到——除非 `~/.bashrc` 自己
定義了同名,那會蓋掉匯出的那份。shell 啟動時印的東西會進 PR log。沒有 uv 也行:
`pip install .` 加 `python3 -m auto_review_pr_dashboard`。

| Target | Action |
|---|---|
| `make` | `uv sync` — 只裝依賴到本地 `.venv` |
| `make install` | CLI 加 shell wrapper |
| `make uninstall` | 兩個都移除,連同 daemon socket 目錄 |
| `make test` | 在 uv 環境跑測試 |
| `make clean` | 移除 `.venv`、`__pycache__` 與產生的 `requirements.txt` |
| `make requirements` | 選裝:從 `uv.lock` 產生 pin 好的 `requirements.txt` |

`make uninstall` 在 daemon 還活著時會拒絕(先在 dashboard 按 `q`,或 `FORCE=1`),
保留 `.tmp/auto-review-pr-dashboard/`,且只有 symlink 還指向這裡時才移除它。

[Releases](https://github.com/scott1028/auto-review-pr-dashboard/releases) 有預先打包的
執行檔:Linux AppImage(`x86_64` / `aarch64`,內含 Python、免 uv;無 FUSE 時命令前加
`APPIMAGE_EXTRACT_AND_RUN=1`)與 macOS tar.gz(`aarch64` / `x86_64`,解壓後跑
`bin/auto-review-pr-dashboard`)。

## Usage

```bash
auto-review-pr-dashboard <ai_cli> [<prompt>] [options] [-- <ai_cli args>...]
auto-review-pr-dashboard --resume
auto-review-pr-dashboard -l
  scope — 至少要有 --repo-url / --pr-url 之一:
  --repo-url OWNER/REPO|URL  要搜尋的 repo(可重複;需搭配 --author)
  --pr-url URL               只審這個 PR(可重複)
  --author LOGIN             依作者過濾(可重複)
  --reviewer LOGIN           依審查者過濾(可重複)

  --interval MIN      兩輪 loop 間隔分鐘(預設:60)
  --pr-timeout MIN    一個 PR 的 AI process 超過此時數就殺掉(預設:30)
  --cooldown MIN      疑似撞到用量上限後等待分鐘(預設:30)
  --parallel N        同時最多審 N 個 PR(預設:1)
  --dry-run           只探索、印判決表,不審任何 PR
  -l, --list          列出有活著 daemon 可續的工作目錄
  -- ARGS...          把 ARGS 塞給 <ai_cli>:內建 flag 之後、prompt 之前

auto-review-pr-dashboard codex --repo-url owner/repo --author <login> --dry-run
auto-review-pr-dashboard claude --pr-url https://github.com/owner/repo/pull/<n>
auto-review-pr-dashboard codex --pr-url https://github.com/owner/repo/pull/<n> -- --yolo
```

prompt 決定*怎麼審*,flag 決定*審哪些 PR*——範圍永遠不從 prompt 文字推,搜不到的
條件(「label X 的 PR」)寫在 prompt 裡沒用。`--repo-url` 接受 `owner/repo` 或任何
git url;餵 PR url 會被擋下並提醒你改用 `--pr-url`;命令前的 inline 環境變數會送到
`gh` 和 agent。正常啟動會為「呼叫者的工作目錄」拉起一個脫機 daemon 並接上去
(見 [Architecture](#architecture-tui--ipc--service));`--resume` 要從同一個目錄下、
要單獨用,會取代之前的 dashboard;daemon 掛掉只會回報、不會自動重啟;`--dry-run`
一律只在前台。

### Prompt 怎麼選

`prompt.md` 在啟動時讀取;不存在就建立,內容剛好是 `code review`。整個檔案就是
prompt(位置參數的 prompt 會被忽略,空的會被拒),而且永遠不會自動加 skill 呼叫——要加自己加:

```text
code review
/baseline-fe-code-review
1. Focus on finding bugs.
```

系統會附加自動生成的單 PR 範圍;daemon 只讀一次並存檔,改檔案或 `--resume` 都不影響這一輪。

## Keys

| Key | Where | Action |
|---|---|---|
| `↑` / `↓`、mouse click | list | 選 PR(單擊直接打開) |
| `enter` | list | 開 PR 詳情頁 |
| `c` | list / detail | 取消這個 PR——跑中的 agent 整個 process group 被殺 |
| `r` | list / detail | 把已取消的 PR 排回這一輪佇列的尾端 |
| `n` | 任何處 | 跳過任何倒數——空等或 cooldown 都算 |
| `p` | 任何處 | 暫停(讓跑中的 PR 先跑完,然後按住;倒數凍結) |
| `l` | 任何處 | 顯示 / 隱藏即時 agent log 面板 |
| `d` | 任何處 | 背景化——分離並關閉 dashboard,daemon 繼續跑 |
| `f` / `esc` | detail | 切 log follow / 返回列表 |
| `q` | 任何處 | 停 daemon 並離開(每個 agent 先 SIGTERM,`TERMINATE_GRACE_SEC`——5 秒,在 `runner.py`——後 SIGKILL);有 PR 在跑會先問,預設選 No |

## Architecture: TUI → IPC → Service

```text
  終端機(可丟)                    脫機 daemon(工作歸它)
┌──────────────────────┐  JSON   ┌──────────────────────────────────┐
│ app.py — Textual TUI │  lines  │ session.py — SessionServer       │
│  render snapshot     │◀────────│   snapshot / log / tick / notice │
│  keypress → command  │────────▶│   cancel resume run_now pause    │
└──────────────────────┘  unix   │ runner.py — LoopRunner           │
    關掉它,什麼都不丟             │   queue, gh, one agent per PR    │
    --resume 接回來               │ store.py — .tmp/ artifacts       │
                                  └──────────────────────────────────┘
                                        └─▶ 每個跑中的 PR 一個 process group: <ai_cli>
```

- **審查活得比終端機久。** 一個 PR 可能審 30 分鐘,一輪 loop 好幾小時;若 agent 是
  終端機的小孩,視窗一關、SSH 一斷就一起死。daemon 用 `start_new_session` 拉起,
  按 `d` 或關視窗,工作照跑。
- **審到一半被殺不是免費的。** agent 中途被殺時可能已貼了行內評論但還沒貼收尾
  marker,下一輪就會重審那個 SHA——詳情頁會告訴你。process 的命,歸持有佇列的那方。
- **服務不該知道 UI。** `LoopRunner` 只把事件推給 `on_event` callback,
  所以同一個 class 能在 daemon 下跑、也能對 mock CLI 在 process 內跑。
- **socket 就是契約,所以 TUI 保持無狀態。** 每次接上先收完整 snapshot,daemon
  在每次狀態變動推一份,keypress 是一則 `command` 訊息、不是直接呼叫 runner。
  反壓也在 daemon 端:tick 不推 snapshot,buffer 超過 1 MiB 的 client 直接斷掉。
- **一個工作目錄一個 daemon,走 unix socket**,在
  `/tmp/auto-review-pr-dashboard-<uid>/<cwd-hash>.sock`(mode `0700`,旁邊 `.lock` 排隊
  啟動;每次啟動、recycle、停止都清掉沒人在服的 `.lock`/`.sock` 對)——只在本機、不開
  port、沒認證,handshake 查 protocol version、run id 和 cwd;同一範圍第二次啟動被拒。
- **長跑會自己換血。** `run_forever` 在 recycle 時返回,daemon 接住 `.lock`、
  清空 `.tmp/`、把 loop 狀態從 stdin 交給接班人;TUI 等 `daemon.json` 裡的
  新 `run_id` 出現再接回去,畫面不丟。

## 每一輪 loop 做什麼

1. **探索** — 每個範圍軸各一次 `gh pr list`(每個 `--author` 一次、每個 reviewer 各
   `review-requested:` 與 `reviewed-by:` 一次),聯集後去重;GitHub 在同一個 `--search` 裡
   會 AND 故要分開呼叫。兩範圍都給時,聯集保留「各中其一」的 PR;`--pr-url` 免 `--author`。
2. **判決** — Draft 或標題 `[WIP]` / `WIP:` 判 `Skip (WIP)`;否則用 `gh pr view
   --json headRefOid,comments` 和最新一則 `<!-- review-pr-branches: head=<sha> -->` marker
   決定 `Full` / `Incremental` / `Skip`;`Skip` 還是看得見但不開 agent,只影響顯示與佇列。
3. **審查** — 剩下的每個 PR 一個 agent,同時最多 `--parallel` 個,指令來自 `prompt.md`。
4. **Diff** — 每次跑前跑後各快照一次 comment id,「貼了什麼」以 GitHub 為準、
   不解析 agent 輸出;只有帶這個 skill marker 的評論才算,隊友中途貼的不記帳。

## 當 agent 撞到用量上限

偵測需要一次**失敗**的 run,正常審查不會誤觸:失敗輸出的尾端對上 quota 樣式
(`usage limit`、`429`、`quota`,…),或不分措辭連續兩次失敗——後者也會抓到登入失效、
CLI 壞了、網路斷了。該 PR 會以 `queued` 回到佇列**最前**,整輪 loop 停在 banner 後面
等 `--cooldown` 分鐘,按 `n` 可跳過,免得第一個失敗幾秒後就把剩下的佇列燒光;跑中的
`--parallel` agents 會跑完,它們的失敗也併進這個區塊。已經貼過評論的 run、或 comment
快照失敗沒人知道的,會改標 `failed`,因為重跑可能重複貼;cooldown 滿 3 次這輪就放棄、
等 `--interval`。每次失敗都會把 agent 最後的輸出記進該 PR 的 `error` 欄位。

## Artifacts

寫在**呼叫者**的工作目錄下(通常是那個多 repo 的父目錄):

```
.tmp/auto-review-pr-dashboard/
├── daemon.json                 # 現任 daemon 中繼資料與 socket 位置
├── daemon.log                  # daemon 診斷
├── state.json                  # run 設定 + 目前 loop 序號
├── loops/loop-0003.json        # 每輪紀錄:判決、狀態、貼文 diff、時間
└── logs/loop-0003/owner__repo__7.log
```

每 `RECYCLE_EVERY_LOOPS` 輪(10,在 `session.py`)整個 `.tmp/` 會刪掉——別的工具放的
檔案也一起——所以 `loops/` 與 `daemon.log` 不會無限長;狀態列把這些清除計數顯示成
`cleaned N×`。其間 `logs/` 保留**最新 10 個 loop 目錄**(10 輪,不是 10 小時);要留
更多改 `store.py` 的 `MAX_KEPT_LOOP_DIRS`。

## Caveats

- daemon 沒清乾淨就死,會留下 agents 繼續跑;下次啟動會從 `daemon.json` 的
  `agent_groups` 殺掉那些 process group(pid 已被複用的跳過);自己呼叫過 `setsid`
  的後代就管不到了。
- `-l` 讀 `/proc`:只有 Linux、只有目前使用者;要有 TTY。`--parallel` 的 agents
  共用工作目錄,放固定檔名的暫存檔會互相撞到。
- log 面板是記憶體有上限的顯示,不是 `.log` 的忠實拷貝:超過 2000 字元的行顯示
  `(+N chars)`、只留最後 2000 個字元,只有跑中的 PR 持續 buffer,檔案裡超過
  64KiB 沒換行的輸出會被硬折。
- 用量上限的 pattern 清單盡力而為;新措辭請加進 `config.py` 的
  `USAGE_LIMIT_PATTERNS`——背後的連續失敗斷路器才是安全網。

## 開發與發布

```bash
make test                               # 145 個測試,不連網
python3 -m unittest discover -s tests   # 同上,不用 uv
```

改 `pyproject.toml` 的 `[project].version` 並 push 到 `master`,Release workflow 會比對
已發布版本、只在更高時發布:跑測試 → 蓋 Linux AppImage(x86_64/aarch64)與 macOS
bundle(aarch64/x86_64)→ 發 GitHub Release。手動:Actions → Release → Run workflow 可發
任何版本;tag 是 `v<version>-<commit 前 8 字>`。改版本記得 `uv lock` 並 commit `uv.lock`。
