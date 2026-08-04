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
"""

import os
import json
import html
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict, Counter

import requests

TAIPEI_TZ = timezone(timedelta(hours=8))

ZOHO_CLIENT_ID = os.environ["ZOHO_CLIENT_ID"]
ZOHO_CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
ZOHO_REFRESH_TOKEN = os.environ["ZOHO_REFRESH_TOKEN"]
ACCOUNTS_DOMAIN = os.environ.get("ZOHO_ACCOUNTS_DOMAIN", "https://accounts.zoho.com")
API_DOMAIN = os.environ.get("ZOHO_API_DOMAIN", "https://www.zohoapis.com")
API_VERSION = "v8"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(REPO_ROOT, "data", "stage_cache.json")
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
}
TEAM_ORDER = ["業務一課", "業務二課", "業務三課", "業務四課", "POS", "其他(非本次組織名單內)"]

STAGES_TRACKED = ["公司名單", "本月有機會"]
TRACK_SINCE = "2026-01-01T00:00:00+08:00"  # 只追蹤這個日期之後建立的交易，避免把歷史上所有卡在這兩階段的舊資料全抓進來

# 規則門檻（工作天，只排除週六週日，尚未納入國定假日 —— 之後要補請看 README）
COMPANY_LIST_REMIND_DAY = 2      # 第 2 個工作天：提醒判斷有效/無效
COMPANY_LIST_ESCALATE_DAY = 3    # 第 3 個工作天：標記可轉派
OPPORTUNITY_MIDCHECK_DAY = 11    # 月中檢核
OPPORTUNITY_ENDCHECK_DAY = 22    # 月底檢核


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


def get_timeline(token, record_id, module="Deals"):
    url = f"{API_DOMAIN}/crm/{API_VERSION}/{module}/{record_id}/__timeline"
    resp = requests.get(
        url,
        headers=zoho_headers(token),
        params={"sort_by": "audited_time", "sort_order": "desc", "include_inner_details": "true"},
        timeout=15,  # 縮短逾時時間，避免少數幾筆卡住拖慢整體（原本 30 秒，第一次全量跑會被放大很多倍）
    )
    if resp.status_code == 204:
        return []
    resp.raise_for_status()
    return resp.json().get("data", [])


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
        for change in item.get("field_history", []):
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
    return coql_query(token, query)


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

        if cached and cached.get("stage") == stage and cached.get("entered_at"):
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
        age_workdays = workdays_between(entered_dt, now) if entered_dt else None

        name, team = resolve_owner(r.get("Owner.email"), r.get("Owner.first_name"), r.get("Owner.last_name"))

        if stage == "公司名單":
            if age_workdays is None:
                status = "unknown"
            elif age_workdays >= COMPANY_LIST_ESCALATE_DAY:
                status = "escalate"
            elif age_workdays >= COMPANY_LIST_REMIND_DAY:
                status = "remind"
            else:
                status = "new"
        else:  # 本月有機會
            if age_workdays is None:
                status = "unknown"
            elif age_workdays >= OPPORTUNITY_ENDCHECK_DAY:
                status = "endcheck"
            elif age_workdays >= OPPORTUNITY_MIDCHECK_DAY:
                status = "midcheck"
            else:
                status = "tracking"

        records.append({
            "id": rid,
            "交易名稱": r.get("Deal_Name"),
            "業務": name,
            "業務Email": r.get("Owner.email") or "",
            "業務課": team,
            "Stage": stage,
            "進入此階段時間": entered_at,
            "進入時間備註": note,
            "工作天數": age_workdays,
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
    "unknown": "天數未知",
}
STATUS_CLASS = {
    "new": "st-new", "remind": "st-warn", "escalate": "st-danger",
    "tracking": "st-new", "midcheck": "st-warn", "endcheck": "st-danger",
    "unknown": "st-unknown",
}
# 卡片排序用的緊急程度：數字越小越緊急，排最前面。同一層再依工作天數從多到少排。
STATUS_PRIORITY = {
    "escalate": 0, "endcheck": 0,
    "remind": 1, "midcheck": 1,
    "new": 2, "tracking": 2,
    "unknown": 3,
}


# ---------------------------------------------------------------------------
# 勾選寄信提醒功能（純前端，mailto: 開啟本機信箱，不會自動送出，也不需要任何伺服器）
# 「已通知」標記存在瀏覽器 localStorage，只在同一台裝置/瀏覽器有效，不會跨裝置同步、
# 也不會寫回 Zoho 或 GitHub（那需要後端，目前先不做，見 README）。
# ---------------------------------------------------------------------------
NOTIFY_SCRIPT = '''<script>
(function () {
  var STORE_KEY = "cyberbiz_notified_v1";

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

  function paintNotified() {
    var map = loadNotified();
    document.querySelectorAll(".deal-card").forEach(function (card) {
      var id = card.getAttribute("data-deal-id");
      var tag = card.querySelector(".notified-tag");
      var box = card.querySelector(".notify-box");
      if (map[id]) {
        tag.style.display = "inline-block";
        tag.textContent = "已於 " + map[id].date + " 通知";
        if (box) { box.checked = false; }
      } else {
        tag.style.display = "none";
      }
    });
  }

  function updateToolbar() {
    var checked = document.querySelectorAll(".notify-box:checked");
    var btn = document.getElementById("notifyBtn");
    btn.textContent = "寄送提醒信（已勾選 " + checked.length + " 筆）";
    btn.disabled = checked.length === 0;
  }

  function buildMailto(ownerEmail, ownerName, deals) {
    var subject = "【階段提醒】" + ownerName + " 你有 " + deals.length + " 筆交易需要更新進度";
    var lines = [ownerName + " 你好，", "",
      "以下 " + deals.length + " 筆交易目前停留在原階段已經一段時間，麻煩抽空看一下，更新最新進度或判斷結果：", ""];
    deals.forEach(function (d) {
      var daysTxt = (d.days === null || d.days === undefined) ? "天數未知" : ("已 " + d.days + " 個工作天");
      lines.push("・" + d.name + "（" + d.stage + "，" + daysTxt + "，" + d.status + "）");
    });
    lines.push("", "如果其實已經處理了、只是系統還沒更新，也麻煩補填一下最新狀態，避免被誤判成沒進度。", "", "謝謝！");
    var body = lines.join("\\n");
    return "mailto:" + encodeURIComponent(ownerEmail) +
      "?subject=" + encodeURIComponent(subject) +
      "&body=" + encodeURIComponent(body);
  }

  function onSendClick() {
    var checked = Array.prototype.slice.call(document.querySelectorAll(".notify-box:checked"));
    if (checked.length === 0) return;

    var groups = {};
    checked.forEach(function (box) {
      var card = box.closest(".deal-card");
      var data = JSON.parse(card.getAttribute("data-deal"));
      var key = data.email || data.owner;
      if (!groups[key]) { groups[key] = { owner: data.owner, email: data.email, deals: [] }; }
      groups[key].deals.push(data);
    });

    var map = loadNotified();
    var keys = Object.keys(groups);
    keys.forEach(function (key, idx) {
      var g = groups[key];
      if (!g.email) {
        alert(g.owner + " 找不到 email，已略過，請手動聯絡。");
        return;
      }
      var url = buildMailto(g.email, g.owner, g.deals);
      setTimeout(function () { window.open(url, "_blank"); }, idx * 400);
      g.deals.forEach(function (d) { map[d.id] = { date: todayStr() }; });
    });
    saveNotified(map);
    paintNotified();
    updateToolbar();
  }

  document.addEventListener("DOMContentLoaded", function () {
    paintNotified();
    updateToolbar();
    document.querySelectorAll(".notify-box").forEach(function (box) {
      box.addEventListener("change", updateToolbar);
    });
    document.getElementById("notifyBtn").addEventListener("click", onSendClick);
    document.getElementById("clearCheckBtn").addEventListener("click", function () {
      document.querySelectorAll(".notify-box:checked").forEach(function (b) { b.checked = false; });
      updateToolbar();
    });
    document.getElementById("clearNotifiedBtn").addEventListener("click", function () {
      if (confirm("要清除這台瀏覽器裡記錄的「已通知」標記嗎？（只影響這台裝置，不影響實際資料）")) {
        saveNotified({});
        paintNotified();
      }
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
    by_team = defaultdict(lambda: defaultdict(list))
    for r in records:
        by_team[r["業務課"]][r["業務"]].append(r)

    stage_counter = Counter(r["Stage"] for r in records)
    escalate_count = sum(1 for r in records if r["狀態"] in ("escalate", "endcheck"))

    team_sections = []
    for team in TEAM_ORDER:
        if team not in by_team:
            continue
        owners = by_team[team]
        owners_sorted = sorted(owners.keys(), key=lambda x: x.lower())
        team_total = sum(len(v) for v in owners.values())
        team_escalate = sum(1 for v in owners.values() for d in v if d["狀態"] in ("escalate", "endcheck"))

        owner_blocks = []
        for owner in owners_sorted:
            # 先依「緊急程度」排（可轉派／月底檢核最前面），同緊急程度內再依工作天數從多到少排
            deals = sorted(
                owners[owner],
                key=lambda r: (
                    STATUS_PRIORITY.get(r["狀態"], 9),
                    -(r["工作天數"] if r["工作天數"] is not None else -1),
                ),
            )
            cards = []
            for d in deals:
                rows = []
                amt = fmt_amount(d["金額"])
                if amt:
                    rows.append(f'<div class="frow"><span class="flabel">金額</span><span class="fval">{esc(amt)}</span></div>')
                if d["商品類別"]:
                    rows.append(f'<div class="frow"><span class="flabel">商品類別</span><span class="fval">{esc(d["商品類別"])}</span></div>')
                if d["名單來源"]:
                    rows.append(f'<div class="frow"><span class="flabel">名單來源</span><span class="fval">{esc(d["名單來源"])}</span></div>')
                if d["預計成交日"]:
                    rows.append(f'<div class="frow"><span class="flabel">預計成交日</span><span class="fval">{esc(d["預計成交日"])}</span></div>')
                if d["進入時間備註"]:
                    rows.append(f'<div class="frow"><span class="flabel">備註</span><span class="fval">{esc(d["進入時間備註"])}</span></div>')
                rows_html = "\n".join(rows) if rows else '<div class="frow empty-note">其餘欄位皆未填寫</div>'
                age_txt = f'{d["工作天數"]} 個工作天' if d["工作天數"] is not None else "天數未知"
                stage_tag = "公司名單" if d["Stage"] == "公司名單" else "本月有機會"
                urgency_cls = {0: "urgent-high", 1: "urgent-mid", 2: "urgent-low"}.get(
                    STATUS_PRIORITY.get(d["狀態"], 9), "urgent-none"
                )
                notify_payload = json.dumps({
                    "id": d["id"],
                    "name": d["交易名稱"] or "",
                    "owner": d["業務"] or "",
                    "email": d["業務Email"] or "",
                    "stage": stage_tag,
                    "days": d["工作天數"],
                    "status": STATUS_LABEL[d["狀態"]],
                }, ensure_ascii=False)
                cards.append(f'''
        <div class="deal-card {urgency_cls}" data-deal-id="{esc(d["id"])}" data-deal='{esc(notify_payload)}'>
          <div class="deal-head">
            <label class="notify-check"><input type="checkbox" class="notify-box"><span></span></label>
            <span class="deal-name">{esc(d["交易名稱"])}</span>
            <span class="status-badge {STATUS_CLASS[d["狀態"]]}">{esc(STATUS_LABEL[d["狀態"]])}</span>
          </div>
          <div class="deal-date">階段：{esc(stage_tag)} ｜ 進入此階段：{esc((d["進入此階段時間"] or "")[:19].replace("T", " "))} ｜ 已 {esc(age_txt)}</div>
          <div class="notified-tag" style="display:none"></div>
          <div class="deal-body">
            {rows_html}
          </div>
        </div>''')
            owner_blocks.append(f'''
      <details class="owner-block">
        <summary>
          <span class="owner-name">{esc(owner)}</span>
          <span class="owner-count">共 {len(deals)} 筆</span>
        </summary>
        <div class="owner-deals">
          {"".join(cards)}
        </div>
      </details>''')

        team_sections.append(f'''
    <details class="team-block" open>
      <summary>
        <span class="team-name">{esc(team)}</span>
        <span class="team-count">{len(owners_sorted)} 位業務　·　共 {team_total} 筆　·　{team_escalate} 筆需優先處理</span>
      </summary>
      <div class="team-owners">
        {"".join(owner_blocks)}
      </div>
    </details>''')

    body_sections = "\n".join(team_sections)

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
  .wrap{{max-width:960px;margin:0 auto;padding:0 0 60px;}}
  .hero{{background:linear-gradient(150deg,var(--forest) 0%,var(--forest2) 58%,#24302b 100%);
    color:#fff8ea;padding:34px 32px 28px;margin-bottom:26px; box-shadow:var(--shadow);}}
  .eyebrow{{font-size:12.5px;font-weight:800;letter-spacing:.14em;color:#f5cf82;text-transform:uppercase;margin-bottom:10px;}}
  h1{{margin:0 0 8px;font-size:22px;font-weight:800;letter-spacing:-.02em;}}
  .range{{font-size:13px;color:#e6dcc4;}}
  .stat-line{{margin-top:14px;font-size:12.5px;color:#e6dcc4;}}
  .section-pad{{padding:0 24px;}}
  .stat-grid{{display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:18px 24px 6px;}}
  @media (max-width:700px){{ .stat-grid{{grid-template-columns:repeat(2,1fr);}} }}
  .stat-box{{background:rgba(255,250,241,.92); border:1px solid var(--line); border-radius:16px; padding:14px 16px; text-align:center; box-shadow:var(--shadow);}}
  .stat-box .num{{font-size:22px; font-weight:800; color:var(--forest);}}
  .stat-box .lbl{{font-size:11.5px; color:var(--muted); margin-top:4px;}}
  details.team-block{{background:var(--card); border:1px solid var(--line); border-radius:26px; margin-bottom:16px; overflow:hidden; box-shadow:var(--shadow);}}
  details.team-block > summary{{padding:16px 20px; display:flex; align-items:center; gap:12px; flex-wrap:wrap;
    background:linear-gradient(150deg,var(--forest) 0%,var(--forest2) 100%); cursor:pointer; list-style:none; user-select:none;}}
  details.team-block > summary::-webkit-details-marker{{display:none;}}
  details.team-block > summary::before{{content:"▾"; color:#fff8ea; font-size:15px; transition:transform .15s; flex-shrink:0;}}
  details.team-block:not([open]) > summary::before{{ transform:rotate(-90deg); }}
  .team-name{{font-weight:800; font-size:15.5px; color:#fff8ea;}}
  .team-count{{font-size:12px; color:#e6dcc4; margin-left:auto;}}
  .team-owners{{padding:14px 16px 4px;}}
  details.owner-block{{background:#fffdf8; border:1px solid var(--line); border-radius:18px; margin-bottom:10px; overflow:hidden;}}
  details.owner-block[open]{{border-color:var(--amber);}}
  summary{{list-style:none; cursor:pointer; padding:12px 16px; display:flex; align-items:center; gap:12px; flex-wrap:wrap; user-select:none;}}
  summary::-webkit-details-marker{{display:none;}}
  summary::before{{content:"▸"; color:var(--forest); font-size:13px; transition:transform .15s; flex-shrink:0;}}
  details[open] > summary::before{{ transform:rotate(90deg); }}
  .owner-name{{font-weight:800; font-size:14px; color:var(--forest);}}
  .owner-count{{font-size:12px; color:var(--muted); margin-left:auto;}}
  .owner-deals{{padding:0 14px 14px; display:grid; gap:10px;}}
  .deal-card{{border:1px solid var(--line); border-radius:14px; padding:12px 14px; background:#fffdf8; border-left:6px solid var(--line);}}
  .deal-card.urgent-high{{border-left-color:var(--coral);}}
  .deal-card.urgent-mid{{border-left-color:var(--amber);}}
  .deal-card.urgent-low{{border-left-color:var(--moss);}}
  .deal-head{{display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:4px;}}
  .deal-name{{font-weight:700; font-size:13px; color:var(--ink); word-break:break-word;}}
  .deal-date{{font-size:11.5px; color:var(--muted); margin-bottom:8px;}}
  .status-badge{{font-size:11px; font-weight:700; padding:2px 10px; border-radius:999px; flex-shrink:0; white-space:nowrap;}}
  .st-new{{background:var(--moss-bg); color:#3e5228; border:1px solid var(--moss-bd);}}
  .st-warn{{background:var(--amber-bg); color:#8a5a12; border:1px solid var(--amber-bd);}}
  .st-danger{{background:var(--coral-bg); color:#8a3820; border:1px solid var(--coral-bd);}}
  .st-unknown{{background:#f1ede2; color:#7c7263; border:1px solid var(--line);}}
  .deal-body{{display:grid; gap:5px;}}
  .frow{{display:flex; gap:8px; font-size:12.5px; align-items:baseline;}}
  .flabel{{flex-shrink:0; width:96px; color:var(--muted); font-weight:600;}}
  .fval{{color:var(--ink); word-break:break-word;}}
  .empty-note{{color:#9b9384; font-style:italic;}}
  .footer-note{{margin:24px 24px 0; font-size:12px; color:var(--muted); text-align:center;}}

  .notify-toolbar{{display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin:16px 24px 10px;
    background:rgba(255,250,241,.92); border:1px solid var(--line); border-radius:16px; padding:12px 16px; box-shadow:var(--shadow);}}
  .notify-toolbar button{{border:0; border-radius:999px; padding:9px 16px; font-weight:800; cursor:pointer; font-family:inherit; font-size:12.5px;}}
  .notify-btn-primary{{background:var(--forest); color:#fff8ea;}}
  .notify-btn-primary:disabled{{background:#c9c2b3; cursor:not-allowed;}}
  .notify-btn-secondary{{background:#efe2c8; color:var(--forest);}}
  .notify-count{{font-size:12.5px; color:var(--muted); margin-left:auto;}}
  .notify-check{{display:inline-flex; align-items:center; margin-right:2px; cursor:pointer;}}
  .notify-check input{{width:16px; height:16px; cursor:pointer; accent-color:var(--forest);}}
  .notified-tag{{margin:2px 0 8px; font-size:11px; color:var(--forest); font-weight:700;
    background:var(--moss-bg); border:1px solid var(--moss-bd); border-radius:999px; padding:2px 10px; display:inline-block;}}
</style>
</head>
<body>
<div class="hero">
  <div class="eyebrow">CYBERBIZ Sales Ops · 自動化追蹤</div>
  <h1>公司名單 / 本月有機會 階段追蹤</h1>
  <div class="range">依業務課分組（一課～四課＋POS）｜同一位業務底下依緊急程度排序｜公司名單規則：第 {COMPANY_LIST_REMIND_DAY}/{COMPANY_LIST_ESCALATE_DAY} 個工作天提醒／可轉派｜本月有機會規則：第 {OPPORTUNITY_MIDCHECK_DAY}/{OPPORTUNITY_ENDCHECK_DAY} 個工作天月中／月底檢核</div>
  <div class="stat-line">共 {len(records)} 筆（公司名單 {stage_counter.get("公司名單",0)} 筆、本月有機會 {stage_counter.get("本月有機會",0)} 筆）｜每日自動更新，最後更新：{generated_at}</div>
</div>
<div class="stat-grid">
  <div class="stat-box"><div class="num">{len(records)}</div><div class="lbl">追蹤中總筆數</div></div>
  <div class="stat-box"><div class="num">{stage_counter.get("公司名單",0)}</div><div class="lbl">公司名單</div></div>
  <div class="stat-box"><div class="num">{stage_counter.get("本月有機會",0)}</div><div class="lbl">本月有機會</div></div>
  <div class="stat-box"><div class="num">{escalate_count}</div><div class="lbl">已超過門檻，需優先處理</div></div>
</div>
<div class="notify-toolbar">
  <button class="notify-btn-primary" id="notifyBtn" disabled>寄送提醒信（已勾選 0 筆）</button>
  <button class="notify-btn-secondary" id="clearCheckBtn">清除勾選</button>
  <button class="notify-btn-secondary" id="clearNotifiedBtn">清除本機已通知記錄</button>
  <span class="notify-count" id="notifyHint">勾選交易卡片左上角的框，可以一次對多位不同業務寄出各自的提醒信（每人一封，只列出他自己被勾選的交易）。「已通知」標記只存在這台瀏覽器裡，換裝置不會同步。</span>
</div>
<div class="wrap">
  <div class="section-pad">
    {body_sections}
  </div>
  <div class="footer-note">工作天計算目前只排除週六、週日，尚未納入國定假日／補班日（v1）。資料來源：Zoho CRM Deals 模組，由 GitHub Actions 每日排程自動更新。</div>
</div>
''' + NOTIFY_SCRIPT + '''
</body>
</html>'''


def main():
    token = get_access_token()
    rows = fetch_current_deals(token)
    cache = load_cache()
    records, new_cache, api_calls = build_records(token, rows, cache)
    save_cache(new_cache)

    generated_at = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M (UTC+8)")
    html_out = render_html(records, generated_at)

    os.makedirs(os.path.dirname(OUTPUT_HTML_PATH), exist_ok=True)
    with open(OUTPUT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"完成。共 {len(records)} 筆，本次呼叫 Timeline API {api_calls} 次（快取命中 {len(rows) - api_calls} 筆）。")


if __name__ == "__main__":
    main()
