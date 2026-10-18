**繁體中文** | [English](guide.en.md)

# auto-review-pr-dashboard 使用指南

這份指南補充 [README](../README.md) 沒寫的細節。第一次用的話，看 README 就夠了；遇到問題，
或想知道某個行為背後的原因，再回來查這裡。

## 名詞對照

| 名詞 | 白話解釋 |
|---|---|
| daemon | 在背景一直跑的小管家，佇列和進度都歸它管 |
| dashboard / TUI | 終端機上的畫面，只是 daemon 的螢幕，關掉不影響工作 |
| `ai_cli` | 你要用的 AI CLI。支援 `codex`、`claude`、`pi`、`opencode`，也接受 `codex-personal` 這種後面加了 `-xxx` 的名字 |
| agent | 實際去審某一個 PR 的 AI process |
| `prompt.md` | 給 agent 的審查指示，整個檔案就是 prompt |
| 一輪（loop） | 找一次 PR、把該審的都審完，算一輪 |
| marker | agent 審完後留在 PR 上的隱藏留言，像書籤一樣記下審到哪個 commit |
| `Full` / `Incremental` / `Skip` | 這一輪對某個 PR 的判斷：還沒審過 / 審過，但之後 head 變了 / 目前的 head 已經審過 |
| cooldown | 撞到用量上限後，整輪先停下來等的那段時間 |
| 換班（recycle） | daemon 定期把工作交給一個新的 daemon |
| process group | 一個 process 和它開出來的子 process，要砍就一起砍 |

## 安裝做了哪些事

`make install` 會先跑 `uv tool install .`，把 `auto-review-pr-dashboard` 放上 `PATH`。
接著在 `~/.bashrc.d/auto-review-pr-dashboard` 放一個 symlink，裡面定義了一個同名的 shell
function，要開新的 shell 才會載入它。

為什麼需要這個 function？因為你的 `ai_cli` 可能是一個 bash function，而不是執行檔。這個
shell function 就是所謂的 wrapper，它會用 `BASH_FUNC_<name>%%` 把你選的那個 function
匯出去，daemon 開出來的 process 才叫得到它。

每個 PR 的 AI 都在 `bash -ic` 裡跑（`tests/test_config_parity.py` 會檢查這件事）。
`bash -ic` 會先載入 `~/.bashrc` 再叫 `ai_cli`，所以就算沒經過 wrapper，`~/.bashrc`
裡定義的 function 還是叫得到。兩件事要記得：

- `~/.bashrc` 裡如果也定義了同名的 function，它會蓋掉 wrapper 匯出的那份。
- 你的 shell 啟動時印出來的東西，也會出現在 PR 的 log 裡。

所有 make 指令：

| 指令 | 做什麼 |
|---|---|
| `make` | 跑 `uv sync`，只把依賴裝進本地的 `.venv` |
| `make install` | 裝 CLI 和 shell wrapper |
| `make uninstall` | 移除上面兩樣，連 daemon 的 socket 目錄一起刪。daemon 還在跑的話它會拒絕，請先在 dashboard 按 `q`，或加 `FORCE=1` 強制執行。它不會動 `.tmp/auto-review-pr-dashboard/`；symlink 也只有在還指向這個 repo 時才刪 |
| `make test` | 在 uv 環境裡跑測試 |
| `make clean` | 刪掉 `.venv`、`__pycache__` 和產生出來的 `requirements.txt` |
| `make requirements` | 選用：從 `uv.lock` 產生版本鎖死的 `requirements.txt` |

## 每一輪 loop 做什麼

每一輪都照同樣的四個步驟走：

1. **找 PR**：每個搜尋條件各呼叫一次 `gh pr list`。每個 `--author` 查一次；每個 reviewer
   則用 `review-requested:` 和 `reviewed-by:` 各查一次。結果合在一起，再去掉重複的。
   要分開查，是因為 GitHub 會把同一個 `--search` 裡的條件全部 AND 起來。
   兩種範圍都有給的話，符合任一邊的 PR 都會留下來。
2. **判斷要不要審**：Draft，或標題有 `[WIP]` / `WIP:` 的 PR，直接判成 `Skip (WIP)`。
   其他的用 `gh pr view --json headRefOid,comments` 找出最新一則
   `<!-- review-pr-branches: head=<sha> -->` marker：沒有 marker 就是 `Full`，marker 的
   SHA 跟現在的 head 不同就是 `Incremental`，相同就是 `Skip`。`Skip` 的 PR 一樣看得到，
   只是不會派 agent。
3. **審**：剩下的 PR 每個派一個 agent，同時最多 `--parallel` 個，審查指示來自
   `prompt.md`。
4. **記下貼了什麼**：每次跑之前和之後，各記一次 PR 上所有留言的 id，
   兩邊一比就知道新貼了什麼。這是以 GitHub 上的資料為準，不去解析 agent 的輸出。只有帶
   `review-pr-branches` marker 的留言才算數，所以隊友在中途貼的留言不會算進來。

## agent 撞到用量上限時

agent 撞到用量上限之後，每個 PR 都會很快失敗。不停下來的話，剩下的佇列幾秒內就燒光了，
所以工具會偵測這種情況，讓整輪先停下來等。

偵測只看**失敗**的 run，所以正常的審查不會誤觸。符合下面任一條就算撞到：

- 失敗輸出的最後一段對得上 quota 的樣式，例如 `usage limit`、`429`、`quota`。
- 不管錯誤訊息寫什麼，連續失敗兩次。這條也能抓到登入過期、CLI 壞掉、網路斷線這類問題。

撞到之後，那個 PR 會以 `queued` 狀態回到佇列**最前面**，整輪 loop 停在一個 banner 後面，等
`--cooldown` 分鐘。已經在跑的 `--parallel` agents 會先跑完，它們如果也失敗，就算在同一次
cooldown 裡。幾種特殊情況：

- 已經貼過留言的 run，或是留言快照失敗、沒人知道到底貼了沒有的 run，會改標成 `failed`，
  不會重排，因為重跑可能會重複貼。
- 同一輪 cooldown 滿 3 次，這一輪就放棄，等 `--interval` 之後的下一輪再來。
- 每次失敗，agent 最後的輸出都會記在那個 PR 的 `error` 欄位，方便你事後查。

## daemon 換班和 socket 的細節

socket 所在的目錄權限是 `0700`。旁邊有一個 `.lock`，讓同時發生的啟動排隊；每次啟動、
換班（recycle）、停止時，都會清掉沒人在用的 `.lock`/`.sock`。

TUI 接上時要先握手確認（handshake），daemon 會核對 protocol version、run id、cwd，
三個都對才接受。client 收得太慢也是 daemon 負責處理：每秒一次的 tick 不附 snapshot，buffer
積超過 1 MiB 的 client，daemon 會直接斷掉它。

換班時，`run_forever` 會返回。舊 daemon 先拿住 `.lock`，清空 `.tmp/`，再把 loop 狀態從
stdin 交給接班的新 daemon。TUI 會等 `daemon.json` 出現新的 `run_id`，再自動接上去。

## 限制和特殊情況

- daemon 如果沒有正常結束就死掉，它開的 agents 會繼續跑。下次啟動時，工具會照
  `daemon.json` 裡的 `agent_groups` 砍掉那些 process group，pid 已經被別的 process
  重用的會跳過。如果某個子孫 process 自己呼叫過 `setsid`，就管不到它了。
- log 面板只是記憶體裡的顯示，有上限，完整內容在 `.log` 檔裡。超過 2000 字元的行只顯示前
  2000 個字元，後面接 `(+N chars)`。
- 面板最多留最後 2000 行，而且只有正在跑的 PR 會一直 buffer。`.log` 檔裡如果有超過 64KiB
  都沒換行的輸出，會硬切成好幾行。

## 跑測試和發新版

```bash
make test                               # 145 個測試,不連網
python3 -m unittest discover -s tests   # 同上,不用 uv
```

要發新版，改 `pyproject.toml` 的 `[project].version`，然後 push 到 `master`。Release
workflow 會拿它跟已經發布過的版本比，只有比較高才會發布，流程是：跑測試 → 打包 Linux
AppImage（x86_64/aarch64）和 macOS bundle（aarch64/x86_64）→ 發 GitHub Release。

想手動發的話，到 Actions → Release → Run workflow，這樣不會檢查版本。tag 的格式是
`v<version>-<commit 前 8 字>`。改版本時記得跑 `uv lock`，並把 `uv.lock` 一起 commit。
