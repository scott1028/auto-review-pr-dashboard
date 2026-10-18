**繁體中文** | [English](README.en.md)

# <img src="docs/icon.png" width="40" alt="" /> auto-review-pr-dashboard

![看板娘：薄荷青與橘色的機械人專員，舉著放大鏡，身邊飄著 PR 螢幕](docs/mascot.png)

這個工具讓 AI 一個一個幫你審 GitHub 上的 PR，每個 PR 都開一個獨立的 AI process 來審。

AI 自己記不住進度，視窗一關，審到哪裡就忘了。所以這個工具在背景放了一個小管家（daemon，
一直在背景跑的 process），由它記住哪些 PR 還沒審、哪些審完、哪些審到一半。你看到的
dashboard 只是它的螢幕，關掉也沒關係，再開一個就接回來了。

## 安裝

開始之前，你需要：

- 已經登入的 `gh`，工具靠它找 PR、讀留言
- Python 3.10 以上，還有 uv（沒有 uv 也行，見下面）
- 一個 AI CLI，例如 `codex` 或 `claude`

```bash
make install
```

裝完開一個新的 shell 就能用了。`make install` 實際做了哪些事、還有哪些 make 指令可以用，
寫在 guide 的[安裝做了哪些事](docs/guide.md#安裝做了哪些事)。

沒有 uv 的話，先 `pip install .`，再用 `python3 -m auto_review_pr_dashboard` 跑。連 Python
都不想裝，可以到
[Releases](https://github.com/scott1028/auto-review-pr-dashboard/releases)
下載打包好的執行檔，`x86_64` 和 `aarch64` 都有：

- Linux 用 AppImage。系統沒有 FUSE 的話（AppImage 靠它把自己掛載起來），在命令前面加上
  `APPIMAGE_EXTRACT_AND_RUN=1`。
- macOS 用 tar.gz，解開後執行 `bin/auto-review-pr-dashboard`。

## 開始審 PR

用法只要記一句話：prompt 決定*怎麼審*，flag 決定*審哪些*。

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

範圍只看 flag，不會從 prompt 的文字去猜，所以搜尋做不到的條件，像是「有 label X 的 PR」，
寫進 prompt 也沒用。不確定範圍對不對，就先加 `--dry-run` 跑一次看看。

`--repo-url` 可以填 `owner/repo` 或任何 git url。填成 PR 的 url 的話，它會擋下來，
提醒你改用 `--pr-url`。寫在命令前面的 inline 環境變數，會一起傳給 `gh` 和 agent（實際去審
PR 的 AI process）。

啟動後，工具會替你目前的工作目錄開一個 daemon，然後接上去。之後想接回來，
就到同一個目錄單獨跑 `--resume`，原本開著的 dashboard 會自己退出，由新的接手。daemon
如果掛了，工具只會告訴你，不會自己重開。`--dry-run` 則一律在前台跑。

### prompt.md 怎麼寫

`prompt.md` 放在你執行命令的目錄，內容就是給 agent 的審查指示。第一次跑時如果沒有這個檔，
工具會幫你建一個，裡面只有 `code review` 一行。

整個檔案都會當成 prompt，命令列上的 `<prompt>` 參數會被忽略，空檔案它會直接拒絕。
工具也不會自動幫你加 skill 呼叫，想用 skill 就自己寫進去，例如：

```text
code review
/baseline-fe-code-review
```

每個 PR 送出前，工具會在 prompt 後面接一段自動產生的範圍說明，限定 agent 只審那一個 PR。
daemon 啟動時只讀一次 prompt 並存起來，之後改檔案或 `--resume` 都不會重讀；要換 prompt，
得先按 `q` 停掉，再重新啟動。

## Dashboard 上的按鍵

dashboard 有兩個畫面：列表顯示所有 PR，詳情頁顯示單一個 PR 的狀態和 log。

| Key | 在哪 | 做什麼 |
|---|---|---|
| `↑` / `↓`、滑鼠點擊 | 列表 | 選 PR；用滑鼠的話，點一下就直接打開 |
| `enter` | 列表 | 打開這個 PR 的詳情頁 |
| `c` | 列表 / 詳情頁 | 取消這個 PR，連同正在跑的 agent 和它開的子 process（整個 process group）一起砍掉 |
| `r` | 列表 / 詳情頁 | 把取消掉的 PR 重新排回這一輪，排在最後面 |
| `n` | 任何地方 | 不等了，直接跳過倒數；兩輪之間的等待和 cooldown（撞到用量上限後的等待）都適用 |
| `p` | 任何地方 | 暫停：跑到一半的 PR 會跑完，但不會再開新的；倒數也會停住 |
| `l` | 任何地方 | 打開或收起即時的 agent log 面板 |
| `d` | 任何地方 | 背景執行：關掉 dashboard，daemon 繼續跑 |
| `f` / `esc` | 詳情頁 | 切換 log follow（開著會一直接上新的 log，關掉就停住） / 回到列表 |
| `q` | 任何地方 | 停掉 daemon 並離開。每個 agent 會先收到 SIGTERM，過 `TERMINATE_GRACE_SEC`（5 秒，在 `runner.py`）後再收到 SIGKILL。還有 PR 在跑的話會先問你，預設選 No |

## Architecture: TUI → IPC → Service

程式拆成三塊，下面說明為什麼。TUI 是你在終端機看到的畫面，IPC 是兩個 process
之間傳訊息的管道（這裡用 unix socket），Service 就是真正在跑審查的 daemon。

```text
  終端機（可丟）                 背景的 daemon（工作歸它）
┌──────────────────────┐  JSON   ┌──────────────────────────────────┐
│ app.py — Textual TUI │  lines  │ session.py — SessionServer       │
│  render snapshot     │◀────────│   snapshot / log / tick / notice │
│  keypress → command  │────────▶│   cancel resume run_now pause    │
└──────────────────────┘  unix   │ runner.py — LoopRunner           │
   關掉它，什麼都不丟            │ store.py — .tmp/ artifacts       │
   --resume 接回來               └──────────────────────────────────┘
                                       └─▶ 每個跑中的 PR 一個 process group: <ai_cli>
```

- **審查要活得比終端機久。** 一個 PR 可能要審 30 分鐘，一輪 loop 可能跑好幾個小時。如果
  agent 是終端機底下的子 process，視窗一關、SSH 一斷，它就跟著死。所以 daemon 用
  `start_new_session` 開成獨立的 session，你按 `d` 或直接關掉視窗，工作都照跑。
- **審到一半砍掉是有代價的。** 被砍的 agent 可能已經貼了幾則行內評論，卻還沒貼收尾的
  marker。marker 是一則隱藏留言，像書籤一樣記下這次審到哪個 commit；少了它，
  下一輪就會重審同一個 SHA，詳情頁也會提醒你。所以要不要砍 process，
  由手上拿著佇列的那一方決定。
- **服務不該知道 UI 長怎樣。** `LoopRunner` 只把事件丟給 `on_event` callback，所以同一個
  class 可以在 daemon 裡跑，也可以在同一個 process 裡對著假的 CLI 跑。
- **兩邊只靠 socket 溝通，所以 TUI 不用記任何狀態。** 每次接上，daemon 會先送一份完整的
  snapshot（當下的所有狀態），之後狀態一變就再推一份。你按下的鍵會變成一則 `command`
  訊息送過去，不會直接呼叫 runner。
- **一個工作目錄只有一個 daemon。** 同一個目錄啟動第二次，它會直接拒絕。兩邊用
  `/tmp/auto-review-pr-dashboard-<uid>/<cwd-hash>.sock` 這個 unix socket 溝通，只在本機，
  不開 port，也沒有認證。

## 檔案存在哪裡

所有紀錄都寫在**你執行命令的那個**工作目錄底下，通常就是放了一堆 repo 的那個父目錄：

```
.tmp/auto-review-pr-dashboard/
├── daemon.json                 # 現任 daemon 中繼資料與 socket 位置
├── daemon.log                  # daemon 診斷
├── state.json                  # run 設定 + 目前 loop 序號
├── loops/loop-0003.json        # 每輪紀錄:判決、狀態、貼文 diff、時間
└── logs/loop-0003/owner__repo__7.log
```

daemon 每 `RECYCLE_EVERY_LOOPS` 輪（10 輪，常數在 `session.py`）會換班一次，
把工作交給一個新的 daemon。換班時會清空整個 `.tmp/`，連別的工具放在裡面的檔案也一起刪掉，
這樣 `loops/` 和 `daemon.log` 才不會一直長大。狀態列會用 `cleaned N×` 顯示清過幾次。

兩次換班之間，`logs/` 只留**最新的 10 個 loop 目錄**（是 10 輪，不是 10 小時）。
想多留一點，就改 `store.py` 裡的 `MAX_KEPT_LOOP_DIRS`。

## 幾件事要先知道

- `--parallel` 開多個 agent 時，它們共用同一個工作目錄；如果都用固定檔名的暫存檔，
  就會撞在一起。
- `-l` 是靠讀 `/proc` 找 daemon 的，所以只支援 Linux，也只看得到你自己這個使用者的
  daemon。dashboard 本身則需要 TTY。
- 用量上限是靠比對 agent 的錯誤訊息來判斷的，這份樣式清單只能盡量涵蓋。看到新的措辭，
  就加進 `config.py` 的 `USAGE_LIMIT_PATTERNS`；真正的安全網是「連續失敗兩次」這條規則。

## 想知道更多

細節都在 [guide](docs/guide.md)：

- [名詞對照](docs/guide.md#名詞對照)：daemon、agent、marker 這些詞是什麼意思
- [安裝做了哪些事](docs/guide.md#安裝做了哪些事)：shell function 的用途和所有 make 指令
- [每一輪 loop 做什麼](docs/guide.md#每一輪-loop-做什麼)：怎麼找 PR、怎麼決定要不要審
- [agent 撞到用量上限時](docs/guide.md#agent-撞到用量上限時)：怎麼偵測、等多久、
  什麼時候放棄
- [daemon 換班和 socket 的細節](docs/guide.md#daemon-換班和-socket-的細節)：啟動排隊、
  handshake、換班流程
- [限制和特殊情況](docs/guide.md#限制和特殊情況)：daemon 意外掛掉、log 面板的上限
- [跑測試和發新版](docs/guide.md#跑測試和發新版)：測試指令和 release 流程
