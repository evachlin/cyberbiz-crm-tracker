CYBERBIZ 公司名單 / 本月有機會 自動追蹤網頁
用 GitHub Pages（顯示網頁）+ GitHub Actions（每天固定時間自動撈 Zoho CRM 資料並重新產出報表）做的免費、不需要另外租伺服器的自動化追蹤頁面。

這個東西長怎樣
每天固定時間（預設台北時間 08:00，可自己改）自動連到 Zoho CRM，撈出目前 Stage 還是「公司名單」或「本月有機會」的所有交易。
用 Zoho CRM 的 Timeline API 找出每筆交易「真正進入目前階段」的時間（Stage_Modified_Time 這個欄位本身是空的，不可靠，一定要查 Timeline 才準）。
依照公司內部規則分類：
公司名單：第 2 個工作天提醒判斷有效/無效，第 3 個工作天標記可轉派
本月有機會：第 11 個工作天月中檢核，第 22 個工作天月底檢核
產出一份靜態網頁（docs/index.html），依業務課（一~四課＋POS）分組，主管直接用瀏覽器網址打開就能看，不需要登入 Cowork 或 Claude。
已經抓過、階段沒變的交易不會重複呼叫 Timeline API（存在 data/stage_cache.json），避免 Zoho API 用量每天被吃光。
你需要自己做的事（含金鑰的步驟，AI 不能代替你輸入）
1. 建立 Zoho API 的 Self Client
到 Zoho API Console（用你們公司的 Zoho 帳號登入）。
建立一個「Self Client」。
產生授權碼（Scope 至少要包含：ZohoCRM.modules.deals.READ、ZohoCRM.modules.deals.UPDATE（如果之後要做寫回欄位/寄信通知才需要）、ZohoCRM.settings.READ）。
用授權碼換一次 refresh token（Zoho API Console 的 Self Client 頁面通常會直接給你操作方式，或用一次性的 curl 指令換）。
記下三個值：Client ID、Client Secret、Refresh Token。
如果不確定公司帳號是哪個資料中心（.com / .com.cn / .eu / .in / .jp），登入 Zoho CRM 時網址列看到的網域就是對應的資料中心，之後要填 ZOHO_ACCOUNTS_DOMAIN / ZOHO_API_DOMAIN 這兩個 secret 時要對應到同一個資料中心，否則會連不上。

2. 建立 GitHub Repo
在 GitHub 建一個新的 repository（可以設成 Private，之後開 GitHub Pages 一樣可以用）。
把這個資料夾（generate_report.py、daily-report.yml、README.md… 等所有檔案）整包上傳/push 上去，維持原本的資料夾結構不要動。
3. 設定 GitHub Secrets（金鑰只能你自己貼，不能代勞）
到 repo 的 Settings → Secrets and variables → Actions → New repository secret，新增以下幾個：

Secret 名稱	值
ZOHO_CLIENT_ID	上一步拿到的 Client ID
ZOHO_CLIENT_SECRET	上一步拿到的 Client Secret
ZOHO_REFRESH_TOKEN	上一步拿到的 Refresh Token
ZOHO_ACCOUNTS_DOMAIN	預設 https://accounts.zoho.com（依資料中心調整）
ZOHO_API_DOMAIN	預設 https://www.zohoapis.com（依資料中心調整）
4. 開啟 GitHub Pages
Settings → Pages，Source 選 “Deploy from a branch”，Branch 選你的主分支、資料夾選 /docs，存檔。等第一次 Actions 跑完後，這裡會顯示網址，例如： https://<你的帳號>.github.io/<repo名稱>/

這個網址就是可以直接給主管們用瀏覽器打開的頁面。

5. 手動觸發跑第一次
到 repo 的 Actions 頁籤 → 選 “Daily Zoho CRM Report” → 右上角 Run workflow 按一下，手動跑一次確認整套流程沒問題。之後就會照 .github/workflows/daily-report.yml 裡設定的時間，每天自動跑。

之後可以怎麼調整
改跑的時間：改 .github/workflows/daily-report.yml 裡的 cron 那一行，cron 是 UTC 時間，要換算成台北時間再減 8 小時。
業務課名單異動：改 scripts/generate_report.py 裡的 ROSTER 字典即可，不用改別的地方。
月中/月底、2/3工作天門檻：改 generate_report.py 最上面幾個常數（COMPANY_LIST_REMIND_DAY 等）。
國定假日：目前工作天計算只排除週六、週日（v1），沒有排除國定假日跟補班日。之後要補的話，在 workdays_between() 這個函式裡加一份台灣國定假日清單即可。
寄信通知業務：已經做了兩種，可以並存使用：（1）網頁上手動勾選＋按鈕，開 Gmail 分頁讓你自己按送出（見下方「勾選寄信提醒功能」）；（2）每天自動幫「可轉派／月底檢核」這兩個最緊急狀態的交易建立 Gmail 草稿（見下方「自動寄信提醒（Gmail 草稿）」）。
公司名單第 3 個工作天（escalate 狀態）明確規則：只留備註／通知，不自動轉派。之後無論選哪種寄信方式，第 3 天觸發的動作都是「加一則備註 + 通知業務本人／主管」，程式不會、也不應該自動修改交易的 Owner 欄位。目前這版本本來就是唯讀（只讀 Zoho 資料，不寫回任何東西），之後加通知功能時務必維持這條線，避免不小心做成自動轉派。
勾選寄信提醒功能
每張交易卡片左上角有勾選框，勾選後按頁面上方「寄送提醒信」，會依業務 email 自動分組，每位業務各開一個 Gmail 網頁版寫信分頁，主旨跟內容已經帶入這位業務被勾選的所有交易名稱、階段、工作天數、狀態，你確認沒問題後自己按送出，不會自動寄出。

原本這功能用的是 mailto: 連結（開電腦預設信箱程式），但公司網域管理的 Chrome 設定檔通常會鎖掉「網站註冊為預設信箱處理常式」這個瀏覽器權限，導致點了完全沒反應、也不會跳出任何錯誤訊息。改成直接開 Gmail 網頁版的寫信網址後就不受這個限制，因為那只是一般網址，不需要任何協定權限。如果之後有人改用 Outlook 或其他信箱系統，這段要對應改成該系統的網頁版寫信網址（或改回 mailto，但需先確認對方電腦有設定好預設信箱程式）。

「已於 X 通知」的標記只存在這台瀏覽器的本機儲存（localStorage），換一台電腦或別的主管打開不會同步看到；每天自動重新產生報表也不會受影響（因為存在瀏覽器不是存在 HTML 檔案裡）。如果之後要做到「所有人打開都看到同一份已通知記錄」，需要加一個後端把標記寫回 repo 或 Zoho，屬於跟自動寄信同等級的架構升級，目前先不做。

信件範本文字寫在 scripts/generate_report.py 的 NOTIFY_SCRIPT（buildMailto 那段），要調整語氣或內容直接改那裡的字串即可。

自動寄信提醒（Gmail 草稿）
這是選填功能：每天 Actions 跑完報表後，會自動幫「可轉派」「月底檢核」這兩個最緊急狀態的交易，依業務分組各建一封 HTML 格式的提醒信草稿，放進你自己 Gmail 帳號的草稿匣（不是業務本人的信箱，因為我們只授權了你自己的帳號）。你每天早上打開 Gmail 草稿匣，逐一看過沒問題後手動按送出即可。同一筆交易同一個狀態只會建立一次草稿，不會每天重複產生。

沒設定下面這三組 Secret 的話，這個功能會自動跳過，完全不影響報表本身正常產生，你可以先不做這一段，之後想要了再回來設定。

1. 到 Google Cloud Console 建立專案並啟用 Gmail API
用「你要拿來寄草稿信的那個 Gmail 帳號」登入 Google Cloud Console（建議就用你自己的 cyberbiz.io 帳號）。
上方選單建立一個新專案（New Project），名稱隨意，例如「cyberbiz-crm-notify」。
左側選單 APIs & Services → Library，搜尋「Gmail API」，點進去按 Enable 啟用。
2. 設定 OAuth 同意畫面
左側選單 APIs & Services → OAuth consent screen。
User Type 選 Internal（因為你們是 Google Workspace 網域帳號，選 Internal 只有 cyberbiz.io 網域內的人能用，不需要 Google 審核，設定起來最快）。如果畫面上沒有 Internal 選項只能選 External，選 External 也可以，但要多一步把自己的帳號加進「測試使用者」名單。
填 App name（隨意，例如「CRM 交易提醒」）、User support email、Developer contact information，其餘用預設值，一路 Save/Continue 到完成。
3. 建立 OAuth 用戶端（Client ID / Secret）
左側選單 APIs & Services → Credentials → Create Credentials → OAuth client ID。
Application type 選 Web application。
名稱隨意。Authorized redirect URIs 這欄一定要加上：https://developers.google.com/oauthplayground（這是等一下要用來換 refresh token 的 Google 官方工具的網址，沒加這個等一下會授權失敗）。
建立後畫面會顯示 Client ID 和 Client Secret，先複製記下來，這兩個等一下要用。
4. 用 OAuth Playground 換 Refresh Token（不用寫任何程式）
開啟 Google OAuth 2.0 Playground。
點右上角齒輪圖示 → 打勾 Use your own OAuth credentials → 把上一步的 Client ID、Client Secret 貼進去對應欄位。
左側「Step 1」找到輸入框（Input your own scopes），貼上：https://www.googleapis.com/auth/gmail.compose，按 Authorize APIs。
跳轉到 Google 登入畫面，選你要用來收草稿的那個 Gmail 帳號登入並同意授權（如果跳出「這個應用程式未經 Google 驗證」的警告，是正常的，因為我們選的是 Internal/自己的應用程式，點「進階」→「前往…（不安全）」繼續即可，只有你自己在用不用擔心）。
回到 Playground，「Step 2」按 Exchange authorization code for tokens。
畫面會出現 Refresh token，複製記下來。（Access token 不用管，那個每小時就會過期，程式會自己用 refresh token 重新換，不需要存。）
5. 把三組值存進 GitHub Secrets
跟前面 Zoho 那步一樣，到 repo 的 Settings → Secrets and variables → Actions → New repository secret：

Secret 名稱	值
GMAIL_CLIENT_ID	步驟 3 拿到的 Client ID
GMAIL_CLIENT_SECRET	步驟 3 拿到的 Client Secret
GMAIL_REFRESH_TOKEN	步驟 4 拿到的 Refresh Token
存完之後到 Actions 頁籤手動 Run workflow 跑一次，跑完打開 Gmail 草稿匣確認有沒有新草稿出現。如果沒有出現，去 Actions 那次執行的 log 裡看有沒有印出「換 Gmail access token 失敗」或「建立提醒草稿失敗」之類的錯誤訊息，通常代表某一組值貼錯或 Redirect URI 沒加對。

公司網域帳號有些設定是被 IT／Google Workspace 管理員鎖住的（例如禁止建立 Google Cloud 專案，或禁止安裝「未經驗證的協力廠商應用程式」）。如果卡在這幾步，去 Google Workspace 系統管理控制台的「安全性 → API 控管」確認一下這個應用程式有沒有被擋，或直接請 IT 協助開權限。

之後想改成「不用手動按送出，系統自己直接寄」
現在是保守做法：先建草稿，你看過確認沒問題再自己送，跑穩一陣子沒問題之後可以再升級成全自動寄出。到時候只要兩個小改動：

回到步驟 4 的 OAuth Playground，把 scope 從 gmail.compose 換成 gmail.send，重新走一次流程拿新的 Refresh Token，更新 GitHub Secret 裡的 GMAIL_REFRESH_TOKEN。
scripts/generate_report.py 裡 create_gmail_draft() 呼叫的網址從 .../drafts 改成 .../messages/send，body 一樣是 {"raw": ...}，不用再包一層 "message"。
其餘（信件內容怎麼組、分組邏輯、避免重複寄送的紀錄檔 data/gmail_draft_log.json）都不用動，是同一套邏輯。

已知限制
工作天目前只排除週末，還沒排除國定假日/補班日。
報表跟自動草稿都只處理 ROSTER 名單裡列出的人（業務一~四課、POS，加上 Ambrose、Ellie、Sabrina、William）。Owner 不在名單裡的交易會被整筆過濾掉，不會出現在網頁或草稿裡，不是資料遺漏。要新增/移除誰，改 scripts/generate_report.py 裡的 ROSTER 字典即可。
這份報表看的是「即時的 Stage 現況」，不是歷史存檔；如果要留存每一天的歷史快照，可以另外把每天的 docs/index.html 或原始資料存到帶日期的檔名裡。
自動 Gmail 草稿是建立在你自己授權的那個 Gmail 帳號的草稿匣裡，收件人（To）雖然填的是業務本人，但草稿本身不會出現在業務自己的信箱，需要你手動打開、確認、按送出，這才算真的寄出去。



CYBERBIZ 公司名單 / 本月有機會 自動追蹤網頁
這個東西長怎樣
你需要自己做的事（含金鑰的步驟，AI 不能代替你輸入）
1. 建立 Zoho API 的 Self Client
2. 建立 GitHub Repo
3. 設定 GitHub Secrets（金鑰只能你自己貼，不能代勞）
4. 開啟 GitHub Pages
5. 手動觸發跑第一次
之後可以怎麼調整
勾選寄信提醒功能
自動寄信提醒（Gmail 草稿）
1. 到 Google Cloud Console 建立專案並啟用 Gmail API
2. 設定 OAuth 同意畫面
3. 建立 OAuth 用戶端（Client ID / Secret）
4. 用 OAuth Playground 換 Refresh Token（不用寫任何程式）
5. 把三組值存進 GitHub Secrets
之後想改成「不用手動按送出，系統自己直接寄」
已知限制
