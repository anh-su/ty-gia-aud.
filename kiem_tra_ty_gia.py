#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Theo dõi tỷ giá AUD: tìm ngân hàng đang MUA Đô la Úc với giá cao nhất
và gửi thông báo qua Telegram MỖI KHI giá thay đổi.

Nguồn dữ liệu: https://webgia.com/ngoai-te/aud/ (tổng hợp sẵn ~37 ngân hàng).
Chạy tự động bằng GitHub Actions (xem file .github/workflows/ty-gia-aud.yml).
"""

import os
import re
import sys
import json
from io import StringIO
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd

# ----- Cấu hình -----
CURRENCY = "AUD"
URL = "https://webgia.com/ngoai-te/aud/"
STATE_FILE = "state.json"          # nơi lưu giá lần trước để so sánh
GIO_VN = timezone(timedelta(hours=7))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}


def to_number(x):
    """Chuyển '18.302' hoặc '18.846,40' -> số. Dấu chấm = phân cách nghìn."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = str(x).strip()
    if not s or s in {"-", "—"}:
        return None
    s = s.replace(".", "").replace(",", ".")   # bỏ chấm nghìn, đổi phẩy thập phân -> chấm
    s = re.sub(r"[^\d.]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def lay_bang_ty_gia():
    """Tải trang và trả về DataFrame chứa bảng tỷ giá các ngân hàng."""
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    tables = pd.read_html(StringIO(resp.text))

    for t in tables:
        # Gộp tên cột (phòng khi cột nhiều tầng) thành chuỗi 1 dòng
        cols = []
        for c in t.columns:
            if isinstance(c, tuple):
                cols.append(" ".join(str(x) for x in c).strip())
            else:
                cols.append(str(c))
        t.columns = cols
        joined = " ".join(cols).lower()
        # Bảng tỷ giá phải có cột "mua" và đủ nhiều dòng (mỗi dòng 1 ngân hàng)
        if "mua" in joined and t.shape[0] >= 8:
            return t

    raise RuntimeError("Không tìm thấy bảng tỷ giá trên trang. Có thể trang đã đổi cấu trúc.")


def phan_tich(df):
    """Trả về danh sách (ngân hàng, loại cột, giá) cho các cột MUA."""
    bank_col = df.columns[0]                          # cột đầu = tên ngân hàng
    mua_cols = [c for c in df.columns if "mua" in c.lower()]
    if not mua_cols:
        raise RuntimeError("Không thấy cột 'Mua' trong bảng.")

    ban_ghi = []
    for _, row in df.iterrows():
        bank = str(row[bank_col]).strip()
        bank = re.sub(r"\s+", " ", bank)
        if not bank or bank.lower().startswith("ngân hàng"):
            continue
        for c in mua_cols:
            gia = to_number(row[c])
            if gia and gia > 1000:                   # lọc ô rác / trống
                loai = "tiền mặt" if "tiền mặt" in c.lower() else \
                       ("chuyển khoản" if "chuyển" in c.lower() else "mua")
                ban_ghi.append({"bank": bank, "loai": loai, "gia": gia})
    if not ban_ghi:
        raise RuntimeError("Đọc được bảng nhưng không trích ra được giá nào.")
    return ban_ghi


def soan_tin(ban_ghi):
    """Tạo nội dung thông báo + bản ghi 'cao nhất' để so sánh thay đổi."""
    cao_nhat = max(ban_ghi, key=lambda r: r["gia"])

    tien_mat = [r for r in ban_ghi if r["loai"] == "tiền mặt"]
    chuyen_khoan = [r for r in ban_ghi if r["loai"] == "chuyển khoản"]

    def dong(r):
        return f"{int(round(r['gia'])):,}".replace(",", ".") + f" VND — {r['bank']}"

    luc = datetime.now(GIO_VN).strftime("%H:%M %d/%m/%Y")
    dong_top = "\n".join(
        f"{i}. {dong(r)} ({r['loai']})"
        for i, r in enumerate(
            sorted(ban_ghi, key=lambda r: r["gia"], reverse=True)[:3], start=1
        )
    )

    text = (
        f"💱 <b>Tỷ giá AUD — ngân hàng MUA cao nhất</b>\n"
        f"🏆 <b>{dong(cao_nhat)}</b> ({cao_nhat['loai']})\n\n"
        f"Top mua cao nhất:\n{dong_top}\n"
    )
    if tien_mat:
        tm = max(tien_mat, key=lambda r: r["gia"])
        text += f"\n• Mua tiền mặt cao nhất: {dong(tm)}"
    if chuyen_khoan:
        ck = max(chuyen_khoan, key=lambda r: r["gia"])
        text += f"\n• Mua chuyển khoản cao nhất: {dong(ck)}"
    text += f"\n\n🕒 {luc}\nNguồn: webgia.com"

    chot = {"bank": cao_nhat["bank"], "loai": cao_nhat["loai"], "gia": cao_nhat["gia"]}
    return text, chot


def gui_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(
        api,
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )
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
    df = lay_bang_ty_gia()
    ban_ghi = phan_tich(df)
    text, chot = soan_tin(ban_ghi)

    truoc = doc_state()
    khong_doi = (
        truoc.get("bank") == chot["bank"]
        and truoc.get("loai") == chot["loai"]
        and truoc.get("gia") == chot["gia"]
    )

    # Cho phép ép gửi để test: đặt biến môi trường FORCE=1
    force = os.environ.get("FORCE") == "1"

    if khong_doi and not force:
        print("Giá không đổi -> không gửi.")
        return

    gui_telegram(text)
    luu_state(chot)
    print("Đã gửi Telegram. Giá cao nhất:", chot)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("LỖI:", e, file=sys.stderr)
        sys.exit(1)
