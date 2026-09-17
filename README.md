# 張震 股市盤中家教班：每日直播逐字稿自動化

每個平日台灣時間 11:20 起，GitHub Actions 會每 3 分鐘檢查 [@xinchenginsta 的直播](https://www.youtube.com/@xinchenginsta/streams)。
當天那集結束、YouTube 回放處理好之後，會用 Gemini API 聽打逐字稿，寫進你的 Google 試算表（可以下載成 Excel）。

**全部在網頁上完成**：程式跑在 GitHub，金鑰放在 GitHub Secrets，Google 的設定在瀏覽器裡的 Cloud Shell 做。不需要在自己電腦安裝任何東西。

```
GitHub Actions 排程（週一～五 11:10 啟動 → 程式等到 11:20，每 3 分鐘檢查到 14:00；另有備援與額度重置後補跑）
   │
   ├─ YouTube Data API：今天的直播結束了嗎？ ── 還沒 → 結束，5 分鐘後再查
   ├─ Google 試算表：今天這集寫過了嗎？      ── 寫過 → 結束
   ├─ Gemini API（gemini-3.8-flash，額度用完改用 gemini-3.5-flash-lite）：直接讀 YouTube 網址，每 30 分鐘切一段聽打
   └─ 寫入試算表「逐字稿」＋「執行紀錄」
```

- **第一次**（試算表還沒有資料）：自動抓頻道最新 5 場已結束的直播，由舊到新寫入。
- **之後每個平日**：只抓當天那一集。抓到之後，當天剩下的排程只會檢查一下就結束。

---

## ⚠️ 開始前請先了解

1. **Public 或 Private repo 的差別**：
   - **Public**：任何人都看得到程式碼和 Actions 的 log，Actions 分鐘數不限。但如果連續 60 天沒有任何活動（例如 commit），GitHub 會自動停用排程，停用後到 Actions 頁面按「Enable workflow」就能恢復。
   - **Private**：只有你看得到，也沒有 60 天規則，但 Actions 每月只有 2,000 分鐘免費額度（本專案每月約用 700～1,000 分鐘）。
   - 不論哪一種，程式都**不會**把金鑰或逐字稿內容印到 log，服務帳戶 email 會自動遮蔽成 `***`；逐字稿只存在你自己的試算表。
2. **排程時間不一定準**：GitHub 排程在尖峰時段可能延遲或跳過某一次，詳見下方「日常運作」。
3. **GitHub 使用條款**：GitHub 的條款寫明，GitHub 主機上的 Actions 應該用在「軟體專案的開發、測試、部署」相關工作。
   每天定時轉錄影片不完全符合這個用途，GitHub 有權限制或停用。
   本專案**刻意不加**「防止排程被停用」之類的繞過機制，這類工具的 repo 已經被 GitHub 官方封鎖。
   如果需要長期穩定運作，建議之後改用 Google Cloud Run Job + Cloud Scheduler。
4. **請用個人 Gmail 帳號**做 Google 的設定。學校帳號（Google Workspace）常被管理員禁止建立服務帳戶金鑰或使用 AI Studio。

## 頻道格式（2026-09-17 實測）

| 項目 | 內容 |
|---|---|
| 頻道 ID | `UCPqyYS3n6yyXL2jygauXpzg` |
| 標題格式 | `2026/09/17(四)張震  股市盤中家教班`（程式從標題解析日期） |
| 直播時間 | 平日約 10:00 開播，11:01～11:19 結束，每集 57～70 分鐘 |
| 回放上架 | 直播結束後約 3～5 分鐘 |
| 影片保留 | 頻道只留最近 5～6 部，舊的會被刪掉，所以要每天抓 |
| YouTube 字幕 | **沒有**（連自動字幕都沒有） |

**為什麼不用 NotebookLM？** NotebookLM 沒有一般帳號能用的 API。它匯入 YouTube 時靠的是影片字幕，而這個頻道的影片沒有字幕，所以拿不到逐字稿。
Gemini API 可以直接讀取公開的 YouTube 網址，自己聽聲音來聽打，不需要字幕，也不用下載影片。

---

## 部署步驟

程式碼已經在這個 repo 裡了，你只需要完成下面的設定。

### 步驟 1：取得 Gemini API 金鑰（AI Studio）

1. 用個人 Gmail 登入 <https://aistudio.google.com/apikey>
2. 按「建立 API 金鑰（Create API key）」。第一次使用時，系統會自動幫你建立一個專案
3. 複製金鑰並先存在記事本，它就是等一下要填的 **`GEMINI_API_KEY`**

### 步驟 2：在 Cloud Shell 建立 YouTube 金鑰與服務帳戶

1. 用**同一個 Gmail** 開啟 <https://shell.cloud.google.com/>，等終端機出現。第一次使用要按「授權」
2. 把下面**整段**複製、貼到終端機，按 Enter。它會先把腳本存成 `setup.sh` 再執行，約 1～2 分鐘，**只要執行一次**：

```bash
cat > setup.sh <<'SCRIPT'
set -euo pipefail
export CLOUDSDK_CORE_DISABLE_PROMPTS=1                # gcloud 不詢問、直接用預設選項
PROJECT_ID="yt-transcript-${RANDOM}${RANDOM}"        # 專案 ID 必須全球唯一，所以加上隨機數字
SA_EMAIL="sheet-writer@${PROJECT_ID}.iam.gserviceaccount.com"

echo "▶ 建立專案 ${PROJECT_ID}"
gcloud projects create "$PROJECT_ID" --name="YouTube Transcript"
gcloud config set project "$PROJECT_ID"

echo "▶ 啟用 YouTube Data API、Google Sheets API、API Keys API"
gcloud services enable youtube.googleapis.com sheets.googleapis.com apikeys.googleapis.com

echo "▶ 建立只能呼叫 YouTube Data API 的金鑰"
gcloud services api-keys create --key-id=youtube-data --display-name="YouTube Data API" \
  --api-target=service=youtube.googleapis.com

echo "▶ 建立服務帳戶（不給任何專案權限，只用來寫入你共用給它的試算表）"
gcloud iam service-accounts create sheet-writer --display-name="Sheet writer"
for i in 1 2 3 4 5 6; do                            # 新服務帳戶要幾秒才生效，失敗就重試
  gcloud iam service-accounts keys create key.json --iam-account="$SA_EMAIL" && break
  echo "  服務帳戶尚未就緒，10 秒後重試…"; sleep 10
done
test -s key.json

echo
echo "================ ① YOUTUBE_API_KEY ================"
gcloud services api-keys get-key-string youtube-data --format="value(keyString)"
echo
echo "================ ② 服務帳戶 email（步驟 3 要共用給它）================"
echo "$SA_EMAIL"
echo
echo "================ ③ GOOGLE_SERVICE_ACCOUNT_JSON（一整行很長）================"
base64 -w0 key.json; echo
rm -f key.json                                      # 金鑰只留在 GitHub Secrets，這裡立即刪除
SCRIPT
bash setup.sh
```

3. 把輸出的 ①②③ 分別複製到記事本。
   - ③ 很長，請從頭拖曳到尾完整選取。就算複製時被斷成好幾行也沒關係，程式會自動忽略換行和空白。
   - 金鑰都要保密，不要貼到 issue、commit 或聊天群組。

> 如果出現錯誤，把錯誤訊息記下來，常見原因在最下面的「常見問題」。
> 腳本中途失敗時，可以到 <https://console.cloud.google.com/cloud-resource-manager> 刪掉建到一半的專案，再重新執行一次。

### 步驟 3：建立 Google 試算表並共用給服務帳戶

1. 開啟 <https://sheets.new>，把試算表命名為「張震逐字稿」
2. 右上角「共用」→ 貼上步驟 2 的 **② 服務帳戶 email** → 權限選「**編輯者**」→ 取消勾選「通知使用者」→「共用」
3. 從網址複製試算表 ID：
   `https://docs.google.com/spreadsheets/d/`**`這一段就是ID`**`/edit`
   這就是 **`SPREADSHEET_ID`**

程式第一次執行時，會自動建立「逐字稿」和「執行紀錄」兩個工作表。原本的「工作表1」可以刪掉。

### 步驟 4：在 GitHub 設定 4 個 Secrets

開啟 <https://github.com/Lee200202/Notebooklm-Scraper-Test/settings/secrets/actions>，
按「New repository secret」，逐一新增（Name 要完全一樣）：

| Name | Secret |
|---|---|
| `GEMINI_API_KEY` | 步驟 1 的金鑰 |
| `YOUTUBE_API_KEY` | 步驟 2 的 ① |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | 步驟 2 的 ③ |
| `SPREADSHEET_ID` | 步驟 3 的試算表 ID |

### 步驟 4-2：設定寄信通知（選填，建議）

設定後的寄信時機：
- **轉錄成功**：寫入試算表後立即寄一封。
- **轉錄失敗**：不會每次失敗都寄，因為排程一天內會自動重試很多次。每天 **17:05** 如果今天這集還沒完成，才寄一封失敗報告，附上當天的錯誤紀錄和補抓方式。

設定方式（寄件帳號建議用**個人 Gmail**，學校／公司帳號不能建立應用程式密碼）：

1. 寄件的 Gmail 帳號要先開啟「兩步驟驗證」：<https://myaccount.google.com/signinoptions/two-step-verification>
2. 開啟 <https://myaccount.google.com/apppasswords>，名稱輸入 `transcript-bot`，按「建立」，複製 16 碼密碼
3. 在 GitHub Secrets 新增：

| Name | Secret |
|---|---|
| `MAIL_USERNAME` | 寄件的完整 Gmail 地址，例如 `yourname@gmail.com` |
| `MAIL_APP_PASSWORD` | 上一步的 16 碼應用程式密碼，有沒有空格都可以 |
| `MAIL_TO` | （選填）收件者，多位用半形逗號分隔；不設定就寄給 `MAIL_USERNAME` 自己 |

> 應用程式密碼可以登入該 Gmail 信箱，請務必只存在 GitHub Secrets。
> 你有多個帳號的話，建議用一個**專門寄通知的 Gmail** 當寄件帳號，收件者再填平常用的信箱。
> 不想再用時，到上面的應用程式密碼頁面刪除這組密碼即可。

### 步驟 5：執行「檢查設定」

1. 開啟 <https://github.com/Lee200202/Notebooklm-Scraper-Test/actions/workflows/daily-transcript.yml>
2. 右側「Run workflow」→ mode 選 **`check`** → 按綠色的「Run workflow」
3. 重新整理頁面，點進最新一筆執行 →「transcript」→ 展開「執行」步驟，應該看到：

```
✅ 4 個 GitHub Secrets 都已設定
✅ YouTube Data API 正常，找到 5 場直播（現在台灣時間 2026-09-17 19:00）：
     2026-09-17  r5YtdmBoMEA   66 分鐘  可轉錄  2026/09/17(四)張震  股市盤中家教班
     ...
✅ Google 試算表可讀寫，目前已有 0 集逐字稿（「執行紀錄」已新增一列檢查紀錄）
✅ Gemini API 金鑰有效，主要模型 gemini-3.8-flash（Gemini 3.8 Flash），備援模型 gemini-3.5-flash-lite（…）
✅ Gmail 寄信正常，已寄出測試信給 1 位收件者，請到信箱確認

全部檢查通過，可以開始轉錄。
```

`check` 不會轉錄，也不會用掉 Gemini 的生成額度。有設定寄信時，每次 `check` 都會寄一封「✅ 設定檢查：寄信測試」。
沒設定寄信時，這一行會顯示「ℹ️ 未設定寄信（選填）」，不算錯誤。出現 ❌ 時依訊息修正，再執行一次。

### 步驟 6：先試轉一集

同一個頁面再按「Run workflow」，在 **video_id** 填上檢查結果列出的最新一集，例如 `r5YtdmBoMEA`，再執行。
通常要幾分鐘到十幾分鐘，完成後打開試算表看逐字稿品質。這一步大約用掉 3 次 Gemini 請求。

### 步驟 7：補抓最新 5 集

「Run workflow」→ mode 選 **`init`**，video_id 留空 → 執行。
已經寫入的集數會自動略過，每集依序處理，會花比較久。剩下 4 集約用 12 次 Gemini 請求。

**完成！** 之後每個平日 11:20 起會自動執行，不用再管。

---

## 日常運作

| 時間（台灣） | 行為 |
|---|---|
| 平日 11:10 | 主要排程啟動（提早 10 分鐘，預留 GitHub 排程延遲），程式先等到 11:20 |
| 11:20 起每 3 分鐘 | 在**同一次執行**中檢查今天的直播：還在直播、剛結束或 YouTube 還在處理 → 3 分鐘後再查，最晚到 14:00 |
| 直播結束滿 5 分鐘後的那次檢查 | 開始轉錄並寫入試算表，**立即寄成功通知信**，這次執行結束 |
| 平日 11:30、11:50、12:10～13:50 每 20 分鐘 | 備援排程：萬一 11:10 那次被 GitHub 延遲或略過就接手；今天那集已完成則立刻結束 |
| 平日 15:10、15:40、16:10、16:40 | Gemini 每日額度重置後再檢查一次；今天那集已完成就直接結束 |
| 轉錄失敗 | 記錄到「執行紀錄」，並在 Actions 頁面標註錯誤，但**不會馬上寄信**；之後的排程自動重試，同一集一天最多失敗 3 次（手動執行不受限） |
| 平日 17:05 | **每日報告**：今天這集還沒完成 → 寄一封失敗報告；已完成但成功通知當時沒寄出 → 補寄；休市日不寄 |
| 假日、休市沒直播 | 找不到當天影片，直接結束，不算錯誤 |

- **為什麼不是直接設「每 3 分鐘」的排程**：GitHub Actions 排程最短只能每 5 分鐘一次，而且尖峰時段可能延遲或跳過。所以改成 11:10 啟動一次，由程式自己等到 11:20、每 3 分鐘檢查。
- **仍可能有的誤差**：如果 GitHub 把 11:10 的排程延遲超過 10 分鐘，第一次檢查就會晚於 11:20（啟動後立刻檢查，之後照樣每 3 分鐘）。萬一整次被略過，11:30 或 11:50 的備援排程會接手。
- **在 Actions 列表看到「Cancelled」是正常的**：主要那次還在等待或轉錄時，備援排程會排隊；同時排隊的只保留最新一個，較舊的會被 GitHub 自動取消。
- **看執行狀況**：<https://github.com/Lee200202/Notebooklm-Scraper-Test/actions>，以及試算表的「執行紀錄」工作表。寄信紀錄也會記在這裡（已寄成功通知／已寄失敗通知／寄信失敗）。
- **GitHub 自己的失敗通知信**：排程執行時的「轉錄失敗／額度不足」不會讓 Actions 顯示失敗，所以 GitHub 不會立即寄信，改由 17:05 的報告通知。金鑰錯誤、試算表無法開啟、17:05 報告寄不出去等**系統性錯誤**，Actions 仍會顯示失敗，GitHub 會立即寄信。

## 試算表內容

**「逐字稿」工作表**：一集一列

| 日期 | 星期 | 標題 | 影片ID | 影片網址 | 直播開始 | 直播結束 | 長度(分鐘) | 字數 | 模型 | 寫入時間 | 逐字稿1 | 逐字稿2 | … |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

- 逐字稿每 30 分鐘一段，段首標示 `【0:00:00 – 0:30:00】`
- 單一儲存格超過 30,000 字時，會接續寫到「逐字稿2」「逐字稿3」。Excel 每格上限是 32,767 字，這樣下載成 Excel 也不會被截斷
- **下載成 Excel**：試算表選單「檔案 → 下載 → Microsoft Excel (.xlsx)」

**「執行紀錄」工作表**：每次轉錄的「成功／失敗／配額不足」、設定檢查紀錄，以及錯誤原因。

## 調整設定（GitHub Variables，選填）

開啟 <https://github.com/Lee200202/Notebooklm-Scraper-Test/settings/variables/actions> →「New repository variable」。
沒設定就使用預設值：

| Name | 預設 | 說明 |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.8-flash` | 主要模型。想全部用便宜的模型可以改成 `gemini-3.5-flash-lite` |
| `GEMINI_FALLBACK_MODEL` | `gemini-3.5-flash-lite` | 主要模型額度用完時改用的模型；設成 `none` 則不使用備援 |
| `SEGMENT_MINUTES` | `30` | 每段長度（至少 5）。改小聽打會更完整，但請求次數會增加 |
| `VIDEO_FPS` | `0`（不指定） | 畫面取樣率。**建議不要設定**：實測設 `0.2` 可省約 6 成 token，但部分影片片段會一直回傳 400 錯誤（設定後程式遇到失敗會自動改成不帶 fps 重送，但會多用請求次數） |
| `INIT_VIDEO_COUNT` | `5` | init 補抓幾集 |
| `READY_DELAY_MINUTES` | `5` | 直播結束後等幾分鐘才轉錄 |
| `POLL_START` | `11:20` | 排程從幾點開始檢查（台灣時間，HH:MM）。如果改得比 11:10 早，也要一起修改 workflow 裡 11:10 的 cron |
| `POLL_END` | `14:00` | 最晚檢查到幾點 |
| `POLL_INTERVAL_MINUTES` | `3` | 每幾分鐘檢查一次 |
| `MAX_ATTEMPTS_PER_DAY` | `3` | 排程觸發時，同一集一天失敗幾次就暫停自動重試（在 Actions 頁面手動執行不受限） |
| `CUSTOM_VOCABULARY` | 股市常用詞 | 專有名詞提示，用半形逗號分隔，例如 `張震,台積電,外資` |
| `SMTP_HOST` | `smtp.gmail.com` | 不用 Gmail 寄信時才需要改 |
| `SMTP_PORT` | `465` | 465 = SSL；其他埠號會用 STARTTLS |

- 聽打規則（繁體中文、數字格式、分段方式）寫在 [`config.py`](config.py) 的 `SYSTEM_INSTRUCTION`
- 排程時間寫在 [`.github/workflows/daily-transcript.yml`](.github/workflows/daily-transcript.yml)，已經設定 `timezone: "Asia/Taipei"`。修改 17:05 報告的 cron 時，同檔案「執行」步驟裡比對的 cron 字串也要一起改
- 兩者都可以直接在 GitHub 網頁上按鉛筆圖示修改並 commit

## 額度與費用

| 服務 | 用量 | 費用 |
|---|---|---|
| Gemini API（免費方案） | 每集約 3 次請求（每 30 分鐘一段） | gemini-3.8-flash 每天約 20 次；額度用完自動改用 gemini-3.5-flash-lite（另一份額度） |
| Gemini API（付費方案） | 每集約 33～39 萬輸入 token、1 萬輸出 token | 依 2026 年牌價估算：gemini-3.8-flash 約 US$0.3／集、gemini-3.5-flash-lite 約 US$0.13／集 |
| YouTube Data API | 每次檢查 3 單位，每天約 100 單位 | 免費（每天上限 10,000） |
| Google Sheets API | 很少 | 免費 |
| GitHub Actions | 主要那次約 20～30 分鐘（含 11:10～11:20 的等待），備援與補跑約 12 次、每次約 1 分鐘 | 公開 repo 免費；Private repo 每月約用 700～1,000 分鐘，在 2,000 分鐘內 |

**Gemini 額度的重點**

- **額度按「專案＋模型」計算，不是按 API 金鑰**：同一個專案建立再多把金鑰，額度都一樣。不同模型各有一份額度，所以主要模型用完時，程式會自動改用備援模型（預設 `gemini-3.5-flash-lite`），不用等隔天。
- **每日額度在太平洋時間午夜重置，也就是台灣時間 15:00（美國冬令時間期間是 16:00），不是台灣午夜**。
  - 例如：晚上手動補抓用掉的額度，和**隔天早上 11:20 的排程**算在同一天。
  - 所以排程在 15:10～16:40 另外加跑 4 次，早上因額度不足沒抓到的話，重置後會自動補。
- 真的需要更多額度：在 AI Studio 替專案[綁定帳單](https://aistudio.google.com/projects)（Tier 1），額度會提高很多；依上表估算，每月 22 集約 US$3～7。
- **不要用多個 Google 帳號（或多個專案）的金鑰輪流使用來疊加免費額度**。Gemini API 條款引用的 [Google APIs 服務條款](https://developers.google.com/terms)規定不得規避 API 的使用限制，違反可能導致相關帳號被停權。本專案刻意只支援一組 `GEMINI_API_KEY`。
- 實際額度以 [AI Studio 額度頁面](https://aistudio.google.com/rate-limit) 為準（可以切換模型查看）。
- 免費方案的輸入內容可能會被 Google 用來改善產品，付費方案不會。

## 常見問題

**Q：`check` 出現「找不到試算表」或「沒有權限開啟試算表」？**
檢查三件事：`SPREADSHEET_ID` 只能貼網址中 `/d/` 和 `/edit` 之間的那段；試算表已共用給步驟 2 的 ② email；權限是「編輯者」。

**Q：`check` 出現「GOOGLE_SERVICE_ACCOUNT_JSON 格式錯誤」？**
③ 沒有複製完整，請重新複製整行再更新 Secret。如果當初的輸出已經不見，可以在 Cloud Shell 重新產生一把金鑰：

```bash
PROJECT_ID=$(gcloud projects list --filter="projectId~^yt-transcript-" --format="value(projectId)" | head -1)
gcloud iam service-accounts keys create key.json --iam-account="sheet-writer@${PROJECT_ID}.iam.gserviceaccount.com" --project="$PROJECT_ID"
base64 -w0 key.json; echo; rm -f key.json
```

**Q：Cloud Shell 腳本出現 `projects create` 錯誤？**
可能是專案數量達到上限，或帳號還沒同意 Google Cloud 條款。先到 <https://console.cloud.google.com/> 同意條款，刪除不用的專案後再執行一次。

**Q：排程突然都沒有執行？**
到 <https://github.com/Lee200202/Notebooklm-Scraper-Test/actions> 看是否顯示「This scheduled workflow is disabled」。有的話按「Enable workflow」。

**Q：哪天漏抓了怎麼辦？**
手動執行 mode `init`，會補齊最新 5 集中還沒寫入的；也可以在 video_id 填指定集數。頻道只保留最近幾集，影片被刪掉就抓不到了。

**Q：「今天已失敗 3 次，暫停重試」？**
看「執行紀錄」的「訊息」欄了解原因。修正後到 Actions 手動執行（mode 選 `init`，或在 video_id 填那集的 ID）；手動執行不受次數限制。

**Q：log 出現「等待結果時發生錯誤（HTTP 400）：Request contains an invalid argument.」？**
這是 Gemini 處理 YouTube 影片時的錯誤，通常在等待結果時才出現。實測有兩種情況：
- **帶了 `fps` 參數**（例如 `VIDEO_FPS=0.2`）：同一段影片重送幾次都失敗，改成不帶 fps 就成功。所以預設不帶 fps；有設定的話，程式遇到失敗會自動把那一段改成不帶 fps 重送。
- **沒帶 fps**：偶爾失敗一次，重送就好。

程式每段最多送 3 次（中間等 30 秒、90 秒）。3 次都失敗才算這集失敗，之後的排程會再試。

**Q：log 的時間是哪個時區？**
程式印出的時間是台灣時間。GitHub 介面左側另外顯示的時間戳記則是 UTC。

**Q：log 出現「HTTP 429 … You exceeded your current quota」？**
表示 Gemini 額度用完。log 會印出是哪一種額度，例如 `generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash` 就是該模型的每日請求數。
程式會等 30 秒、90 秒重試；仍然 429 就改用備援模型繼續。備援模型也用完，就記錄「配額不足」並停止，等 15:00 或 16:00 額度重置後的排程補跑。
「模型」欄會記錄這集實際用了哪些模型，例如 `gemini-3.8-flash + gemini-3.5-flash-lite`。

**Q：沒收到通知信？**
依序確認：
1. `check` 的寄信那一行是不是 ✅
2. 信件是否被分到垃圾郵件或「促銷內容」
3. 到「執行紀錄」看有沒有「寄信失敗」
4. `MAIL_APP_PASSWORD` 必須是應用程式密碼，不是 Gmail 登入密碼

注意：失敗報告只在 17:05 寄，而且只有「今天有直播但沒完成」時才會寄。

**Q：逐字稿有漏段或變成摘要？**
把 Variable `SEGMENT_MINUTES` 設成 `15`，每段越短越不容易漏，代價是請求次數加倍。

**Q：金鑰不小心外洩了？**
- Gemini：到 AI Studio 刪除該金鑰，建立新的
- YouTube：在 Cloud Shell 依序執行 `gcloud services api-keys delete youtube-data`，再重跑步驟 2 中「建立金鑰」與「印出金鑰」那兩個指令
- 服務帳戶：用上面「重新產生金鑰」的方法換新，再到 <https://console.cloud.google.com/iam-admin/serviceaccounts> 刪除舊金鑰
- 最後更新 GitHub Secrets

## 專案結構

| 檔案 | 用途 |
|---|---|
| [`main.py`](main.py) | 主流程：check／daily（--watch）／init／report／指定影片 |
| [`notifier.py`](notifier.py) | Gmail 寄信：成功通知、每日失敗報告、檢查用測試信 |
| [`youtube_client.py`](youtube_client.py) | YouTube Data API：找直播、判斷是否已結束 |
| [`transcriber.py`](transcriber.py) | Gemini API：分段聽打、背景執行輪詢、截斷自動切半、錯誤重試 |
| [`sheet_store.py`](sheet_store.py) | Google 試算表：寫入逐字稿與執行紀錄 |
| [`config.py`](config.py) | 設定讀取、金鑰格式驗證、聽打規則 |
| [`tests/test_pipeline.py`](tests/test_pipeline.py) | 離線測試：假的 Gemini／YouTube／試算表，不需要金鑰 |
| [`.github/workflows/daily-transcript.yml`](.github/workflows/daily-transcript.yml) | 排程與手動執行 |
| [`.github/workflows/tests.yml`](.github/workflows/tests.yml) | 程式碼有變動時自動跑測試 |
