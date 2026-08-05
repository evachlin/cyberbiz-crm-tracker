#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CYBERBIZ Zoho CRM 每日自動化報表
=================================
每天由 GitHub Actions 排程執行一次：
1. 用 Zoho Self Client 的 refresh token 換 access token
2. 用 COQL 撈出目前 Stage 為「公司名單」或「本月有機會」的所有交易
3. 對「新出現」或「剛換階段」的交易，呼叫 Zoho CRM Timeline API（__timeline）
   找出真正進入目前階段的時間點（Stage_Modified_Time 這欄本身是空的，不可靠）
4. 已經抓過、階段沒變的交易，直接沿用 data/stage_cache.json 裡的進入時間，
   不重複呼叫 Timeline API —— 避免每天全量重查把 Zoho API 用量吃光
5. 依「本月有機會：11 / 22 個工作天」「公司名單：2 / 3 個工作天」規則分類
6. 產出一份靜態 HTML（docs/index.html），交給 GitHub Pages 顯示

需要的環境變數（由 GitHub Actions secrets 帶入，不要寫在程式碼裡）：
  ZOHO_CLIENT_ID
  ZOHO_CLIENT_SECRET
  ZOHO_REFRESH_TOKEN
  ZOHO_ACCOUNTS_DOMAIN   (預設 https://accounts.zoho.com，中國資料中心用 .com.cn，歐洲用 .eu，印度用 .in)
  ZOHO_API_DOMAIN        (預設 https://www.zohoapis.com，需跟 ACCOUNTS_DOMAIN 對應的資料中心一致)

  以下 3 個是「自動建立 Gmail 提醒草稿」功能用的，選填 —— 沒設定的話這功能會自動跳過，
  不影響報表本身照常產生。設定方式見 README「自動寄信提醒（Gmail 草稿）」章節。
  GMAIL_CLIENT_ID
  GMAIL_CLIENT_SECRET
  GMAIL_REFRESH_TOKEN
"""

import base64
import os
import json
import html
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict, Counter
from email.message import EmailMessage

import requests

TAIPEI_TZ = timezone(timedelta(hours=8))

ZOHO_CLIENT_ID = os.environ["ZOHO_CLIENT_ID"]
ZOHO_CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
ZOHO_REFRESH_TOKEN = os.environ["ZOHO_REFRESH_TOKEN"]
ACCOUNTS_DOMAIN = os.environ.get("ZOHO_ACCOUNTS_DOMAIN", "https://accounts.zoho.com")
API_DOMAIN = os.environ.get("ZOHO_API_DOMAIN", "https://www.zohoapis.com")
API_VERSION = "v8"

# 網頁版 Zoho CRM 的交易連結（跟上面的 API_DOMAIN 是不同東西：API_DOMAIN 是給程式呼叫用的，
# 這個是給人點的瀏覽器網址）。org 代碼來自 Zoho「Organization」API 的 domain_name 欄位，
# Cyberbiz 這個組織固定是 org695870979，除非之後換了 Zoho 帳號否則不用改。
ZOHO_CRM_WEB_DOMAIN = os.environ.get("ZOHO_CRM_WEB_DOMAIN", "https://crm.zoho.com")
ZOHO_CRM_ORG_ID = os.environ.get("ZOHO_CRM_ORG_ID", "org695870979")

# Gmail API（自動建立提醒草稿用，選填）。三個都設定了才會啟用，任一沒填就自動跳過這個功能。
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN", "")
GMAIL_AUTO_NOTIFY_ENABLED = bool(GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN)
# 自動建草稿只對「需優先處理」等級的狀態觸發（見下面 URGENT_STATUSES 常數），其餘狀態
# 維持只顯示在網頁上，不自動發信提醒，避免信件太多、太早通知造成干擾。

# 主管彙整信（公司名單可轉派）：收件人固定寄給這位主管，他評估是否要轉派。
# 用 "or" 不是 .get(key, default)：GitHub Actions 的 env: 區塊只要寫了
# secrets.MANAGER_SUMMARY_EMAIL，即使沒設定那個 Secret 也會帶一個空字串進來，
# 用 .get(key, default) 拿到的會是空字串而不是預設值，要用 "or" 才能正確 fallback。
MANAGER_SUMMARY_EMAIL = os.environ.get("MANAGER_SUMMARY_EMAIL") or "ambrose.tsai@cyberbiz.io"
MANAGER_SUMMARY_LOG_KEY = "_manager_summary"  # 存在 gmail_draft_log.json 裡的保留 key，跟一般交易 id 分開

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(REPO_ROOT, "data", "stage_cache.json")
DRAFT_LOG_PATH = os.path.join(REPO_ROOT, "data", "gmail_draft_log.json")
OUTPUT_HTML_PATH = os.path.join(REPO_ROOT, "docs", "index.html")

# ---------------------------------------------------------------------------
# 業務課對照表：跟 Zoho 裡的 Owner email 對應。人員異動時只要改這裡。
# ---------------------------------------------------------------------------
ROSTER = {
    "angel.yeh@cyberbiz.io": ("Angel Yeh", "業務一課"),
    "vivian.chan@cyberbiz.io": ("Vivian Chan", "業務一課"),
    "ryder.wu@cyberbiz.io": ("Ryder Wu", "業務一課"),
    "sarah.lin@cyberbiz.io": ("Sarah Lin", "業務一課"),
    "eason.hsiao@cyberbiz.io": ("Eason Hsiao", "業務二課"),
    "justin.tsao@cyberbiz.io": ("Justin Tsao", "業務二課"),
    "josh.wu@cyberbiz.io": ("Josh Wu", "業務二課"),
    "steven.lin@cyberbiz.io": ("Steven Lin", "業務二課"),
    "andy.zhuang@cyberbiz.io": ("Andy Zhuang", "業務二課"),
    "allen.yk.chen@cyberbiz.io": ("Allen YK Chen", "業務三課"),
    "chris.zhong@cyberbiz.io": ("Chris Zhong", "業務三課"),
    "shelly.wang@cyberbiz.io": ("Shelly Wang", "業務三課"),
    "ryan.fang@cyberbiz.io": ("Ryan Fang", "業務三課"),
    "chester.liao@cyberbiz.io": ("Chester Liao", "業務四課"),
    "francis.cheng@cyberbiz.io": ("Francis Cheng", "業務四課"),
    "calvin.chen@cyberbiz.io": ("Calvin Chen", "業務四課"),
    "vincent.wu@cyberbiz.io": ("Vincent Wu", "業務四課"),
    "jj.hsieh@cyberbiz.io": ("JJ Hsieh", "POS"),
    "jack.lin@cyberbiz.io": ("Jack Lin", "POS"),
    "chuan.luo@cyberbiz.io": ("Chuan Luo", "POS"),
    "ellie.chu@cyberbiz.io": ("Ellie Chu", "業務一課"),
    "sabrina.hua@cyberbiz.io": ("Sabrina Hua", "業務二課"),
    "william.wang@cyberbiz.io": ("William Wang", "業務三課"),
    "ambrose.tsai@cyberbiz.io": ("Ambrose Tsai", "Ambrose"),
}
TEAM_ORDER = ["業務一課", "業務二課", "業務三課", "業務四課", "POS", "Ambrose"]

# 報表跟自動草稿都只處理 ROSTER 裡列出的這些人，Owner 不在上面這份名單裡的交易一律不會
# 出現在報表或草稿裡（不是資料遺漏，是刻意過濾掉）。要放行更多人，把 email 加進 ROSTER 即可，
# 不需要另外改這個過濾邏輯。
ROSTER_ONLY_FILTER = True

STAGES_TRACKED = ["公司名單", "本月有機會", "短期追蹤"]
TRACK_SINCE = "2026-01-01T00:00:00+08:00"  # 只追蹤這個日期之後建立的交易，避免把歷史上所有卡在這幾個階段的舊資料全抓進來

# 規則門檻（工作天，只排除週六週日，尚未納入國定假日 —— 之後要補請看 README）
COMPANY_LIST_REMIND_DAY = 2      # 第 2 個工作天：提醒判斷有效/無效
COMPANY_LIST_ESCALATE_DAY = 3    # 第 3 個工作天：標記可轉派
OPPORTUNITY_MIDCHECK_DAY = 11    # 月中檢核
OPPORTUNITY_ENDCHECK_DAY = 22    # 月底檢核
# 短期追蹤：只有一個門檻，用「日曆天」算（不是工作天），超過就標記需優先處理
SHORT_TERM_ESCALATE_DAYS = 90

# 重複提醒：交易進入某個「需優先處理」狀態後，如果業務一直沒處理、狀態也沒變，
# 隔幾天要再提醒一次（不然預設只會提醒一次，之後不管拖多久都不會再收到信）。
# 業務個人提醒信（三個階段共用）跟寄給主管的公司名單彙整信，用不同的重複間隔：
# 業務個人的怕太頻繁疲乏，7 天一次；主管彙整信跟主管討論過，希望更緊盯轉派決策，改成 2 天一次。
# 都是用「距離上次建立草稿的日曆天數」判斷，跟工作天／日曆天的天數計算方式無關。
REMIND_REPEAT_DAYS = 7
MANAGER_SUMMARY_REMIND_REPEAT_DAYS = 2


# ---------------------------------------------------------------------------
# Zoho API 基礎函式
# ---------------------------------------------------------------------------
def get_access_token():
    resp = requests.post(
        f"{ACCOUNTS_DOMAIN}/oauth/v2/token",
        params={
            "refresh_token": ZOHO_REFRESH_TOKEN,
            "client_id": ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"換 access token 失敗：{data}")
    return data["access_token"]


def zoho_headers(token):
    return {"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"}


def coql_query(token, select_query):
    """執行 COQL，自動翻頁抓完全部符合條件的資料。"""
    url = f"{API_DOMAIN}/crm/{API_VERSION}/coql"
    rows, offset, page_size = [], 0, 200
    while True:
        body = {"select_query": f"{select_query} LIMIT {page_size} OFFSET {offset}"}
        resp = requests.post(url, headers=zoho_headers(token), json=body, timeout=60)
        if resp.status_code == 204:
            break
        resp.raise_for_status()
        result = resp.json()
        page_rows = result.get("data", [])
        rows.extend(page_rows)
        info = result.get("info", {})
        if not info.get("more_records"):
            break
        offset += page_size
        time.sleep(0.3)  # 對 API 客氣一點
    return rows


def get_timeline(token, record_id, module="Deals", max_pages=10):
    """
    抓 Timeline，會翻頁抓到 max_pages 頁為止（不是只抓第一頁）。

    真正的根本 bug：Zoho Timeline API 回傳的資料是放在最上層的 "__timeline" 鍵，
    不是 "data" 鍵（這點跟其他 CRM API 不一樣，很容易誤會）。原本程式寫
    resp.json().get("data", [])，因為回應裡根本沒有 "data" 這個鍵，
    永遠都拿到空陣列 —— 等於每一筆交易查 Timeline 都直接失敗、
    全部都用建立時間回退估算，不是只有少數交易的問題。
    """
    url = f"{API_DOMAIN}/crm/{API_VERSION}/{module}/{record_id}/__timeline"
    items = []
    page_token = None
    for _ in range(max_pages):
        # 注意：Zoho 規定 per_page 跟 page_token 不能同時給，翻頁時只能用 page_token
        if page_token:
            params = {
                "sort_by": "audited_time",
                "sort_order": "desc",
                "include_inner_details": "true",
                "page_token": page_token,
            }
        else:
            params = {
                "sort_by": "audited_time",
                "sort_order": "desc",
                "include_inner_details": "true",
                "per_page": 200,
            }
        resp = requests.get(url, headers=zoho_headers(token), params=params, timeout=15)
        if resp.status_code == 204:
            break
        resp.raise_for_status()
        result = resp.json()
        page_items = result.get("__timeline") or []
        if not page_items:
            break
        items.extend(page_items)
        info = result.get("info", {})
        if not info.get("more_records"):
            break
        page_token = info.get("next_page_token")
        if not page_token:
            break
    return items


def find_stage_entry_time(token, record_id, target_stage, created_time):
    """
    在 Timeline 裡找出「變成 target_stage 那一刻」的時間。
    找不到（例如一開始建立就是這個階段）就回退用 Created_Time。
    """
    try:
        timeline = get_timeline(token, record_id)
    except requests.HTTPError:
        return created_time, "無法讀取階段歷史，暫以建立時間估算"
    for item in timeline:
        # 有些 timeline 事件（例如新增 Task/Note）field_history 這個鍵存在但值是 null，
        # 不是缺少這個鍵，用 .get(key, []) 接不住 None，要用 "or []" 才保險。
        for change in item.get("field_history") or []:
            if change.get("api_name") == "Stage":
                new_val = (change.get("_value") or {}).get("new")
                if new_val == target_stage:
                    return item.get("audited_time"), None
    return created_time, "建立時即為此階段（或超出時間軸追蹤範圍），以建立時間估算"


# ---------------------------------------------------------------------------
# 工作天計算（v1：只排除週六日，未納入國定假日／補班日）
# ---------------------------------------------------------------------------
def parse_zoho_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s)


def workdays_between(start_dt, end_dt):
    if start_dt is None:
        return None
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=TAIPEI_TZ)
    start = start_dt.astimezone(TAIPEI_TZ).date()
    end = end_dt.astimezone(TAIPEI_TZ).date()
    if end < start:
        return 0
    count, cur = 0, start
    while cur < end:
        if cur.weekday() < 5:  # 0=Mon ... 4=Fri
            count += 1
        cur += timedelta(days=1)
    return count


def calendar_days_between(start_dt, end_dt):
    """短期追蹤用：算日曆天（含假日），不排除週末，跟 workdays_between 不一樣。"""
    if start_dt is None:
        return None
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=TAIPEI_TZ)
    start = start_dt.astimezone(TAIPEI_TZ).date()
    end = end_dt.astimezone(TAIPEI_TZ).date()
    return max((end - start).days, 0)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


def resolve_owner(email, first_name, last_name):
    if email in ROSTER:
        return ROSTER[email]
    disp = f"{(first_name or '').strip()} {(last_name or '').strip()}".strip()
    return (disp or email or "未知"), "其他(非本次組織名單內)"


def fetch_current_deals(token):
    stages_sql = ",".join(f"'{s}'" for s in STAGES_TRACKED)
    query = (
        "SELECT id, Deal_Name, Owner.first_name, Owner.last_name, Owner.email, "
        "Stage, Created_Time, Modified_Time, Amount, Closing_Date, product_type, visitor_source "
        f"FROM Deals WHERE (Stage in ({stages_sql})) AND (Created_Time >= '{TRACK_SINCE}') "
        "ORDER BY Created_Time ASC"
    )
    rows = coql_query(token, query)
    if ROSTER_ONLY_FILTER:
        # 只留下 Owner 在 ROSTER 名單裡的交易，其他人的一律不進報表、也不會被自動草稿處理到。
        rows = [r for r in rows if r.get("Owner.email") in ROSTER]
    return rows


def build_records(token, rows, cache):
    now = datetime.now(TAIPEI_TZ)
    new_cache = dict(cache)  # 先繼承舊快取，中途存檔時舊資料不會不見
    records = []
    api_calls_made = 0
    total = len(rows)
    started = time.time()

    print(f"開始處理 {total} 筆交易...", flush=True)

    for idx, r in enumerate(rows, start=1):
        rid = r["id"]
        stage = r["Stage"]
        cached = cache.get(rid)

        # 只有「乾淨命中 Timeline 記錄」（note 是 None）才信任快取；
        # 如果快取裡的是退回估算值（note 有內容），代表當初沒查到真正的階段變更時間，
        # 不能永久鎖死，每次都要重查，直到查到真正的 Timeline 記錄為止（自我修正）。
        if cached and cached.get("stage") == stage and cached.get("entered_at") and cached.get("note") is None:
            entered_at = cached["entered_at"]
            note = cached.get("note")
        else:
            created_time = r.get("Created_Time")
            try:
                entered_at, note = find_stage_entry_time(token, rid, stage, created_time)
            except requests.exceptions.RequestException as e:
                # 單筆網路問題不要讓整個程式停掉，退回用建立時間，下次執行會再重試這一筆
                entered_at, note = created_time, f"查 Timeline 時發生網路問題（{e.__class__.__name__}），暫以建立時間估算"
            api_calls_made += 1

        new_cache[rid] = {"stage": stage, "entered_at": entered_at, "note": note}

        # 每處理 20 筆，或每呼叫 20 次 API，就先把目前的快取存檔一次。
        # 這樣如果這次執行中途被取消或失敗，下次重跑不會從零開始，只需要接著處理剩下的。
        if idx % 20 == 0 or idx == total:
            save_cache(new_cache)
            elapsed = time.time() - started
            print(f"進度 {idx}/{total}（本次已呼叫 Timeline API {api_calls_made} 次，已耗時 {elapsed:.0f} 秒）", flush=True)

        entered_dt = parse_zoho_dt(entered_at)

        name, team = resolve_owner(r.get("Owner.email"), r.get("Owner.first_name"), r.get("Owner.last_name"))

        if stage == "公司名單":
            age = workdays_between(entered_dt, now) if entered_dt else None
            age_unit = "個工作天"
            if age is None:
                status = "unknown"
            elif age >= COMPANY_LIST_ESCALATE_DAY:
                status = "escalate"
            elif age >= COMPANY_LIST_REMIND_DAY:
                status = "remind"
            else:
                status = "new"
        elif stage == "本月有機會":
            age = workdays_between(entered_dt, now) if entered_dt else None
            age_unit = "個工作天"
            if age is None:
                status = "unknown"
            elif age >= OPPORTUNITY_ENDCHECK_DAY:
                status = "endcheck"
            elif age >= OPPORTUNITY_MIDCHECK_DAY:
                status = "midcheck"
            else:
                status = "tracking"
        else:  # 短期追蹤：用日曆天算，只有一個門檻
            age = calendar_days_between(entered_dt, now) if entered_dt else None
            age_unit = "天"
            if age is None:
                status = "unknown"
            elif age >= SHORT_TERM_ESCALATE_DAYS:
                status = "st_escalate"
            else:
                status = "st_tracking"

        records.append({
            "id": rid,
            "交易名稱": r.get("Deal_Name"),
            "業務": name,
            "業務Email": r.get("Owner.email") or "",
            "業務課": team,
            "Stage": stage,
            "進入此階段時間": entered_at,
            "進入時間備註": note,
            "工作天數": age,
            "天數單位": age_unit,
            "狀態": status,
            "金額": r.get("Amount"),
            "預計成交日": r.get("Closing_Date"),
            "商品類別": r.get("product_type"),
            "名單來源": r.get("visitor_source"),
        })

    return records, new_cache, api_calls_made


# ---------------------------------------------------------------------------
# HTML 產出
# ---------------------------------------------------------------------------
STATUS_LABEL = {
    "new": "新名單",
    "remind": f"提醒判斷（≥{COMPANY_LIST_REMIND_DAY}工作天）",
    "escalate": f"可轉派（≥{COMPANY_LIST_ESCALATE_DAY}工作天）",
    "tracking": "追蹤中",
    "midcheck": f"月中檢核（≥{OPPORTUNITY_MIDCHECK_DAY}工作天）",
    "endcheck": f"月底檢核（≥{OPPORTUNITY_ENDCHECK_DAY}工作天）",
    "st_tracking": "短期追蹤中",
    "st_escalate": f"短期追蹤逾期（≥{SHORT_TERM_ESCALATE_DAYS}天）",
    "unknown": "天數未知",
}
STATUS_CLASS = {
    "new": "st-new", "remind": "st-warn", "escalate": "st-danger",
    "tracking": "st-new", "midcheck": "st-warn", "endcheck": "st-danger",
    "st_tracking": "st-new", "st_escalate": "st-danger",
    "unknown": "st-unknown",
}
# 卡片排序用的緊急程度：數字越小越緊急，排最前面。同一層再依工作天數從多到少排。
STATUS_PRIORITY = {
    "escalate": 0, "endcheck": 0, "st_escalate": 0,
    "remind": 1, "midcheck": 1,
    "new": 2, "tracking": 2, "st_tracking": 2,
    "unknown": 3,
}
# 需要優先處理／會觸發自動 Gmail 草稿的狀態，集中在這裡管理，避免各處各寫一份漏掉新狀態。
URGENT_STATUSES = ("escalate", "endcheck", "st_escalate")


# ---------------------------------------------------------------------------
# 報表本體是一張可篩選、可排序的表格（不再是巢狀展開的 accordion）。
# 資料在 Python 端算好之後整包以 JSON 塞進頁面（REPORT_ROWS），畫面渲染、排序、篩選、
# 勾選寄信全部交給前端 JS（REPORT_SCRIPT）處理，純前端、不需要任何伺服器。
#
# 勾選＋寄送提醒信：開新分頁到 Gmail 網頁版寫信畫面（不是 mailto:，因為公司網域管理的
# Chrome 設定檔會鎖掉「預設信箱處理常式」權限，導致 mailto 完全沒反應也不會跳錯誤）。
# 「已通知」標記存在瀏覽器 localStorage，只在同一台裝置/瀏覽器有效，不會跨裝置同步、
# 也不會寫回 Zoho 或 GitHub（那需要後端，見 README）。
# ---------------------------------------------------------------------------
REPORT_SCRIPT = '''<script>
(function () {
  var STORE_KEY = "cyberbiz_notified_v1";
  var activeTeam = "all";
  var sortKey = null;
  var sortDir = 1;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function loadNotified() {
    try { return JSON.parse(localStorage.getItem(STORE_KEY) || "{}"); }
    catch (e) { return {}; }
  }
  function saveNotified(map) {
    localStorage.setItem(STORE_KEY, JSON.stringify(map));
  }
  function todayStr() {
    var d = new Date();
    return d.getFullYear() + "/" + String(d.getMonth() + 1).padStart(2, "0") + "/" + String(d.getDate()).padStart(2, "0");
  }
  function daysText(r) {
    return (r.days === null || r.days === undefined) ? "天數未知" : (r.days + " " + r.daysUnit);
  }

  function renderLog(map) {
    var body = document.getElementById("notifyLogBody");
    var empty = document.getElementById("notifyLogEmpty");
    if (!body) return;
    var rows = Object.keys(map).map(function (id) { return map[id]; })
      .sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); });
    if (rows.length === 0) {
      body.innerHTML = "";
      if (empty) empty.style.display = "block";
      return;
    }
    if (empty) empty.style.display = "none";
    body.innerHTML = rows.map(function (r) {
      var daysTxt = (r.days === null || r.days === undefined) ? "-" : (r.days + " " + (r.daysUnit || "個工作天"));
      return "<tr><td>" + esc(r.date) + "</td><td>" + esc(r.owner) + "</td><td>" + esc(r.email) +
        "</td><td>" + esc(r.name) + "</td><td>" + esc(r.stage) + "</td><td>" + daysTxt +
        "</td><td>" + esc(r.status) + "</td></tr>";
    }).join("");
  }

  function updateToolbar() {
    var checked = document.querySelectorAll(".notify-box:checked");
    var btn = document.getElementById("notifyBtn");
    btn.textContent = "寄送提醒信（已勾選 " + checked.length + " 筆）";
    btn.disabled = checked.length === 0;

    var selectAll = document.getElementById("selectAllBox");
    if (selectAll) {
      var visible = document.querySelectorAll(".notify-box");
      if (visible.length === 0) {
        selectAll.checked = false;
        selectAll.indeterminate = false;
      } else {
        selectAll.checked = checked.length === visible.length;
        selectAll.indeterminate = checked.length > 0 && checked.length < visible.length;
      }
    }
  }

  function buildGmailUrl(ownerEmail, ownerName, deals) {
    var subject = "【ZOHO交易階段提醒】" + ownerName + " 你有 " + deals.length + " 筆交易需要更新進度";
    var lines = [ownerName + " 你好，", "",
      "以下 " + deals.length + " 筆交易目前停留在原階段已經一段時間，麻煩抽空看一下，更新最新進度或判斷結果：", ""];
    deals.forEach(function (d) {
      lines.push("・" + d.name + "（" + d.stage + "，" + daysText(d) + "，" + d.statusLabel + "）");
      lines.push("   點我開啟這筆交易：" + d.crmUrl);
    });
    lines.push("", "如果其實已經處理了、只是系統還沒更新，也麻煩補填一下最新狀態，避免被誤判成沒進度。", "", "謝謝！");
    var body = lines.join("\\n");
    // Gmail 網頁版的寫信網址，不是 mailto:（mailto 需要瀏覽器/系統註冊處理常式，
    // 公司網域管理的 Chrome 設定檔常常鎖掉這個權限，導致完全沒反應）。
    return "https://mail.google.com/mail/?view=cm&fs=1&tf=1" +
      "&to=" + encodeURIComponent(ownerEmail) +
      "&su=" + encodeURIComponent(subject) +
      "&body=" + encodeURIComponent(body);
  }

  function onSendClick() {
    var checked = Array.prototype.slice.call(document.querySelectorAll(".notify-box:checked"));
    if (checked.length === 0) return;

    var groups = {};
    checked.forEach(function (box) {
      var row = REPORT_ROWS.find(function (r) { return r.id === box.dataset.id; });
      if (!row) return;
      var key = row.email || row.owner;
      if (!groups[key]) { groups[key] = { owner: row.owner, email: row.email, deals: [] }; }
      groups[key].deals.push(row);
    });

    var map = loadNotified();
    Object.keys(groups).forEach(function (key) {
      var g = groups[key];
      if (!g.email) {
        alert(g.owner + " 找不到 email，已略過，請手動聯絡。");
        return;
      }
      var url = buildGmailUrl(g.email, g.owner, g.deals);
      // 同步呼叫 window.open（不要用 setTimeout 延遲），瀏覽器才會判斷這是使用者
      // 剛剛點擊觸發的動作，不會被彈跳視窗攔截器擋掉。
      window.open(url, "_blank");
      g.deals.forEach(function (d) {
        map[d.id] = {
          date: todayStr(), ts: Date.now(),
          owner: g.owner, email: g.email,
          name: d.name, stage: d.stage, days: d.days, daysUnit: d.daysUnit, status: d.statusLabel
        };
      });
    });
    saveNotified(map);
    renderTable();
  }

  function renderTable() {
    var notified = loadNotified();
    var data = REPORT_ROWS.filter(function (r) {
      if (activeTeam !== "all" && r.team !== activeTeam) return false;
      return true;
    });
    if (sortKey) {
      data = data.slice().sort(function (a, b) {
        var av = a[sortKey], bv = b[sortKey];
        if (av === null || av === undefined) av = (typeof bv === "number") ? -1 : "";
        if (bv === null || bv === undefined) bv = (typeof av === "number") ? -1 : "";
        if (typeof av === "string") return sortDir * av.localeCompare(bv);
        return sortDir * (av - bv);
      });
    }
    var tbody = document.getElementById("tbody");
    tbody.innerHTML = "";
    data.forEach(function (r) {
      var isNotified = notified[r.id];
      var tr = document.createElement("tr");
      tr.className = "data-row";
      var nameCell = esc(r.name) + (isNotified ? '<span class="notified-tag">已於 ' + esc(isNotified.date) + " 通知</span>" : "");
      tr.innerHTML =
        '<td onclick="event.stopPropagation()"><input type="checkbox" class="notify-box" data-id="' + esc(r.id) + '"></td>' +
        '<td><span class="dot ' + r.dotClass + '"></span>' + esc(r.statusLabel) + "</td>" +
        "<td>" + nameCell + "</td>" +
        '<td><span class="team-pill">' + esc(r.team) + "</span></td>" +
        "<td>" + esc(r.owner) + "</td>" +
        '<td class="days">' + esc(daysText(r)) + "</td>" +
        "<td>" + (r.amount ? esc(r.amount) : "-") + "</td>";
      var detail = document.createElement("tr");
      detail.className = "detail-row";
      var chips = "";
      if (r.productType) chips += '<span class="chip">' + esc(r.productType) + "</span>";
      if (r.source) chips += '<span class="chip">' + esc(r.source) + "</span>";
      if (r.closingDate) chips += '<span class="chip">預計成交：' + esc(r.closingDate) + "</span>";
      detail.innerHTML = '<td colspan="7">' +
        "進入此階段：" + esc(r.enteredAt || "未知") +
        (r.note ? "<br>備註：" + esc(r.note) : "") +
        '<div class="chips">' + chips + "</div>" +
        '<a class="crm-link" href="' + esc(r.crmUrl) + '" target="_blank" rel="noopener">在 Zoho CRM 開啟這筆交易 &rarr;</a>' +
        "</td>";
      tr.addEventListener("click", function () { detail.classList.toggle("open"); });
      tbody.appendChild(tr);
      tbody.appendChild(detail);
    });
    document.querySelectorAll(".notify-box").forEach(function (box) {
      box.addEventListener("change", updateToolbar);
    });
    updateToolbar();
    renderLog(notified);
  }

  document.addEventListener("DOMContentLoaded", function () {
    renderTable();
    document.getElementById("notifyBtn").addEventListener("click", onSendClick);
    document.getElementById("clearCheckBtn").addEventListener("click", function () {
      document.querySelectorAll(".notify-box:checked").forEach(function (b) { b.checked = false; });
      updateToolbar();
    });
    document.getElementById("clearNotifiedBtn").addEventListener("click", function () {
      if (confirm("要清除這台瀏覽器裡記錄的「已通知」標記嗎？（只影響這台裝置，不影響實際資料）")) {
        saveNotified({});
        renderTable();
      }
    });
    document.getElementById("toggleLogBtn").addEventListener("click", function () {
      var panel = document.getElementById("notifyLogPanel");
      var open = panel.style.display !== "none";
      panel.style.display = open ? "none" : "block";
      this.textContent = open ? "查看通知記錄" : "收起通知記錄";
    });
    document.getElementById("filters").addEventListener("click", function (e) {
      var btn = e.target.closest(".chip-btn");
      if (!btn) return;
      // 沒有「全部」按鈕了：再點一次目前已選的課別，就會取消選取、回到顯示全部課別。
      if (activeTeam === btn.dataset.team) {
        activeTeam = "all";
        btn.classList.remove("active");
      } else {
        activeTeam = btn.dataset.team;
        document.querySelectorAll(".chip-btn[data-team]").forEach(function (b) { b.classList.toggle("active", b === btn); });
      }
      renderTable();
    });
    var selectAllBox = document.getElementById("selectAllBox");
    if (selectAllBox) {
      selectAllBox.addEventListener("click", function (e) {
        e.stopPropagation();
        var check = selectAllBox.checked;
        document.querySelectorAll(".notify-box").forEach(function (b) { b.checked = check; });
        updateToolbar();
      });
    }
    document.querySelectorAll("th[data-sort]").forEach(function (th) {
      th.addEventListener("click", function () {
        var key = th.dataset.sort;
        if (sortKey === key) { sortDir *= -1; } else { sortKey = key; sortDir = 1; }
        renderTable();
      });
    });
  });
})();
</script>'''


def esc(s):
    return html.escape(str(s)) if s is not None else ""


def fmt_amount(v):
    if v is None:
        return None
    try:
        return f"NT$ {int(float(v)):,}"
    except Exception:
        return str(v)


def render_html(records, generated_at):
    # 傳進來的 records 在 main() 就已經先篩過，只剩下滿足三個門檻條件（可轉派／
    # 月底檢核逾期／短期追蹤逾期）的交易，正常天數的不會出現在這份報表裡。
    stage_counter = Counter(r["Stage"] for r in records)
    teams_present = [t for t in TEAM_ORDER if any(r["業務課"] == t for r in records)]

    # 預設排序：緊急程度優先，同層再依天數從多到少（使用者點欄位標題可以在網頁上重新排序）
    sorted_records = sorted(
        records,
        key=lambda r: (
            STATUS_PRIORITY.get(r["狀態"], 9),
            -(r["工作天數"] if r["工作天數"] is not None else -1),
        ),
    )

    row_objs = []
    for d in sorted_records:
        try:
            amount_raw = float(d["金額"]) if d["金額"] not in (None, "") else 0
        except (TypeError, ValueError):
            amount_raw = 0
        priority = STATUS_PRIORITY.get(d["狀態"], 9)
        if d["狀態"] == "unknown":
            dot_class = "d-unknown"
        elif priority == 0:
            dot_class = "d-danger"
        elif priority == 1:
            dot_class = "d-warn"
        else:
            dot_class = "d-ok"
        row_objs.append({
            "id": d["id"],
            "name": d["交易名稱"] or "",
            "team": d["業務課"],
            "owner": d["業務"] or "",
            "email": d["業務Email"] or "",
            "stage": d["Stage"],
            "days": d["工作天數"],
            "daysUnit": d["天數單位"],
            "statusLabel": STATUS_LABEL[d["狀態"]],
            "dotClass": dot_class,
            "priority": priority,
            "amount": fmt_amount(d["金額"]) or "",
            "amountRaw": amount_raw,
            "productType": d["商品類別"] or "",
            "source": d["名單來源"] or "",
            "closingDate": d["預計成交日"] or "",
            "note": d["進入時間備註"] or "",
            "enteredAt": (d["進入此階段時間"] or "")[:19].replace("T", " "),
            "crmUrl": f"{ZOHO_CRM_WEB_DOMAIN}/crm/{ZOHO_CRM_ORG_ID}/tab/Deals/{d['id']}",
        })
    rows_json = json.dumps(row_objs, ensure_ascii=False)

    team_filter_buttons = "\n  ".join(
        f'<button class="chip-btn" data-team="{esc(t)}">{esc(t)}</button>' for t in teams_present
    )

    return f'''<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CYBERBIZ 名單/交易階段自動追蹤</title>
<style>
  :root{{
    --ink:#17202a; --muted:#667085; --paper:#f7f2e8; --card:#fffaf1; --line:#dfd0ba;
    --forest:#13523f; --forest2:#173d33; --moss:#6c8a48; --amber:#d58b25; --coral:#c95d3f; --sky:#336a8b;
    --moss-bg:#eef3e4; --moss-bd:#cddbb4;
    --amber-bg:#faf0da; --amber-bd:#eccf95;
    --coral-bg:#fbe8e1; --coral-bd:#e6b7a4;
    --shadow:0 20px 55px rgba(48,38,22,.14);
  }}
  *{{box-sizing:border-box;}}
  body{{margin:0;color:var(--ink);
    font-family:Verdana,"Microsoft JhengHei","Noto Sans TC",Calibri,Arial,sans-serif;line-height:1.65;
    background:
      radial-gradient(circle at 14% 8%, rgba(213,139,37,.22), transparent 25%),
      radial-gradient(circle at 85% 0%, rgba(19,82,63,.18), transparent 28%),
      linear-gradient(135deg,#fbf3e4 0%,#efe1cc 62%,#e7d8bf 100%);
  }}
  .wrap{{max-width:1000px;margin:0 auto;padding:0 0 60px;}}
  .hero{{background:linear-gradient(150deg,var(--forest) 0%,var(--forest2) 58%,#24302b 100%);
    color:#fff8ea;padding:34px 32px 28px;margin-bottom:26px; box-shadow:var(--shadow);}}
  .eyebrow{{font-size:12.5px;font-weight:800;letter-spacing:.14em;color:#f5cf82;text-transform:uppercase;margin-bottom:10px;}}
  h1{{margin:0 0 8px;font-size:22px;font-weight:800;letter-spacing:-.02em;}}
  .range{{font-size:13px;color:#e6dcc4;}}
  .stat-line{{margin-top:14px;font-size:12.5px;color:#e6dcc4;}}
  .section-pad{{padding:0 24px;}}
  .stat-grid{{display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:12px; margin:18px 24px 6px;}}
  .stat-box{{background:rgba(255,250,241,.92); border:1px solid var(--line); border-radius:16px; padding:14px 16px; text-align:center; box-shadow:var(--shadow);}}
  .stat-box .num{{font-size:22px; font-weight:800; color:var(--forest);}}
  .stat-box .lbl{{font-size:11.5px; color:var(--muted); margin-top:4px;}}
  .footer-note{{margin:24px 24px 0; font-size:12px; color:var(--muted); text-align:center;}}

  .notify-toolbar{{display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin:16px 24px 10px;
    background:rgba(255,250,241,.92); border:1px solid var(--line); border-radius:16px; padding:12px 16px; box-shadow:var(--shadow);}}
  .notify-toolbar button{{border:0; border-radius:999px; padding:9px 16px; font-weight:800; cursor:pointer; font-family:inherit; font-size:12.5px;}}
  .notify-btn-primary{{background:var(--forest); color:#fff8ea;}}
  .notify-btn-primary:disabled{{background:#c9c2b3; cursor:not-allowed;}}
  .notify-btn-secondary{{background:#efe2c8; color:var(--forest);}}
  .notify-count{{font-size:12.5px; color:var(--muted); margin-left:auto;}}

  .notify-log-panel{{margin:0 24px 16px; background:rgba(255,250,241,.95); border:1px solid var(--line);
    border-radius:16px; padding:14px 16px; box-shadow:var(--shadow);}}
  .notify-log-title{{font-weight:800; color:var(--forest); font-size:13.5px; margin-bottom:8px;}}
  .notify-log-empty{{font-size:12.5px; color:var(--muted); font-style:italic;}}
  .notify-log-scroll{{overflow-x:auto;}}
  .notify-log-table{{width:100%; border-collapse:collapse; font-size:12px; min-width:640px;}}
  .notify-log-table th{{text-align:left; color:var(--forest); border-bottom:2px solid var(--line); padding:6px 8px; white-space:nowrap;}}
  .notify-log-table td{{border-bottom:1px solid var(--line); padding:6px 8px; word-break:break-word;}}

  .filters{{display:flex; flex-wrap:wrap; gap:8px; margin:0 24px 14px;}}
  .chip-btn{{border:1px solid var(--line); background:#fffdf8; color:var(--forest); font-size:12px; font-weight:700;
    padding:7px 14px; border-radius:999px; cursor:pointer; font-family:inherit;}}
  .chip-btn.active{{background:var(--forest); color:#fff8ea; border-color:var(--forest);}}

  .sticky-panel{{position:sticky; top:0; z-index:40; background:var(--paper);
    padding-top:14px; padding-bottom:2px; box-shadow:0 10px 16px -6px rgba(48,38,22,.22);}}
  .sticky-panel .stat-grid{{margin-top:0;}}

  .table-wrap{{margin:0 24px; overflow-x:auto; border-radius:16px; box-shadow:var(--shadow);}}
  table{{width:100%; border-collapse:collapse; background:var(--card); min-width:680px;}}
  thead th{{text-align:left; font-size:11px; color:#e6dcc4; background:linear-gradient(150deg,var(--forest) 0%,var(--forest2) 100%); padding:10px 12px; cursor:pointer; user-select:none; white-space:nowrap;}}
  thead th:hover{{text-decoration:underline;}}
  thead th.no-sort{{cursor:default;}}
  thead th.no-sort:hover{{text-decoration:none;}}
  tbody tr.data-row{{border-bottom:1px solid var(--line); cursor:pointer;}}
  tbody tr.data-row:hover{{background:#fff4e0;}}
  tbody td{{padding:10px 12px; font-size:12.5px; vertical-align:middle;}}
  .dot{{display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:7px; vertical-align:middle;}}
  .dot.d-danger{{background:var(--coral);}}
  .dot.d-warn{{background:var(--amber);}}
  .dot.d-ok{{background:var(--moss);}}
  .dot.d-unknown{{background:#b4b2a9;}}
  .team-pill{{font-size:10.5px; padding:2px 8px; border-radius:999px; background:var(--moss-bg); color:#3e5228; border:1px solid var(--moss-bd); white-space:nowrap;}}
  .days{{font-weight:700;}}
  .notified-tag{{display:block; margin-top:3px; font-size:10px; color:var(--forest); font-weight:700;
    background:var(--moss-bg); border:1px solid var(--moss-bd); border-radius:999px; padding:1px 8px; width:fit-content;}}
  .detail-row td{{background:#fff8ea; font-size:12px; color:var(--muted); padding:10px 16px 14px;}}
  .detail-row{{display:none;}}
  .detail-row.open{{display:table-row;}}
  .chips{{display:flex; flex-wrap:wrap; gap:6px; margin-top:6px;}}
  .chip{{font-size:11px; padding:3px 9px; border-radius:999px; background:#fff; border:1px solid var(--line); color:var(--ink);}}
  .crm-link{{color:var(--forest); font-weight:700; text-decoration:underline; display:inline-block; margin-top:6px;}}
  .notify-box{{width:15px; height:15px; cursor:pointer; accent-color:var(--forest);}}
</style>
</head>
<body>
<div class="hero">
  <div class="eyebrow">CYBERBIZ Sales Ops · 自動化追蹤</div>
  <h1>公司名單 / 本月有機會 / 短期追蹤 階段追蹤</h1>
  <div class="range">只列出 ROSTER 名單裡的業務（一課～四課＋POS＋Ambrose），且只列出已經超過門檻、需優先處理的交易（正常天數的不顯示）｜上方可依課別篩選，點欄位標題可排序｜公司名單：超過 {COMPANY_LIST_ESCALATE_DAY} 個工作天可轉派｜本月有機會：超過 {OPPORTUNITY_ENDCHECK_DAY} 個工作天月底檢核逾期｜短期追蹤：超過 {SHORT_TERM_ESCALATE_DAYS} 天逾期</div>
  <div class="stat-line">共 {len(records)} 筆需優先處理（公司名單 {stage_counter.get("公司名單",0)} 筆、本月有機會 {stage_counter.get("本月有機會",0)} 筆、短期追蹤 {stage_counter.get("短期追蹤",0)} 筆）｜每日自動更新，最後更新：{generated_at}</div>
</div>
<div class="sticky-panel">
  <div class="stat-grid">
    <div class="stat-box"><div class="num">{len(records)}</div><div class="lbl">需優先處理總筆數</div></div>
    <div class="stat-box"><div class="num">{stage_counter.get("公司名單",0)}</div><div class="lbl">公司名單可轉派</div></div>
    <div class="stat-box"><div class="num">{stage_counter.get("本月有機會",0)}</div><div class="lbl">本月有機會月底逾期</div></div>
    <div class="stat-box"><div class="num">{stage_counter.get("短期追蹤",0)}</div><div class="lbl">短期追蹤逾期</div></div>
  </div>
  <div class="notify-toolbar">
    <button class="notify-btn-primary" id="notifyBtn" disabled>寄送提醒信（已勾選 0 筆）</button>
    <button class="notify-btn-secondary" id="clearCheckBtn">清除勾選</button>
    <button class="notify-btn-secondary" id="toggleLogBtn">查看通知記錄</button>
    <button class="notify-btn-secondary" id="clearNotifiedBtn">清除本機已通知記錄</button>
    <span class="notify-count" id="notifyHint">勾選左側框，可以一次對多位不同業務寄出各自的提醒信（每人一封，只列出他自己被勾選的交易）。「已通知」標記跟通知記錄都只存在這台瀏覽器裡，換裝置不會同步。</span>
  </div>
  <div class="filters" id="filters">
    {team_filter_buttons}
  </div>
</div>
<div class="notify-log-panel" id="notifyLogPanel" style="display:none">
  <div class="notify-log-title">通知記錄（存在這台瀏覽器裡）</div>
  <div class="notify-log-empty" id="notifyLogEmpty">目前還沒有任何通知記錄。</div>
  <div class="notify-log-scroll">
    <table class="notify-log-table">
      <thead><tr><th>日期</th><th>業務</th><th>Email</th><th>交易名稱</th><th>階段</th><th>工作天數</th><th>狀態</th></tr></thead>
      <tbody id="notifyLogBody"></tbody>
    </table>
  </div>
</div>
<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th class="no-sort"><input type="checkbox" id="selectAllBox" title="全選目前畫面上的交易"></th>
        <th data-sort="priority">狀態</th>
        <th data-sort="name">交易名稱</th>
        <th data-sort="team">課別</th>
        <th data-sort="owner">業務</th>
        <th data-sort="days">工作天數</th>
        <th data-sort="amountRaw">金額</th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>
</div>
<div class="wrap">
  <div class="footer-note">工作天／短期追蹤天數計算目前只排除週六、週日（工作天）或用日曆天數（短期追蹤），尚未納入國定假日／補班日（v1）。資料來源：Zoho CRM Deals 模組，由 GitHub Actions 每日排程自動更新。</div>
</div>
<script>var REPORT_ROWS = {rows_json};</script>
''' + REPORT_SCRIPT + '''
</body>
</html>'''


# ---------------------------------------------------------------------------
# Gmail API：自動幫「可轉派／月底檢核」的交易建立提醒信草稿（存在寄件者自己的 Gmail 草稿匣，
# 不會自動送出）。三個 GMAIL_* secret 沒設定的話，這整個功能會自動跳過，report 照常產生。
#
# 未來如果要改成「不用人工按送出，系統自己直接寄出」：
#   1. Google Cloud OAuth 同意畫面的 Gmail API 授權範圍要從 gmail.compose 改成 gmail.send
#      （或兩個都加），並重新走一次授權流程拿新的 refresh token。
#   2. create_gmail_draft() 呼叫的網址從 .../drafts 改成 .../messages/send，
#      body 一樣是 {"raw": ...}，不需要再包一層 "message"。
#   其餘（HTML 信件內容、分組邏輯、避免重複寄送的紀錄檔）都不用改，是同一套。
# ---------------------------------------------------------------------------
def get_gmail_access_token():
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": GMAIL_CLIENT_ID,
            "client_secret": GMAIL_CLIENT_SECRET,
            "refresh_token": GMAIL_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"換 Gmail access token 失敗：{data}")
    return data["access_token"]


def load_draft_log():
    if os.path.exists(DRAFT_LOG_PATH):
        with open(DRAFT_LOG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_draft_log(log):
    os.makedirs(os.path.dirname(DRAFT_LOG_PATH), exist_ok=True)
    with open(DRAFT_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)


def build_notify_html(owner_name, deals):
    """組出提醒信的 HTML 內容：Verdana 字體、階段資訊粗體、交易名稱本身是超連結。"""
    items_html = []
    for d in deals:
        days_txt = "天數未知" if d["工作天數"] is None else f'已 {d["工作天數"]} {d["天數單位"]}'
        crm_url = f"{ZOHO_CRM_WEB_DOMAIN}/crm/{ZOHO_CRM_ORG_ID}/tab/Deals/{d['id']}"
        items_html.append(
            '<li style="margin-bottom:8px;">'
            f'<a href="{esc(crm_url)}" style="color:#173d33;">{esc(d["交易名稱"] or "")}</a>'
            f'　<b>（{esc(d["Stage"])}，{esc(days_txt)}，{esc(STATUS_LABEL[d["狀態"]])}）</b>'
            '</li>'
        )
    return f'''<div style="font-family:Verdana,'Microsoft JhengHei',Arial,sans-serif;font-size:14px;line-height:1.7;color:#17202a;">
  <p>{esc(owner_name)} 你好，</p>
  <p>以下 {len(deals)} 筆交易目前停留在原階段已經一段時間，麻煩抽空看一下，更新最新進度或判斷結果：</p>
  <ul style="padding-left:20px;">
    {"".join(items_html)}
  </ul>
  <p>如果其實已經處理了、只是系統還沒更新，也麻煩補填一下最新狀態，避免被誤判成沒進度。</p>
  <p>謝謝！</p>
</div>'''


def build_manager_summary_html(deals):
    """公司名單可轉派彙整信：跨業務、跨課別，寄給主管評估是否轉派。
    依課別（TEAM_ORDER 的順序）分組，同課別裡再依業務姓名排序，
    同一人底下的交易再依天數（多到少）排序，用表格呈現。"""
    groups = defaultdict(list)
    for d in deals:
        groups[(d["業務課"], d["業務"] or "")].append(d)

    def team_sort_key(team):
        return TEAM_ORDER.index(team) if team in TEAM_ORDER else len(TEAM_ORDER)

    sections_html = []
    # 先依課別（照 TEAM_ORDER 的順序：業務一~四課、POS、Ambrose），同課別裡再依業務姓名排序。
    for (team, owner), owner_deals in sorted(groups.items(), key=lambda kv: (team_sort_key(kv[0][0]), kv[0][1])):
        owner_deals = sorted(
            owner_deals,
            key=lambda d: -(d["工作天數"] if d["工作天數"] is not None else -1),
        )
        rows_html = []
        for d in owner_deals:
            days_txt = "天數未知" if d["工作天數"] is None else f'{d["工作天數"]} {d["天數單位"]}'
            crm_url = f"{ZOHO_CRM_WEB_DOMAIN}/crm/{ZOHO_CRM_ORG_ID}/tab/Deals/{d['id']}"
            rows_html.append(
                "<tr>"
                f'<td style="padding:6px 10px;border-bottom:1px solid #eee;">'
                f'<a href="{esc(crm_url)}" style="color:#173d33;">{esc(d["交易名稱"] or "")}</a></td>'
                f'<td style="padding:6px 10px;border-bottom:1px solid #eee;">{esc(d["Stage"])}</td>'
                f'<td style="padding:6px 10px;border-bottom:1px solid #eee;">{esc(days_txt)}</td>'
                f'<td style="padding:6px 10px;border-bottom:1px solid #eee;">{esc(STATUS_LABEL[d["狀態"]])}</td>'
                "</tr>"
            )
        sections_html.append(f'''
  <p style="margin-bottom:6px;"><b>{esc(team)} {esc(owner) or "（無業務資料）"}</b>（{len(owner_deals)} 筆）</p>
  <table style="border-collapse:collapse;width:100%;margin-bottom:18px;font-size:13px;">
    <thead>
      <tr style="background:#f7f2e8;">
        <th style="text-align:left;padding:6px 10px;border-bottom:2px solid #dfd0ba;">交易名稱</th>
        <th style="text-align:left;padding:6px 10px;border-bottom:2px solid #dfd0ba;">階段</th>
        <th style="text-align:left;padding:6px 10px;border-bottom:2px solid #dfd0ba;">天數</th>
        <th style="text-align:left;padding:6px 10px;border-bottom:2px solid #dfd0ba;">規則</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows_html)}
    </tbody>
  </table>''')

    return f'''<div style="font-family:Verdana,'Microsoft JhengHei',Arial,sans-serif;font-size:14px;line-height:1.7;color:#17202a;">
  <p>Ambrose 你好，</p>
  <p>以下 {len(deals)} 筆「公司名單」交易已經超過 {COMPANY_LIST_ESCALATE_DAY} 個工作天沒有更新，麻煩幫忙評估是否需要轉派給其他業務（依課別、業務姓名排序）：</p>
  {"".join(sections_html)}
  <p>如果已經處理過（例如已判斷有效/無效、或已經聯繫客戶），也麻煩請對應業務補填最新狀態，避免重複出現在這份清單。</p>
  <p>謝謝！</p>
</div>'''


def build_mime_message(to_email, subject, html_body):
    msg = EmailMessage()
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content("這封信包含 HTML 格式，請用支援 HTML 的信箱檢視。")
    msg.add_alternative(html_body, subtype="html")
    return msg


def create_gmail_draft(token, msg):
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    resp = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"message": {"raw": raw}},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def should_send_reminder(entry, current_status, now, repeat_days=REMIND_REPEAT_DAYS):
    """
    判斷這筆交易現在要不要（再）建立一封提醒草稿。
    - 之前從沒建過草稿：要建。
    - 狀態變了（例如本月有機會從月中檢核變月底檢核）：一定要重新建一次，不管天數。
    - 狀態沒變，但距離上次建立草稿已經超過 repeat_days 天，業務還是沒處理：重複提醒一次。
    - 狀態沒變、也還沒到重複提醒的天數：不用再建，避免同一件事每天疲勞轟炸。
    repeat_days 預設是業務個人提醒信用的 REMIND_REPEAT_DAYS（7天）；
    主管彙整信另外傳 MANAGER_SUMMARY_REMIND_REPEAT_DAYS（2天），間隔比較短。
    """
    if not entry:
        return True
    if entry.get("status") != current_status:
        return True
    drafted_at = entry.get("drafted_at")
    if not drafted_at:
        return True
    try:
        last_dt = datetime.fromisoformat(drafted_at)
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=TAIPEI_TZ)
    days_since = (now - last_dt).total_seconds() / 86400
    return days_since >= repeat_days


def run_auto_notify(records):
    if not GMAIL_AUTO_NOTIFY_ENABLED:
        print("尚未設定 GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN，略過自動建立提醒草稿功能。")
        return

    log = load_draft_log()
    now = datetime.now(TAIPEI_TZ)
    by_owner = defaultdict(list)
    for r in records:
        if r["狀態"] not in URGENT_STATUSES:
            continue
        entry = log.get(r["id"])
        if not should_send_reminder(entry, r["狀態"], now):
            continue  # 狀態沒變，而且還沒到重複提醒的天數，不用再建
        by_owner[(r["業務Email"], r["業務"])].append(r)

    if not by_owner:
        print("沒有新的需優先處理交易需要建立提醒草稿。")
        return

    try:
        token = get_gmail_access_token()
    except requests.exceptions.RequestException as e:
        print(f"換 Gmail access token 失敗，本次略過自動草稿功能（{e.__class__.__name__}）：{e}")
        return

    created = 0
    now_iso = datetime.now(TAIPEI_TZ).isoformat()
    for (owner_email, owner_name), deals in by_owner.items():
        if not owner_email:
            print(f"{owner_name} 沒有 email，略過自動草稿，請手動聯絡。")
            continue
        subject = f"【ZOHO交易階段提醒】{owner_name} 你有 {len(deals)} 筆交易需要更新進度"
        html_body = build_notify_html(owner_name, deals)
        msg = build_mime_message(owner_email, subject, html_body)
        try:
            result = create_gmail_draft(token, msg)
        except requests.exceptions.RequestException as e:
            print(f"幫 {owner_name} 建立提醒草稿失敗（{e.__class__.__name__}）：{e}")
            continue
        created += 1
        for d in deals:
            log[d["id"]] = {"status": d["狀態"], "drafted_at": now_iso, "draft_id": result.get("id")}

    save_draft_log(log)
    print(f"本次共建立 {created} 封提醒草稿，存在寄件者 Gmail 帳號的草稿匣，需要手動確認後送出。")


def run_manager_summary(records):
    """公司名單超過門檻（escalate）的交易，額外彙整成一封信給主管評估轉派，跟業務個人通知分開追蹤。"""
    if not GMAIL_AUTO_NOTIFY_ENABLED:
        return

    log = load_draft_log()
    manager_log = log.get(MANAGER_SUMMARY_LOG_KEY, {})
    now = datetime.now(TAIPEI_TZ)
    pending = []
    for r in records:
        if r["狀態"] != "escalate":
            continue
        entry = manager_log.get(r["id"])
        if not should_send_reminder(entry, r["狀態"], now, repeat_days=MANAGER_SUMMARY_REMIND_REPEAT_DAYS):
            continue
        pending.append(r)

    if not pending:
        print("沒有新的公司名單可轉派交易需要彙整給主管。")
        return

    try:
        token = get_gmail_access_token()
    except requests.exceptions.RequestException as e:
        print(f"換 Gmail access token 失敗，本次略過主管彙整草稿（{e.__class__.__name__}）：{e}")
        return

    subject = f"【ZOHO交易階段提醒】公司名單可轉派彙整（共 {len(pending)} 筆）"
    html_body = build_manager_summary_html(pending)
    msg = build_mime_message(MANAGER_SUMMARY_EMAIL, subject, html_body)
    try:
        result = create_gmail_draft(token, msg)
    except requests.exceptions.RequestException as e:
        print(f"建立主管彙整草稿失敗（{e.__class__.__name__}）：{e}")
        return

    now_iso = datetime.now(TAIPEI_TZ).isoformat()
    for r in pending:
        manager_log[r["id"]] = {"status": r["狀態"], "drafted_at": now_iso, "draft_id": result.get("id")}
    log[MANAGER_SUMMARY_LOG_KEY] = manager_log
    save_draft_log(log)
    print(f"已建立主管彙整草稿，共 {len(pending)} 筆公司名單可轉派交易，收件人 {MANAGER_SUMMARY_EMAIL}。")


def main():
    token = get_access_token()
    rows = fetch_current_deals(token)
    cache = load_cache()
    records, new_cache, api_calls = build_records(token, rows, cache)
    save_cache(new_cache)

    # 快取／自動草稿仍然要看「全部」記錄（追蹤中但天數還沒到門檻的也要一起處理，
    # 這樣快取才不會漏、之後一旦超過門檻才能馬上被抓到），但網頁報表本身只列出
    # 已經滿足三個門檻條件（可轉派／月底檢核／短期追蹤逾期）的交易，正常天數的不顯示。
    report_records = [r for r in records if r["狀態"] in URGENT_STATUSES]

    generated_at = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M (UTC+8)")
    html_out = render_html(report_records, generated_at)

    os.makedirs(os.path.dirname(OUTPUT_HTML_PATH), exist_ok=True)
    with open(OUTPUT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)

    run_auto_notify(records)
    run_manager_summary(records)

    print(f"完成。共 {len(records)} 筆，本次呼叫 Timeline API {api_calls} 次（快取命中 {len(rows) - api_calls} 筆）。")


if __name__ == "__main__":
    main()
