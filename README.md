# CYBERBIZ 公司名單 / 本月有機會 自動追蹤網頁

用 **GitHub Pages**（顯示網頁）+ **GitHub Actions**（每天固定時間自動撈 Zoho CRM 資料並重新產出報表）做的免費、不需要另外租伺服器的自動化追蹤頁面。

## 這個東西長怎樣

- 每天固定時間（預設台北時間 08:00，可自己改）自動連到 Zoho CRM，撈出目前 Stage 還是「公司名單」或「本月有機會」的所有交易。
- 用 Zoho CRM 的 Timeline API 找出每筆交易「真正進入目前階段」的時間（Stage_Modified_Time 這個欄位本身是空的，不可靠，一定要查 Timeline 才準）。
- 依照公司內部規則分類：
  - 公司名單：第 2 個工作天提醒判斷有效/無效，第 3 個工作天標記可轉派
  - 本月有機會：第 11 個工作天月中檢核，第 22 個工作天月底檢核
- 產出一份靜態網頁（`docs/index.html`），依業務課（一~四課＋POS）分組，主管直接用瀏覽器網址打開就能看，不需要登入 Cowork 或 Claude。
- 已經抓過、階段沒變的交易不會重複呼叫 Timeline API（存在 `data/stage_cache.json`），避免 Zoho API 用量每天被吃光。

## 你需要自己做的事（含金鑰的步驟，AI 不能代替你輸入）

### 1. 建立 Zoho API 的 Self Client

1. 到 [Zoho API Console](https://api-console.zoho.com/)（用你們公司的 Zoho 帳號登入）。
2. 建立一個「Self Client」。
3. 產生授權碼（Scope 至少要包含：`ZohoCRM.modules.deals.READ`、`ZohoCRM.modules.deals.UPDATE`（如果之後要做寫回欄位/寄信通知才需要）、`ZohoCRM.settings.READ`）。
4. 用授權碼換一次 refresh token（Zoho API Console 的 Self Client 頁面通常會直接給你操作方式，或用一次性的 curl 指令換）。
5. 記下三個值：`Client ID`、`Client Secret`、`Refresh Token`。

> 如果不確定公司帳號是哪個資料中心（.com / .com.cn / .eu / .in / .jp），登入 Zoho CRM 時網址列看到的網域就是對應的資料中心，之後要填 `ZOHO_ACCOUNTS_DOMAIN` / `ZOHO_API_DOMAIN` 這兩個 secret 時要對應到同一個資料中心，否則會連不上。

### 2. 建立 GitHub Repo

1. 在 GitHub 建一個新的 repository（可以設成 Private，之後開 GitHub Pages 一樣可以用）。
2. 把這個資料夾（`generate_report.py`、`daily-report.yml`、`README.md`... 等所有檔案）整包上傳/push 上去，維持原本的資料夾結構不要動。

### 3. 設定 GitHub Secrets（金鑰只能你自己貼，不能代勞）

到 repo 的 **Settings → Secrets and variables → Actions → New repository secret**，新增以下幾個：

| Secret 名稱 | 值 |
|---|---|
| `ZOHO_CLIENT_ID` | 上一步拿到的 Client ID |
| `ZOHO_CLIENT_SECRET` | 上一步拿到的 Client Secret |
| `ZOHO_REFRESH_TOKEN` | 上一步拿到的 Refresh Token |
| `ZOHO_ACCOUNTS_DOMAIN` | 預設 `https://accounts.zoho.com`（依資料中心調整） |
| `ZOHO_API_DOMAIN` | 預設 `https://www.zohoapis.com`（依資料中心調整） |

### 4. 開啟 GitHub Pages

**Settings → Pages**，Source 選 "Deploy from a branch"，Branch 選你的主分支、資料夾選 `/docs`，存檔。等第一次 Actions 跑完後，這裡會顯示網址，例如：
`https://<你的帳號>.github.io/<repo名稱>/`

這個網址就是可以直接給主管們用瀏覽器打開的頁面。

### 5. 手動觸發跑第一次

到 repo 的 **Actions** 頁籤 → 選 "Daily Zoho CRM Report" → 右上角 **Run workflow** 按一下，手動跑一次確認整套流程沒問題。之後就會照 `.github/workflows/daily-report.yml` 裡設定的時間，每天自動跑。

## 之後可以怎麼調整

- **改跑的時間**：改 `.github/workflows/daily-report.yml` 裡的 `cron` 那一行，cron 是 UTC 時間，要換算成台北時間再減 8 小時。
- **業務課名單異動**：改 `scripts/generate_report.py` 裡的 `ROSTER` 字典即可，不用改別的地方。
- **月中/月底、2/3工作天門檻**：改 `generate_report.py` 最上面幾個常數（`COMPANY_LIST_REMIND_DAY` 等）。
- **國定假日**：目前工作天計算只排除週六、週日（v1），沒有排除國定假日跟補班日。之後要補的話，在 `workdays_between()` 這個函式裡加一份台灣國定假日清單即可。
- **寄信通知業務**：目前這個版本只做網頁，還沒有寄信功能。之後要加的話有兩種做法：（1）讓 `generate_report.py` 把算好的狀態寫回 Zoho CRM 自訂欄位，搭配你在 Zoho 後台自己設的 Workflow Rule + Email Alert 讓 Zoho 自己寄信；（2）在這個 Action 裡直接加一段用 Python 寄信（需要再申請一組寄信用的 API 金鑰存進 GitHub Secrets）。兩種都可以在這個架構上加，不用重做。

## 已知限制

- 工作天目前只排除週末，還沒排除國定假日/補班日。
- 「其他(非本次組織名單內)」分組代表 Owner 不在目前提供的一~四課/POS 名單裡（例如開店顧問、SA 等角色），不是資料遺漏。
- 這份報表看的是「即時的 Stage 現況」，不是歷史存檔；如果要留存每一天的歷史快照，可以另外把每天的 `docs/index.html` 或原始資料存到帶日期的檔名裡。
