#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Theo dõi tỷ giá AUD và gửi Telegram khi giá thay đổi.

Bố cục tin nhắn:
  1) Tỷ giá AUD thị trường hiện tại (forex quốc tế, KHÔNG qua ngân hàng/trung gian VN).
  2) Top 5 ngân hàng mua tiền mặt cao nhất + Top 5 mua chuyển khoản cao nhất.
  3) Kết luận: mua tiền mặt cao nhất & mua chuyển khoản cao nhất.

Nguồn:
  - Thị trường : open.er-api.com (forex quốc tế, không khóa).
  - Ngân hàng  : webgia.com (gom ~37 ngân hàng) + Vietcombank (XML chính thức, ưu tiên khi trùng).
Chỉ gửi khi ngân hàng mua cao nhất (tiền mặt hoặc chuyển khoản) thay đổi.
"""

import os
import re
import sys
import json
import unicodedata
import xml.etree.ElementTree as ET
from io import StringIO
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd

CURRENCY = "AUD"
WEBGIA_URL = f"https://webgia.com/ngoai-te/{CURRENCY.lower()}/"
VCB_URL = "https://portal.vietcombank.com.vn/Usercontrols/TVPortal.TyGia/pXML.aspx?b=10"
MARKET_URL = f"https://open.er-api.com/v6/latest/{CURRENCY}"
STATE_FILE = "state.json"
GIO_VN = timezone(timedelta(hours=7))
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


# ===== Tiện ích =====
def chuan_hoa_ten(s):
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def fmt(gia):
    return f"{int(round(gia)):,}".replace(",", ".") + " VND"


def so_webgia(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = str(x).strip()
    if not s or s in {"-", "—"}:
        return None
    s = re.sub(r"[^\d.]", "", s.replace(".", "").replace(",", "."))
    try:
        return float(s)
    except ValueError:
        return None


def so_vcb(x):
    if not x or str(x).strip() in {"-", ""}:
        return None
    try:
        return float(str(x).replace(",", ""))
    except ValueError:
        return None


# ===== Nguồn thị trường (forex quốc tế) =====
def gia_thi_truong():
    r = requests.get(MARKET_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    d = r.json()
    if d.get("result") != "success":
        raise RuntimeError("API thị trường trả về lỗi")
    vnd = d.get("rates", {}).get("VND")
    if not vnd:
        raise RuntimeError("Không có VND trong dữ liệu thị trường")
    return float(vnd)


# ===== Nguồn ngân hàng =====
def nguon_webgia():
    resp = requests.get(WEBGIA_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    tables = pd.read_html(StringIO(resp.text))
    df = None
    for t in tables:
        cols = [" ".join(str(x) for x in c).strip() if isinstance(c, tuple) else str(c)
                for c in t.columns]
        t.columns = cols
        if "mua" in " ".join(cols).lower() and t.shape[0] >= 8:
            df = t
            break
    if df is None:
        raise RuntimeError("webgia: không tìm thấy bảng tỷ giá")
    bank_col = df.columns[0]
    mua_cols = [c for c in df.columns if "mua" in c.lower()]
    out = []
    for _, row in df.iterrows():
        bank = re.sub(r"\s+", " ", str(row[bank_col]).strip())
        if not bank or bank.lower().startswith("ngân hàng"):
            continue
        for c in mua_cols:
            gia = so_webgia(row[c])
            if gia and gia > 1000:
                loai = "tiền mặt" if "tiền mặt" in c.lower() else \
                       ("chuyển khoản" if "chuyển" in c.lower() else "mua")
                out.append({"bank": bank, "loai": loai, "gia": gia,
                            "nguon": "webgia", "uu_tien": 1})
    if not out:
        raise RuntimeError("webgia: không trích được giá")
    return out


def nguon_vietcombank():
    resp = requests.get(VCB_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    out = []
    for ex in root.findall("Exrate"):
        if ex.get("CurrencyCode") == CURRENCY:
            buy = so_vcb(ex.get("Buy"))
            tf = so_vcb(ex.get("Transfer"))
            if buy:
                out.append({"bank": "Vietcombank", "loai": "tiền mặt", "gia": buy,
                            "nguon": "VCB", "uu_tien": 2})
            if tf:
                out.append({"bank": "Vietcombank", "loai": "chuyển khoản", "gia": tf,
                            "nguon": "VCB", "uu_tien": 2})
    return out


def gom_ngan_hang():
    tat_ca = []
    for ten, fn in [("webgia", nguon_webgia), ("vietcombank", nguon_vietcombank)]:
        try:
            tat_ca.extend(fn())
        except Exception as e:
            print(f"[LỖI nguồn {ten}] {e}", file=sys.stderr)
    if not tat_ca:
        raise RuntimeError("Không lấy được dữ liệu ngân hàng nào")
    best = {}
    for r in tat_ca:
        key = (chuan_hoa_ten(r["bank"]), r["loai"])
        cu = best.get(key)
        if cu is None or r["uu_tien"] > cu["uu_tien"] or \
           (r["uu_tien"] == cu["uu_tien"] and r["gia"] > cu["gia"]):
            best[key] = r
    return list(best.values())


def soan_tin(ban_ghi, market):
    cash = sorted([r for r in ban_ghi if r["loai"] == "tiền mặt"],
                  key=lambda r: r["gia"], reverse=True)
    transfer = sorted([r for r in ban_ghi if r["loai"] == "chuyển khoản"],
                      key=lambda r: r["gia"], reverse=True)

    luc = datetime.now(GIO_VN).strftime("%H:%M %d/%m/%Y")

    # 1) Thị trường
    if market:
        tt = f"🌐 <b>Tỷ giá AUD thị trường (không qua ngân hàng):</b>\n   1 AUD ≈ {fmt(market)} (forex quốc tế, tham chiếu)\n\n"
    else:
        tt = "🌐 <b>Tỷ giá AUD thị trường:</b> (không lấy được lúc này)\n\n"

    # 2) Top 5 mỗi loại
    def liet_ke(ds):
        return "\n".join(f"{i}. {fmt(r['gia'])} — {r['bank']}"
                         for i, r in enumerate(ds[:5], 1)) or "  (không có dữ liệu)"

    body = (
        f"💵 <b>Top 5 ngân hàng MUA TIỀN MẶT cao nhất:</b>\n{liet_ke(cash)}\n\n"
        f"🏦 <b>Top 5 ngân hàng MUA CHUYỂN KHOẢN cao nhất:</b>\n{liet_ke(transfer)}\n\n"
    )

    # 3) Kết luận
    ket = "✅ <b>Kết luận:</b>\n"
    chot = {}
    if cash:
        ket += f"• Mua tiền mặt cao nhất: <b>{fmt(cash[0]['gia'])} — {cash[0]['bank']}</b>\n"
        chot["cash_bank"], chot["cash_gia"] = cash[0]["bank"], cash[0]["gia"]
    if transfer:
        ket += f"• Mua chuyển khoản cao nhất: <b>{fmt(transfer[0]['gia'])} — {transfer[0]['bank']}</b>\n"
        chot["transfer_bank"], chot["transfer_gia"] = transfer[0]["bank"], transfer[0]["gia"]

    text = f"💱 <b>Tỷ giá {CURRENCY} hôm nay</b>\n\n{tt}{body}{ket}\n🕒 {luc} · nguồn: webgia + Vietcombank"
    return text, chot


def gui_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(api, data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                      timeout=30)
    r.raise_for_status()


def doc_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def luu_state(chot):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(chot, f, ensure_ascii=False, indent=2)


def main():
    ban_ghi = gom_ngan_hang()
    try:
        market = gia_thi_truong()
    except Exception as e:
        print(f"[LỖI thị trường] {e}", file=sys.stderr)
        market = None

    text, chot = soan_tin(ban_ghi, market)

    truoc = doc_state()
    khong_doi = all(truoc.get(k) == v for k, v in chot.items()) and bool(chot)
    if khong_doi and os.environ.get("FORCE") != "1":
        print("Ngân hàng mua cao nhất không đổi -> không gửi.")
        return

    gui_telegram(text)
    luu_state(chot)
    print("Đã gửi Telegram:", chot)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("LỖI:", e, file=sys.stderr)
        sys.exit(1)
