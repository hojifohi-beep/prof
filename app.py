# -*- coding: utf-8 -*-
"""
Micro-Monopoly Finder — لوحة تحكم سحابية لاكتشاف المنتجات الرابحة (COD - الجزائر)
==========================================================================
التشغيل محلياً:   streamlit run app.py
النشر:            Streamlit Community Cloud (ضع المفاتيح في Settings -> Secrets)

ملاحظات مهمة:
- تتبع المخزون يعتمد على ما تعرضه صفحة المنتج علناً + محاكاة سلة اختيارية (طلب واحد خفيف).
  لا يتم التحايل على حماية Cloudflare: عند اكتشاف الحجب يتوقف الفحص ويُطلب إدخال يدوي.
- الاستخدام على مسؤوليتك: التزم بشروط استخدام المتاجر التي تفحصها وتجنب الطلبات المتكررة.
"""

import hmac
import json
import os
import random
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime
from urllib.parse import quote, urlparse

import pandas as pd
import requests
import streamlit as st

# ============================================================================
# 0) الإعدادات العامة
# ============================================================================
st.set_page_config(page_title="Micro-Monopoly Finder DZ", page_icon="🎯", layout="wide")

st.markdown(
    """
    <style>
    .stApp {direction: rtl;}
    .stApp p, .stApp label, .stApp h1, .stApp h2, .stApp h3, .stApp h4 {text-align: right;}
    code, pre, textarea, input[type="text"], input[type="password"], input[type="number"] {direction: ltr; text-align: left;}
    </style>
    """,
    unsafe_allow_html=True,
)

DB_PATH = "monopoly_finder.db"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# مفاتيح JSON التي قد تمثل المخزون
STOCK_KEYS = {
    "stock", "stock_quantity", "stock_qty", "inventory_quantity", "inventory",
    "quantity_in_stock", "available_quantity", "qty", "quantity",
}

# عتبات فلترة الأرقام الوهمية (تعديل يدوي مفاجئ)
MANUAL_DROP_RATIO = 0.30   # هبوط >= 30% من المخزون السابق دفعة واحدة
MANUAL_DROP_MIN_UNITS = 200  # وبمقدار لا يقل عن 200 وحدة

# أوزان معادلة المؤشر الأساسي (Base Score) — مجموعها 1.0
WEIGHTS = {"velocity": 0.30, "intent": 0.25, "ads": 0.15, "competition": 0.15, "margin": 0.15}


# ============================================================================
# 1) الأمان والمفاتيح (Security & Auth)
# ============================================================================
def get_secret(name: str):
    """قراءة مفتاح من st.secrets أو متغيرات البيئة دون تعطيل التطبيق عند غيابه."""
    try:
        value = st.secrets.get(name)
        if value:
            return str(value)
    except Exception:
        pass
    return os.environ.get(name) or None


def security_gate() -> str:
    """
    بوابة كلمة المرور + تحديد مفتاح DeepSeek.
    - إذا كان APP_PASSWORD معرّفاً ولم يُدخل الزائر قيمته الصحيحة => st.stop() فوراً.
    - يرجع مفتاح DeepSeek (من Secrets أو من الحقل اليدوي).
    """
    st.sidebar.title("🔐 الأمان والمفاتيح")
    required = get_secret("APP_PASSWORD")

    if required:
        typed = st.sidebar.text_input("كلمة مرور التطبيق", type="password")
        ok = bool(typed) and hmac.compare_digest(typed.encode("utf-8"), required.encode("utf-8"))
        if not ok:
            if typed:
                st.sidebar.error("❌ كلمة المرور غير صحيحة")
            st.title("🎯 Micro-Monopoly Finder")
            st.info("🔒 أدخل كلمة المرور من الشريط الجانبي للمتابعة.")
            st.stop()  # لا تُعرض أي بيانات ولا تُستهلك أي أرصدة API
        st.sidebar.success("✅ تم التحقق")
    else:
        st.sidebar.warning("⚠️ APP_PASSWORD غير مضبوط في Secrets — التطبيق مفتوح للجميع.")

    api_key = get_secret("DEEPSEEK_API_KEY")
    if api_key:
        st.sidebar.caption("🔑 مفتاح DeepSeek محمّل من Secrets.")
    else:
        api_key = st.sidebar.text_input("مفتاح DeepSeek API (يدوي)", type="password") or ""
        if not api_key:
            st.sidebar.caption("بدون مفتاح ستتعطل ميزات NLP والمرتجعات فقط، وبقية التطبيق يعمل.")
    return api_key


# ============================================================================
# 2) قاعدة البيانات (SQLite) + النسخ الاحتياطي
# ============================================================================
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """إنشاء الجداول إن لم تكن موجودة."""
    with closing(db_connect()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                product_name TEXT,
                url TEXT,
                platform TEXT,
                sales_per_day REAL,
                current_stock INTEGER,
                intent_ratio REAL,
                competitors INTEGER,
                ad_variants INTEGER,
                return_risk REAL,
                sell_price REAL,
                wholesale_price REAL,
                delivery_cost REAL,
                cpa REAL,
                delivery_rate REAL,
                return_cost REAL,
                breakeven_cpa REAL,
                net_profit REAL,
                margin_pct REAL,
                base_score REAL,
                final_score REAL,
                notes TEXT
            );
            CREATE TABLE IF NOT EXISTS stock_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                ts TEXT NOT NULL,
                stock INTEGER NOT NULL,
                source TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_snap_url ON stock_snapshots(url, ts);
            """
        )
        conn.commit()


def validate_sqlite_bytes(data: bytes):
    """التحقق من أن الملف المرفوع قاعدة SQLite سليمة وتحتوي جدول products."""
    if not data or not data.startswith(b"SQLite format 3\x00"):
        return False, "الملف ليس قاعدة SQLite صالحة (الترويسة غير صحيحة)."
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        with closing(sqlite3.connect(tmp_path)) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                return False, "فشل فحص سلامة قاعدة البيانات (integrity_check)."
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "products" not in tables:
                return False, "القاعدة لا تحتوي جدول products."
        return True, "الملف سليم."
    except sqlite3.Error as exc:
        return False, f"خطأ SQLite: {exc}"
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def restore_db(data: bytes):
    ok, msg = validate_sqlite_bytes(data)
    if not ok:
        return False, msg
    try:
        if os.path.exists(DB_PATH):
            shutil.copy(DB_PATH, DB_PATH + ".bak")  # نسخة أمان قبل الاستبدال
        with open(DB_PATH, "wb") as fh:
            fh.write(data)
        init_db()  # يضيف أي جداول ناقصة (مثل stock_snapshots)
        return True, "تمت استعادة قاعدة البيانات بنجاح."
    except Exception as exc:  # noqa: BLE001
        return False, f"فشلت الاستعادة: {exc}"


PRODUCT_COLUMNS = [
    "created_at", "product_name", "url", "platform", "sales_per_day", "current_stock",
    "intent_ratio", "competitors", "ad_variants", "return_risk", "sell_price",
    "wholesale_price", "delivery_cost", "cpa", "delivery_rate", "return_cost",
    "breakeven_cpa", "net_profit", "margin_pct", "base_score", "final_score", "notes",
]


def insert_product(rec: dict) -> int:
    cols = ",".join(PRODUCT_COLUMNS)
    marks = ",".join("?" for _ in PRODUCT_COLUMNS)
    with closing(db_connect()) as conn:
        cur = conn.execute(
            f"INSERT INTO products ({cols}) VALUES ({marks})",
            [rec.get(c) for c in PRODUCT_COLUMNS],
        )
        conn.commit()
        return cur.lastrowid


def load_products() -> pd.DataFrame:
    with closing(db_connect()) as conn:
        return pd.read_sql_query("SELECT * FROM products ORDER BY id DESC", conn)


def delete_product(pid: int) -> None:
    with closing(db_connect()) as conn:
        conn.execute("DELETE FROM products WHERE id = ?", (pid,))
        conn.commit()


def add_snapshot(url: str, stock: int, ts: datetime | None = None, source: str = "auto") -> None:
    ts = ts or datetime.now()
    with closing(db_connect()) as conn:
        conn.execute(
            "INSERT INTO stock_snapshots (url, ts, stock, source) VALUES (?,?,?,?)",
            (url, ts.isoformat(timespec="seconds"), int(stock), source),
        )
        conn.commit()


def get_snapshots(url: str) -> list:
    with closing(db_connect()) as conn:
        rows = conn.execute(
            "SELECT ts, stock, source FROM stock_snapshots WHERE url = ? ORDER BY ts ASC", (url,)
        ).fetchall()
    return [(datetime.fromisoformat(r["ts"]), int(r["stock"]), r["source"]) for r in rows]


# ============================================================================
# 3) محرك المخزون (Inventory Delta Engine)
# ============================================================================
def to_int(value):
    """تحويل آمن لعدد صحيح غير سالب، وإلا None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and float(value).is_integer() else None
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if cleaned.isdigit():
            return int(cleaned)
    return None


def balanced_slice(text: str, start: int, limit: int = 3_000_000):
    """استخراج كائن JSON/JS متوازن الأقواس ابتداءً من start."""
    depth, in_str, esc, quote_c = 0, False, False, ""
    end = min(len(text), start + limit)
    for i in range(start, end):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote_c:
                in_str = False
            continue
        if ch in "\"'":
            in_str, quote_c = True, ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


JS_MARKERS = [
    "window.YouCanData",
    "window.storeino.product",
    "window.storeino",
    "window.__INITIAL_STATE__",
    "window.product",
    "ShopifyAnalytics.meta",
    "var meta",
    "var product",
]


def extract_embedded_objects(html: str) -> list:
    """استخراج كائنات الجافاسكربت و ld+json المضمنة في HTML. يرجع [(label, obj)]."""
    found = []
    # (أ) كائنات window.X = {...}
    for marker in JS_MARKERS:
        for m in re.finditer(re.escape(marker) + r"\s*=\s*", html):
            pos = m.end()
            if pos < len(html) and html[pos] in "{[":
                chunk = balanced_slice(html, pos)
                if not chunk:
                    continue
                try:
                    found.append((marker, json.loads(chunk)))
                except (json.JSONDecodeError, ValueError):
                    continue  # كائن JS غير JSON صرف (مفاتيح بدون اقتباس مثلاً) — نتجاوزه
    # (ب) بيانات ld+json
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, flags=re.DOTALL | re.IGNORECASE,
    ):
        try:
            found.append(("ld+json", json.loads(m.group(1).strip())))
        except (json.JSONDecodeError, ValueError):
            continue
    # (ج) __NEXT_DATA__ (متاجر مبنية على Next.js)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, flags=re.DOTALL)
    if m:
        try:
            found.append(("__NEXT_DATA__", json.loads(m.group(1))))
        except (json.JSONDecodeError, ValueError):
            pass
    return found


def walk_stock(obj, path: str = "", out: list | None = None, depth: int = 0) -> list:
    """بحث تعاقبي عن حقول المخزون الرقمية. يرجع [(path, value)]."""
    if out is None:
        out = []
    if depth > 12:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            kl = str(k).lower()
            if kl == "inventorylevel" and isinstance(v, dict) and "value" in v:
                n = to_int(v["value"])
                if n is not None:
                    out.append((p, n))
            elif kl in STOCK_KEYS and not isinstance(v, (dict, list)):
                n = to_int(v)
                if n is not None:
                    out.append((p, n))
            else:
                walk_stock(v, p, out, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:200]):
            walk_stock(v, f"{path}[{i}]", out, depth + 1)
    return out


def aggregate_stock(found: list):
    """تجميع قراءات المخزون: مجموع المتغيرات (variants) إن وُجدت، وإلا أكبر قيمة."""
    unique = {}
    for p, n in found:
        unique[p] = n
    if not unique:
        return None
    variant_vals = [n for p, n in unique.items() if "variant" in p.lower()]
    if variant_vals:
        return sum(variant_vals)
    return max(unique.values())


def guess_product_id(objs: list):
    for _, obj in objs:
        if isinstance(obj, dict):
            for cand in (obj, obj.get("product") if isinstance(obj.get("product"), dict) else {}):
                pid = cand.get("id") if isinstance(cand, dict) else None
                if pid is not None and not isinstance(pid, (dict, list)):
                    return str(pid)
    return None


def detect_platform(html: str, url: str) -> str:
    low = html.lower()
    if "cdn.shopify.com" in low or "shopify.theme" in low or "myshopify.com" in low:
        return "Shopify"
    if "youcan" in low or "youcan.shop" in url.lower():
        return "YouCan"
    if "storeino" in low or "storeino" in url.lower():
        return "Storeino"
    return "غير معروف"


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "fr-DZ,fr;q=0.9,ar;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    })
    return s


def is_blocked(resp: requests.Response) -> bool:
    head = resp.text[:5000].lower() if resp.text else ""
    if resp.status_code in (403, 503) and (
        "cf-ray" in resp.headers or "cloudflare" in resp.headers.get("server", "").lower()
        or "just a moment" in head or "cf-chl" in head
    ):
        return True
    return resp.status_code == 403


def polite_get(sess: requests.Session, url: str):
    """
    طلب GET مهذّب: تأخير عشوائي، محاولة واحدة عند 429، وتوقف فوري عند الحجب.
    يرجع (html | None, status_code | None, note).
    """
    time.sleep(random.uniform(0.8, 1.6))
    try:
        resp = sess.get(url, timeout=20, allow_redirects=True)
        if resp.status_code == 429:
            wait = min(int(resp.headers.get("Retry-After", "5") or 5), 10)
            time.sleep(wait)
            resp = sess.get(url, timeout=20, allow_redirects=True)
        if is_blocked(resp):
            return None, resp.status_code, "الموقع محمي (Cloudflare/403) — تم الإيقاف دون محاولة تجاوز. أدخل المخزون يدوياً."
        if resp.status_code >= 400:
            return None, resp.status_code, f"استجابة غير ناجحة: HTTP {resp.status_code}"
        return resp.text, resp.status_code, "ok"
    except requests.RequestException as exc:
        return None, None, f"خطأ شبكة: {exc}"


REMAINING_PATTERNS = [
    r"(?:only|seulement|maximum|max|available|disponible|stock|remaining|restant|متبقي|المتوفر|المتاح|متاح|الكمية)\D{0,30}?(\d{1,7})",
    r"(\d{1,7})\s*(?:left|restant|restants|disponibles?|متبقية|قطعة|وحدة)",
]


def parse_remaining(text: str, requested: int):
    """استخراج المخزون المتبقي من استجابة السلة (JSON أو نص)."""
    try:
        data = json.loads(text)
        vals = [n for _, n in walk_stock(data) if n < requested]
        if vals:
            return max(vals)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    for pat in REMAINING_PATTERNS:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            n = int(m.group(1))
            if 0 <= n < requested:
                return n
    return None


def probe_shopify(sess, url, qty):
    """محاكاة سلة Shopify: طلب كمية كبيرة وقراءة رسالة الخطأ، ثم تفريغ السلة إن قُبل الطلب."""
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    m = re.search(r"/products/([^/?#]+)", parsed.path)
    if not m:
        return {"ok": False, "note": "رابط Shopify لا يحتوي /products/<handle>."}
    time.sleep(random.uniform(0.8, 1.4))
    r = sess.get(f"{origin}/products/{m.group(1)}.js", timeout=20)
    if r.status_code != 200:
        return {"ok": False, "note": f"تعذر جلب بيانات المنتج (HTTP {r.status_code})."}
    variants = r.json().get("variants", [])
    if not variants:
        return {"ok": False, "note": "لا توجد متغيرات للمنتج."}
    vid = variants[0]["id"]
    time.sleep(random.uniform(0.8, 1.4))
    resp = sess.post(
        f"{origin}/cart/add.js",
        json={"items": [{"id": vid, "quantity": qty}]},
        headers={"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
        timeout=20,
    )
    if is_blocked(resp):
        return {"ok": False, "note": "المحاكاة محجوبة (Cloudflare) — تم الإيقاف."}
    remaining = parse_remaining(resp.text, qty)
    if resp.status_code == 200:
        try:  # تنظيف: إن قُبلت الكمية فالمخزون غير متتبَّع؛ نفرّغ السلة
            sess.post(f"{origin}/cart/clear.js", timeout=15)
        except requests.RequestException:
            pass
        return {"ok": True, "remaining": None, "note": "قُبلت الكمية الكبيرة => المخزون غير متتبَّع أو البيع مسموح عند النفاد.", "raw": resp.text[:300]}
    return {"ok": remaining is not None, "remaining": remaining, "note": f"HTTP {resp.status_code}", "raw": resp.text[:300]}


def probe_custom(sess, endpoint, template, product_id, qty):
    """محاكاة سلة لمنصات أخرى (YouCan/Storeino) عبر Endpoint يحدده المستخدم من DevTools."""
    if not endpoint or not product_id:
        return {"ok": False, "note": "أدخل Endpoint السلة ومعرّف المنتج في الإعدادات المتقدمة."}
    try:
        payload = json.loads(template.replace("{product_id}", str(product_id)).replace("{qty}", str(qty)))
    except (json.JSONDecodeError, ValueError):
        return {"ok": False, "note": "قالب JSON غير صالح."}
    time.sleep(random.uniform(0.8, 1.4))
    resp = sess.post(endpoint, json=payload, timeout=20)
    if is_blocked(resp):
        return {"ok": False, "note": "المحاكاة محجوبة (Cloudflare) — تم الإيقاف."}
    remaining = parse_remaining(resp.text, qty)
    return {"ok": remaining is not None, "remaining": remaining, "note": f"HTTP {resp.status_code}", "raw": resp.text[:300]}


def scan_product(url, platform_hint, do_probe, qty, endpoint, template, pid_override) -> dict:
    """الفحص الكامل: HTML + كائنات JS + محاكاة السلة (اختيارية)."""
    result = {"url": url, "platform": None, "embedded": [], "embedded_stock": None,
              "probe": None, "stock": None, "stock_source": None, "notes": []}
    sess = make_session()
    html, status, note = polite_get(sess, url)
    if html is None:
        result["notes"].append(note)
        return result

    platform = detect_platform(html, url) if platform_hint == "تلقائي" else platform_hint
    result["platform"] = platform

    objs = extract_embedded_objects(html)
    found = []
    for label, obj in objs:
        for path, n in walk_stock(obj):
            found.append((f"{label}:{path}", n))
    result["embedded"] = found
    result["embedded_stock"] = aggregate_stock(found)
    if not objs:
        result["notes"].append("لم يتم العثور على كائنات JS/JSON مضمنة قابلة للقراءة.")

    if do_probe:
        try:
            if platform == "Shopify":
                result["probe"] = probe_shopify(sess, url, qty)
            else:
                pid = pid_override or guess_product_id(objs)
                result["probe"] = probe_custom(sess, endpoint, template, pid, qty)
        except Exception as exc:  # noqa: BLE001
            result["probe"] = {"ok": False, "note": f"فشلت المحاكاة: {exc}"}

    probe = result["probe"] or {}
    if probe.get("remaining") is not None:
        result["stock"], result["stock_source"] = probe["remaining"], "محاكاة السلة"
    elif result["embedded_stock"] is not None:
        result["stock"], result["stock_source"] = result["embedded_stock"], "كائنات مضمنة"
    return result


def compute_sales_velocity(snaps: list) -> dict:
    """
    حساب سرعة المبيعات من اللقطات المتتالية مع فلترة الأرقام الوهمية:
    - ارتفاع المخزون => إعادة تعبئة => يُتجاهل الفاصل.
    - هبوط مفاجئ كبير (>= 30% و>= 200 وحدة) => تعديل يدوي مشبوه => يُتجاهل.
    - هبوط تدريجي منتظم => مبيعات حقيقية.
    """
    out = {"sales_per_day": None, "units_sold": 0, "days": 0.0, "ignored": [], "intervals": 0}
    if len(snaps) < 2:
        return out
    sold, valid_secs = 0, 0.0
    for (t0, s0, _), (t1, s1, _) in zip(snaps, snaps[1:]):
        dt = (t1 - t0).total_seconds()
        if dt <= 0:
            continue
        drop = s0 - s1
        if drop < 0:
            out["ignored"].append((t1, f"إعادة تعبئة (+{-drop})"))
            continue
        if s0 > 0 and drop >= MANUAL_DROP_MIN_UNITS and drop / s0 >= MANUAL_DROP_RATIO:
            out["ignored"].append((t1, f"تعديل يدوي مشبوه (-{drop} من {s0})"))
            continue
        sold += drop
        valid_secs += dt
        out["intervals"] += 1
    days = valid_secs / 86400
    out.update({"units_sold": sold, "days": days})
    if days > 0:
        out["sales_per_day"] = sold / days
    return out


# ============================================================================
# 4) DeepSeek: نية الشراء + مخاطر المرتجعات
# ============================================================================
class ApiError(Exception):
    pass


def call_deepseek(api_key: str, system_prompt: str, user_text: str) -> dict:
    if not api_key:
        raise ApiError("لا يوجد مفتاح DeepSeek API.")
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text[:12000]},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    try:
        r = requests.post(
            DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
    except requests.RequestException as exc:
        raise ApiError(f"خطأ اتصال: {exc}") from exc
    if r.status_code != 200:
        raise ApiError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        content = r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise ApiError("بنية استجابة غير متوقعة.") from exc
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise ApiError("تعذر تحليل JSON من الاستجابة.")


INTENT_PROMPT = (
    "You are an expert analyst of Algerian e-commerce (Cash on Delivery). "
    "You receive customer comments written in Algerian Darija (Arabic or Latin script) and French. "
    "Examples of buying intent: 'شحال السعر لوهران', 'حاب نطلب زوج', 'كاين توصيل للبليدة', 'prix?', 'livraison wahran'. "
    "Return ONLY a JSON object with keys: "
    '"intent_ratio" (float 0..1 = share of comments expressing explicit purchase intent such as asking price, '
    'delivery to a wilaya, wanting to order), "top_questions" (array of the 3 most important customer questions, '
    'short Arabic phrases), "total_comments" (int).'
)

RETURN_PROMPT = (
    "You are an expert in return/refusal risk for Algerian Cash-on-Delivery e-commerce. "
    "You receive past complaints and reviews about a product (Darija/Arabic/French). "
    "Assess product quality complaints, damage during delivery, and size/fit problems. "
    "Return ONLY a JSON object with keys: "
    '"risk_score" (float from 1.0 = very safe to 10.0 = extreme return risk), '
    '"quality_issues" (short Arabic text), "delivery_damage" (short Arabic text), "size_issues" (short Arabic text), '
    '"rejection_reasons" (array of likely reasons a customer refuses the parcel when the courier arrives, in Arabic).'
)


def analyze_intent(api_key: str, text: str) -> dict:
    data = call_deepseek(api_key, INTENT_PROMPT, text)
    ratio = float(data.get("intent_ratio", 0))
    if ratio > 1:  # أحياناً يرجع النسبة كنسبة مئوية
        ratio /= 100
    ratio = max(0.0, min(1.0, ratio))
    qs = data.get("top_questions") or []
    qs = [str(q) for q in qs][:3]
    return {"intent_ratio": ratio, "top_questions": qs, "total_comments": data.get("total_comments")}


def analyze_returns(api_key: str, text: str) -> dict:
    data = call_deepseek(api_key, RETURN_PROMPT, text)
    score = float(data.get("risk_score"))
    score = max(1.0, min(10.0, score))
    return {
        "risk_score": score,
        "quality_issues": data.get("quality_issues", ""),
        "delivery_damage": data.get("delivery_damage", ""),
        "size_issues": data.get("size_issues", ""),
        "rejection_reasons": [str(x) for x in (data.get("rejection_reasons") or [])],
    }


# ============================================================================
# 5) المحرك المالي + معادلة التقييم النهائي
# ============================================================================
def compute_financials(sell, wholesale, delivery, cpa, rate_pct, return_cost) -> dict:
    """
    كل الأرقام لكل طلبية مسجَّلة (Lead مؤكد):
      هامش الوحدة المستلمة   = البيع - الجملة - التوصيل
      الربح المتوقع قبل الإعلان = r * هامش الوحدة - (1-r) * خسارة الرفض
      نقطة التعادل (Break-Even CPA) = الربح المتوقع قبل الإعلان
      الربح الصافي           = الربح المتوقع قبل الإعلان - CPA
      الهامش الصافي %        = الربح الصافي / سعر البيع
    """
    r = max(0.0, min(1.0, rate_pct / 100.0))
    unit_margin = sell - wholesale - delivery
    before_ads = r * unit_margin - (1 - r) * return_cost
    net = before_ads - cpa
    margin_pct = (net / sell * 100) if sell > 0 else 0.0
    if net <= 0:
        label = "🔴 خاسر"
    elif margin_pct < 10:
        label = "🟠 هامش ضيق"
    elif margin_pct < 25:
        label = "🟡 جيد"
    else:
        label = "🟢 ممتاز"
    return {
        "unit_margin": unit_margin, "breakeven_cpa": before_ads, "net_profit": net,
        "margin_pct": margin_pct, "label": label, "net_per_100": net * 100,
    }


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def base_score(sales_per_day, intent_ratio, ads, competitors, margin_pct) -> tuple:
    """المؤشر الأساسي من 0 إلى 10 (أوزان قابلة للتعديل في WEIGHTS أعلاه)."""
    comps = {
        "velocity": clamp01((sales_per_day or 0) / 20.0),          # 20 وحدة/يوم = سقف
        "intent": clamp01(intent_ratio or 0),
        "ads": clamp01((ads or 0) / 5.0),                           # 5 إعلانات نشطة = سقف
        "competition": 1.0 - clamp01((competitors or 0) / 20.0),    # قلة المنافسين = احتكار أفضل
        "margin": clamp01((margin_pct or 0) / 30.0),                # 30% هامش = سقف
    }
    score = 10 * sum(WEIGHTS[k] * v for k, v in comps.items())
    return score, comps


def final_score(base: float, risk: float | None) -> float:
    """Final = Base * (1 - 0.9 * (Risk - 1) / 9). عند غياب الخطر (غير محدد) لا يُطبَّق أي خصم."""
    r = 1.0 if risk is None else risk
    return base * (1 - 0.9 * (r - 1) / 9)


def meta_ads_link(url: str) -> str:
    return (
        "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=DZ"
        f"&q={quote(url, safe='')}&search_type=keyword_unordered&media_type=all"
    )


# ============================================================================
# 6) الواجهة
# ============================================================================
init_db()
api_key = security_gate()

# ---- الشريط الجانبي: النسخ الاحتياطي ----
st.sidebar.divider()
st.sidebar.subheader("💾 قاعدة البيانات (Ephemeral Storage)")
st.sidebar.caption("ملفات Streamlit Cloud مؤقتة وتُمسح عند إعادة التشغيل. نزّل ملف .db بعد كل جلسة وأعد رفعه لاحقاً.")
try:
    with open(DB_PATH, "rb") as fh:
        st.sidebar.download_button(
            "⬇️ تنزيل monopoly_finder.db", fh.read(),
            file_name="monopoly_finder.db", mime="application/octet-stream",
        )
except OSError as exc:
    st.sidebar.error(f"تعذر قراءة القاعدة: {exc}")

uploaded_db = st.sidebar.file_uploader("استرجاع نسخة .db", type=["db", "sqlite", "sqlite3"])
if uploaded_db is not None and st.sidebar.button("♻️ استرجاع الآن"):
    ok, msg = restore_db(uploaded_db.getvalue())
    (st.sidebar.success if ok else st.sidebar.error)(msg)
    if ok:
        st.rerun()

# ---- الرأس ----
st.title("🎯 Micro-Monopoly Finder — الجزائر (COD)")
st.caption("اكتشاف المنتجات الرابحة وحساب مؤشر الفرصة مع تقييم المخاطر المالية والمرتجعات.")

st.session_state.setdefault("results", {})
R = st.session_state["results"]

c1, c2 = st.columns(2)
product_name = c1.text_input("اسم المنتج", key="product_name")
product_url = c2.text_input("رابط المنتج / صفحة الهبوط", key="product_url", placeholder="https://store.example/products/...")
R["product_name"], R["url"] = product_name.strip(), product_url.strip()

tabs = st.tabs([
    "📦 المخزون", "📢 بصمة الإعلانات", "🗣️ نية الشراء", "↩️ مخاطر المرتجعات",
    "💰 الحاسبة المالية", "🏁 التقييم النهائي", "🗂️ السجل والتصدير",
])

# ---------------------------------------------------------------- المخزون
with tabs[0]:
    st.subheader("تتبع المخزون (Inventory Delta Engine)")
    cA, cB = st.columns([2, 1])
    platform_hint = cA.selectbox("المنصة", ["تلقائي", "Shopify", "YouCan", "Storeino"])
    do_probe = cB.checkbox("تفعيل محاكاة السلة (Cart Probe)", value=False,
                           help="طلب واحد بكمية كبيرة لقراءة رسالة الخطأ. استخدمه باعتدال.")
    with st.expander("⚙️ إعدادات متقدمة للمحاكاة (YouCan / Storeino)"):
        st.caption("Shopify يعمل تلقائياً. لمنصات أخرى افتح DevTools > Network أثناء إضافة المنتج للسلة وانسخ الـ Endpoint هنا.")
        probe_qty = st.number_input("الكمية المطلوبة في المحاكاة", 100, 99999, 9999)
        probe_endpoint = st.text_input("Endpoint إضافة السلة (POST)")
        probe_template = st.text_area("قالب JSON", value='{"product_id": "{product_id}", "quantity": {qty}}')
        probe_pid = st.text_input("معرّف المنتج (اختياري — يُخمَّن تلقائياً من الصفحة)")
    auto_snap = st.checkbox("حفظ القراءة تلقائياً كلقطة مخزون", value=True)

    if st.button("🔍 فحص المخزون الآن", type="primary", disabled=not R["url"]):
        with st.spinner("جارٍ الفحص بهدوء..."):
            try:
                scan = scan_product(R["url"], platform_hint, do_probe, int(probe_qty),
                                    probe_endpoint.strip(), probe_template, probe_pid.strip())
                st.session_state["scan"] = scan
                if auto_snap and scan.get("stock") is not None:
                    add_snapshot(R["url"], scan["stock"], source=scan.get("stock_source") or "auto")
            except Exception as exc:  # noqa: BLE001
                st.error(f"فشل الفحص: {exc}")

    scan = st.session_state.get("scan")
    if scan and scan.get("url") == R["url"]:
        st.write(f"**المنصة المكتشفة:** {scan.get('platform') or '—'}")
        for n in scan["notes"]:
            st.warning(n)
        m1, m2, m3 = st.columns(3)
        m1.metric("من الكائنات المضمنة", scan["embedded_stock"] if scan["embedded_stock"] is not None else "—")
        probe = scan.get("probe")
        m2.metric("من محاكاة السلة", (probe or {}).get("remaining", "—") if probe else "غير مفعّلة")
        m3.metric("المخزون المعتمد", scan["stock"] if scan["stock"] is not None else "—", help=scan.get("stock_source") or "")
        if probe:
            st.caption(f"نتيجة المحاكاة: {probe.get('note')}")
        if scan["embedded"]:
            with st.expander("الحقول المكتشفة"):
                st.dataframe(pd.DataFrame(scan["embedded"], columns=["المسار", "القيمة"]), use_container_width=True)
        R["current_stock"] = scan["stock"]

    st.divider()
    st.markdown("**إضافة قراءة يدوية** (للمتاجر المحمية أو لإدخال سجل تاريخي)")
    d1, d2, d3 = st.columns(3)
    man_stock = d1.number_input("المخزون", min_value=0, value=0, step=1)
    man_date = d2.date_input("التاريخ")
    man_time = d3.time_input("الوقت")
    if st.button("➕ إضافة القراءة", disabled=not R["url"]):
        add_snapshot(R["url"], man_stock, datetime.combine(man_date, man_time), source="manual")
        R["current_stock"] = int(man_stock)
        st.success("تمت الإضافة.")

    snaps = get_snapshots(R["url"]) if R["url"] else []
    vel = compute_sales_velocity(snaps)
    if snaps:
        st.dataframe(pd.DataFrame(snaps, columns=["الوقت", "المخزون", "المصدر"]), use_container_width=True)
    v1, v2, v3 = st.columns(3)
    v1.metric("وحدات مباعة (صالحة)", vel["units_sold"])
    v2.metric("الأيام المحتسبة", f"{vel['days']:.2f}")
    v3.metric("سرعة المبيعات/يوم", f"{vel['sales_per_day']:.1f}" if vel["sales_per_day"] is not None else "—")
    for t, why in vel["ignored"]:
        st.info(f"🚫 تم تجاهل تغيير {t:%Y-%m-%d %H:%M}: {why}")
    override = st.number_input("تعديل يدوي لسرعة المبيعات (وحدة/يوم) — 0 = استخدم المحسوبة", 0.0, 100000.0, 0.0)
    R["sales_per_day"] = override if override > 0 else (vel["sales_per_day"] or 0.0)
    if R.get("current_stock") is None and snaps:
        R["current_stock"] = snaps[-1][1]

# ---------------------------------------------------------------- الإعلانات
with tabs[1]:
    st.subheader("بصمة الإعلانات (Meta Ads Library)")
    if R["url"]:
        st.link_button("🔗 افتح مكتبة إعلانات Meta (الجزائر)", meta_ads_link(R["url"]))
    else:
        st.info("أدخل رابط المنتج أعلاه لتوليد رابط البحث.")
    R["ad_variants"] = int(st.number_input("عدد الإعلانات النشطة (Ad Variants)", 0, 1000, 0))
    if R["ad_variants"] >= 3:
        st.success("🚀 حملة تموّل وتتوسع — 3 إعلانات نشطة أو أكثر.")
    R["competitors"] = int(st.number_input("عدد المنافسين المباشرين (تقدير يدوي)", 0, 1000, 0))

# ---------------------------------------------------------------- نية الشراء
with tabs[2]:
    st.subheader("تحليل نية الشراء بالدارجة (DeepSeek)")
    comments = st.text_area("ألصق التعليقات (تعليق في كل سطر)", height=200,
                            placeholder="شحال السعر لوهران\nحاب نطلب زوج\nكاين توصيل للبليدة؟")
    if st.button("🧠 حلّل النية"):
        if not comments.strip():
            st.warning("ألصق نصوص التعليقات أولاً.")
        else:
            with st.spinner("جارٍ التحليل..."):
                try:
                    R["intent"] = analyze_intent(api_key, comments)
                    R["intent_ratio"] = R["intent"]["intent_ratio"]
                except ApiError as exc:
                    st.error(f"فشل التحليل: {exc}")
                except (TypeError, ValueError) as exc:
                    st.error(f"استجابة غير صالحة: {exc}")
    if R.get("intent"):
        st.metric("نسبة نية الشراء الصريحة", f"{R['intent']['intent_ratio'] * 100:.0f}%")
        st.markdown("**أبرز 3 استفسارات:**")
        for q in R["intent"]["top_questions"]:
            st.write(f"- {q}")
        with st.expander("JSON"):
            st.json(R["intent"])
    else:
        st.caption("لم يُحلَّل شيء بعد — تُحتسب النية بصفر في المؤشر النهائي إلى أن تُحلَّل.")

# ---------------------------------------------------------------- المرتجعات
with tabs[3]:
    st.subheader("مؤشر مخاطر المرتجعات")
    complaints = st.text_area("ألصق الشكاوى والمراجعات السابقة", height=200)
    if st.button("⚠️ قيّم المخاطر"):
        R["risk"] = None
        R["risk_score"] = None
        if not complaints.strip():
            st.info("لا توجد نصوص — النتيجة: غير محدد (دون خصم).")
        else:
            with st.spinner("جارٍ التقييم..."):
                try:
                    R["risk"] = analyze_returns(api_key, complaints)
                    R["risk_score"] = R["risk"]["risk_score"]
                except (ApiError, TypeError, ValueError) as exc:
                    st.warning(f"تعذّر التقييم ({exc}) — النتيجة: غير محدد (دون خصم).")
    if R.get("risk"):
        st.metric("درجة خطر المرتجعات (1–10)", f"{R['risk']['risk_score']:.1f}")
        st.write(f"**الجودة:** {R['risk']['quality_issues']}")
        st.write(f"**الكسر أثناء التوصيل:** {R['risk']['delivery_damage']}")
        st.write(f"**المقاسات:** {R['risk']['size_issues']}")
        st.markdown("**أسباب الرفض المتوقعة عند وصول الموزع:**")
        for x in R["risk"]["rejection_reasons"]:
            st.write(f"- {x}")
    else:
        st.metric("درجة خطر المرتجعات", "غير محدد")

# ---------------------------------------------------------------- المالية
with tabs[4]:
    st.subheader("حاسبة المؤشرات المالية (COD)")
    f1, f2, f3, f4 = st.columns(4)
    sell = f1.number_input("سعر البيع (دج)", 0.0, 1e7, 4000.0, 50.0)
    wholesale = f2.number_input("سعر الجملة (دج)", 0.0, 1e7, 1500.0, 50.0)
    delivery = f3.number_input("متوسط تكلفة التوصيل (دج)", 0.0, 1e5, 600.0, 50.0)
    cpa = f4.number_input("CPA التقديري (دج)", 0.0, 1e5, 1000.0, 50.0)
    g1, g2 = st.columns(2)
    rate = g1.slider("نسبة الاستلام المتوقعة % (Taux de livraison)", 0, 100, 100)
    ret_cost = g2.number_input("خسارة الطرد المرفوض (Frais de Retour) دج", 0.0, 1e5, 300.0, 50.0,
                               help="كل ما تخسره عند رفض الطرد: شحن الرجوع وأي رسوم تفرضها شركة التوصيل.")
    fin = compute_financials(sell, wholesale, delivery, cpa, rate, ret_cost)
    h1, h2, h3, h4 = st.columns(4)
    h1.metric("هامش الوحدة المستلمة", f"{fin['unit_margin']:,.0f} دج")
    h2.metric("نقطة التعادل (Break-Even CPA)", f"{fin['breakeven_cpa']:,.0f} دج")
    h3.metric("الربح الصافي/طلبية", f"{fin['net_profit']:,.0f} دج")
    h4.metric("الهامش الصافي", f"{fin['margin_pct']:.1f}%")
    st.write(f"### التصنيف: {fin['label']}")
    st.caption(f"الربح الصافي لكل 100 طلبية مسجّلة ≈ {fin['net_per_100']:,.0f} دج. "
               "التصنيف: <0 خاسر، <10% ضيق، <25% جيد، غير ذلك ممتاز.")
    if fin["breakeven_cpa"] < 1200:
        st.warning("⚠️ نقطة التعادل أقل من 1200 دج — هامش الأمان ضعيف أمام تقلبات تكلفة الإعلان.")
    R.update({
        "sell_price": sell, "wholesale_price": wholesale, "delivery_cost": delivery, "cpa": cpa,
        "delivery_rate": rate, "return_cost": ret_cost, "fin": fin,
    })

# ---------------------------------------------------------------- النهائي
with tabs[5]:
    st.subheader("المؤشر النهائي للفرصة")
    fin = R.get("fin") or compute_financials(0, 0, 0, 0, 100, 0)
    base, comps = base_score(R.get("sales_per_day"), R.get("intent_ratio"), R.get("ad_variants"),
                             R.get("competitors"), fin["margin_pct"])
    risk = R.get("risk_score")
    final = final_score(base, risk)

    k1, k2, k3 = st.columns(3)
    k1.metric("المؤشر الأساسي (من 10)", f"{base:.2f}")
    k2.metric("خطر المرتجعات", f"{risk:.1f}" if risk is not None else "غير محدد")
    k3.metric("المؤشر النهائي (من 10)", f"{final:.2f}")
    if base >= 5 and risk is not None and risk >= 7:
        st.error("🚨 منتج عالي الطلب لكنه خطير جداً في المرتجعات.")
    st.progress(clamp01(final / 10))
    verdict = "ممتازة 🟢" if final >= 7 else "جيدة 🟡" if final >= 5 else "متوسطة 🟠" if final >= 3 else "ضعيفة 🔴"
    st.write(f"**التقييم:** فرصة {verdict}")

    with st.expander("تفصيل المؤشر الأساسي"):
        st.dataframe(pd.DataFrame({
            "المكوّن": ["سرعة المبيعات", "نية الشراء", "بصمة الإعلانات", "قلة المنافسة", "الهامش"],
            "القيمة (0-1)": [round(comps[k], 2) for k in ["velocity", "intent", "ads", "competition", "margin"]],
            "الوزن": [WEIGHTS[k] for k in ["velocity", "intent", "ads", "competition", "margin"]],
        }), use_container_width=True)
        st.caption("Final = Base × (1 − 0.9 × (Risk − 1) / 9). سقوف التطبيع: 20 وحدة/يوم، 5 إعلانات، 20 منافساً، 30% هامش.")

    notes = st.text_area("ملاحظات", key="notes_field")
    if st.button("💾 حفظ في قاعدة البيانات", type="primary"):
        if not R["product_name"]:
            st.warning("أدخل اسم المنتج في أعلى الصفحة.")
        else:
            try:
                rec = {
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "product_name": R["product_name"], "url": R["url"],
                    "platform": (st.session_state.get("scan") or {}).get("platform"),
                    "sales_per_day": R.get("sales_per_day"), "current_stock": R.get("current_stock"),
                    "intent_ratio": R.get("intent_ratio"), "competitors": R.get("competitors"),
                    "ad_variants": R.get("ad_variants"), "return_risk": risk,
                    "sell_price": R.get("sell_price"), "wholesale_price": R.get("wholesale_price"),
                    "delivery_cost": R.get("delivery_cost"), "cpa": R.get("cpa"),
                    "delivery_rate": R.get("delivery_rate"), "return_cost": R.get("return_cost"),
                    "breakeven_cpa": fin["breakeven_cpa"], "net_profit": fin["net_profit"],
                    "margin_pct": fin["margin_pct"], "base_score": base, "final_score": final,
                    "notes": notes,
                }
                new_id = insert_product(rec)
                st.success(f"تم الحفظ (ID = {new_id}). لا تنسَ تنزيل ملف .db من الشريط الجانبي.")
            except sqlite3.Error as exc:
                st.error(f"خطأ قاعدة البيانات: {exc}")

# ---------------------------------------------------------------- السجل
with tabs[6]:
    st.subheader("سجل المنتجات")
    try:
        df = load_products()
    except Exception as exc:  # noqa: BLE001
        df = pd.DataFrame()
        st.error(f"تعذّر تحميل السجل: {exc}")
    if df.empty:
        st.info("لا توجد سجلات بعد.")
    else:
        st.dataframe(df, use_container_width=True)
        e1, e2 = st.columns(2)
        e1.download_button("⬇️ تصدير CSV", df.to_csv(index=False).encode("utf-8-sig"),
                           file_name="monopoly_report.csv", mime="text/csv")
        e2.download_button("⬇️ تصدير JSON", df.to_json(orient="records", force_ascii=False, indent=2),
                           file_name="monopoly_report.json", mime="application/json")
        with st.expander("🗑️ حذف سجل"):
            del_id = st.selectbox("ID", df["id"].tolist())
            if st.button("حذف نهائي"):
                delete_product(int(del_id))
                st.rerun()
