# -*- coding: utf-8 -*-
"""CRO 실험 관리 프로그램 v0.1

시험 기본정보 · 군/개체 · 체중 · 임상증상 · SOP 문서 체크리스트 · 변경이력
실행: python app.py  →  브라우저에서 http://<이 PC의 IP>:8000
"""
import csv
import io
import math
import os
import re
import secrets
import sqlite3
import statistics
import tempfile
import uuid
from datetime import date, datetime
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, send_file, session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

import sop_import

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("CRO_DATA_DIR", os.path.join(BASE, "data"))
UPLOADS = os.path.join(DATA, "uploads")
DB_PATH = os.path.join(DATA, "cro.db")
os.makedirs(UPLOADS, exist_ok=True)

STATUSES = ["준비", "진행", "종료"]
DOC_STATUSES = ["미작성", "작성중", "완료", "해당없음"]
STAGES = ["시험 전", "시험 중", "시험 후"]
ANIMAL_STATUSES = ["사용중", "폐사", "제외"]
# 문서 종류의 프로그램 연계. 여러 개면 "weights,clinical"처럼 쉼표로 이어 저장한다.
LINKS = {"weights": "체중", "clinical": "임상증상"}
SEVERITIES = ["경미", "중등도", "심함"]
GROUP_KINDS = ["음성 대조군", "양성 대조군", "실험군"]
KIND_CUSTOM = "직접 입력"  # 고르면 kind_custom 칸의 글자를 군 성격으로 쓴다
GROUP_NAME_RE = re.compile(r"[0-9A-Za-z가-힣_.]{1,20}")  # 개체번호 앞에 붙으므로 공백·'-' 없이
COMMON_SIGNS = ["활동성 저하", "식욕 부진", "체중 감소", "설사", "구토", "탈수",
                "피모 불량", "호흡 이상", "기침", "콧물", "눈곱", "발열", "파행",
                "부종", "투여부위 이상", "폐사"]
STUDY_LABELS = {
    "study_no": "시험번호", "title": "실험제목", "species": "동물종",
    "n_animals": "마리수", "start_date": "시작일", "end_date": "종료일",
    "director": "시험책임자", "sponsor": "의뢰기관", "weight_unit": "체중 단위",
    "status": "상태", "note": "비고",
}
DOC_LABELS = {"status": "상태", "doc_no": "문서번호", "doc_date": "작성일",
              "owner": "담당자", "note": "비고"}

# 기본 문서 종류(예시). 관리자 화면에서 회사 SOP에 맞게 수정한다.
DEFAULT_DOC_TYPES = [
    ("시험 전", "시험의뢰서·계약서", 1, ""),
    ("시험 전", "시험계획서", 1, ""),
    ("시험 전", "동물실험윤리위원회(IACUC) 승인서", 1, ""),
    ("시험 전", "시험물질 수령·관리 기록", 1, ""),
    ("시험 전", "동물 입수·검수 기록", 1, ""),
    ("시험 전", "순화·군분리 기록", 1, ""),
    ("시험 중", "투여 기록", 1, ""),
    ("시험 중", "체중 측정 기록", 1, "weights"),
    ("시험 중", "임상증상 관찰 기록", 1, "clinical"),
    ("시험 중", "시험계획 변경·일탈 기록", 0, ""),
    ("시험 중", "부검·시료 채취 기록", 0, ""),
    ("시험 후", "결과보고서", 1, ""),
    ("시험 후", "자료 보관(이관) 기록", 1, ""),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  pw_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
  active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS studies(
  id INTEGER PRIMARY KEY, study_no TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
  species TEXT NOT NULL, n_animals INTEGER NOT NULL, start_date TEXT, end_date TEXT,
  director TEXT, sponsor TEXT, weight_unit TEXT NOT NULL DEFAULT 'g',
  status TEXT NOT NULL DEFAULT '준비', note TEXT, created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS grp(
  id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id),
  name TEXT NOT NULL, ord INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT '', note TEXT);
CREATE TABLE IF NOT EXISTS animals(
  id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id),
  group_id INTEGER NOT NULL REFERENCES grp(id), animal_no TEXT NOT NULL, sex TEXT,
  status TEXT NOT NULL DEFAULT '사용중', status_date TEXT,
  UNIQUE(study_id, animal_no));
CREATE TABLE IF NOT EXISTS weights(
  id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id),
  animal_id INTEGER NOT NULL REFERENCES animals(id), date TEXT NOT NULL,
  value REAL NOT NULL, entered_by TEXT, entered_at TEXT, UNIQUE(animal_id, date));
CREATE TABLE IF NOT EXISTS clinical(
  id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id),
  animal_id INTEGER NOT NULL REFERENCES animals(id), date TEXT NOT NULL,
  normal INTEGER NOT NULL, signs TEXT, severity TEXT, note TEXT,
  entered_by TEXT, entered_at TEXT, UNIQUE(animal_id, date));
CREATE TABLE IF NOT EXISTS doc_types(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, stage TEXT NOT NULL,
  required INTEGER NOT NULL DEFAULT 1, link TEXT NOT NULL DEFAULT '',
  ord INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
  code TEXT NOT NULL DEFAULT '', sop TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS study_docs(
  id INTEGER PRIMARY KEY, study_id INTEGER NOT NULL REFERENCES studies(id),
  doc_type_id INTEGER NOT NULL REFERENCES doc_types(id),
  status TEXT NOT NULL DEFAULT '미작성', doc_no TEXT, doc_date TEXT, owner TEXT,
  note TEXT, file_name TEXT, file_path TEXT, UNIQUE(study_id, doc_type_id));
CREATE TABLE IF NOT EXISTS study_doc_files(
  id INTEGER PRIMARY KEY, study_doc_id INTEGER NOT NULL REFERENCES study_docs(id),
  study_id INTEGER NOT NULL REFERENCES studies(id),
  file_name TEXT NOT NULL, file_path TEXT NOT NULL, note TEXT,
  uploaded_by TEXT, uploaded_at TEXT,
  hidden INTEGER NOT NULL DEFAULT 0, hidden_by TEXT, hidden_at TEXT, hidden_reason TEXT);
CREATE INDEX IF NOT EXISTS ix_doc_files ON study_doc_files(study_doc_id, id);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, user TEXT NOT NULL, study_id INTEGER,
  tbl TEXT NOT NULL, rec TEXT, action TEXT NOT NULL, field TEXT, old TEXT, new TEXT,
  reason TEXT);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE INDEX IF NOT EXISTS ix_weights_study ON weights(study_id, date);
CREATE INDEX IF NOT EXISTS ix_clinical_study ON clinical(study_id, date);
CREATE INDEX IF NOT EXISTS ix_audit_study ON audit(study_id, id);
"""


def _secret():
    p = os.path.join(DATA, "secret.key")
    if not os.path.exists(p):
        with open(p, "w") as f:
            f.write(secrets.token_hex(32))
    with open(p) as f:
        return f.read().strip()


app = Flask(__name__)
app.config.update(SECRET_KEY=_secret(), MAX_CONTENT_LENGTH=100 * 1024 * 1024,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


# ------------------------------------------------------------------ DB / 공통
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_e=None):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(doc_types)")}
    for col in ("code", "sop"):  # v0.1 DB에 서식 번호·소속 SOP 열을 더한다
        if col not in cols:
            con.execute(f"ALTER TABLE doc_types ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    gcols = {r[1] for r in con.execute("PRAGMA table_info(grp)")}
    if "kind" not in gcols:  # 군 성격·설명
        con.execute("ALTER TABLE grp ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
    if "note" not in gcols:
        con.execute("ALTER TABLE grp ADD COLUMN note TEXT")
    if con.execute("SELECT COUNT(*) FROM doc_types").fetchone()[0] == 0:
        for i, (stage, name, req, link) in enumerate(DEFAULT_DOC_TYPES):
            con.execute("INSERT INTO doc_types(name,stage,required,link,ord) VALUES(?,?,?,?,?)",
                        (name, stage, req, link, (i + 1) * 10))
    if con.execute("PRAGMA user_version").fetchone()[0] < 1:
        _migrate_doc_files(con)
        con.execute("PRAGMA user_version=1")
    con.commit()
    con.close()


def _migrate_doc_files(con):
    """v0.1은 문서마다 파일 하나만 연결했다(다시 올리면 연결만 바뀌고 이전 파일은 디스크에 남음).
    지금 연결된 파일과, 변경이력의 '파일 등록' 시각으로 찾은 이전 파일을 파일 목록으로 옮긴다."""
    con.row_factory = sqlite3.Row
    claimed = set()

    def add(sd, name, path, by, at, note):
        con.execute("INSERT INTO study_doc_files(study_doc_id,study_id,file_name,file_path,note,uploaded_by,"
                    "uploaded_at) VALUES(?,?,?,?,?,?,?)", (sd["id"], sd["study_id"], name, path, note, by, at))
        claimed.add(path)

    events = con.execute("SELECT * FROM audit WHERE tbl='문서' AND action='파일 등록' ORDER BY id").fetchall()
    for sd in con.execute("SELECT * FROM study_docs WHERE file_path IS NOT NULL AND file_path!=''").fetchall():
        ev = [e for e in events if e["study_id"] == sd["study_id"] and e["new"] == sd["file_name"]]
        add(sd, sd["file_name"], sd["file_path"], ev[-1]["user"] if ev else None,
            ev[-1]["ts"] if ev else None, None)
    restored = 0
    for e in events:
        sdir = os.path.join(UPLOADS, str(e["study_id"]))
        if not os.path.isdir(sdir):
            continue
        sd = con.execute("SELECT sd.* FROM study_docs sd JOIN doc_types t ON t.id=sd.doc_type_id "
                         "WHERE sd.study_id=? AND t.name=?", (e["study_id"], e["rec"])).fetchone()
        if sd is None or con.execute("SELECT 1 FROM study_doc_files WHERE study_doc_id=? AND file_name=? "
                                     "AND uploaded_at=?", (sd["id"], e["new"], e["ts"])).fetchone():
            continue
        ts = datetime.strptime(e["ts"], "%Y-%m-%d %H:%M:%S").timestamp()
        best = None
        for fn in os.listdir(sdir):
            path = f"{e['study_id']}/{fn}"
            gap = abs(os.path.getmtime(os.path.join(sdir, fn)) - ts)
            if path not in claimed and gap <= 120 and (best is None or gap < best[0]):
                best = (gap, path)
        if best:
            add(sd, e["new"], best[1], e["user"], e["ts"], "이전 버전에서 다른 파일로 교체되었던 파일")
            restored += 1
    if claimed:
        con.execute("INSERT INTO audit(ts,user,tbl,action,new) VALUES(?,?,?,?,?)",
                    (now(), "system", "문서", "파일 목록 전환", f"파일 {len(claimed)}개 (이전 교체 파일 {restored}개 포함)"))
    con.row_factory = None


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today():
    return date.today().isoformat()


def valid_date(s):
    try:
        date.fromisoformat(s)
        return len(s) == 10
    except (TypeError, ValueError):
        return False


def natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s or "")]


def actor():
    u = g.get("user")
    return f"{u['name']}({u['username']})" if u else "system"


def audit(study_id, tbl, rec, action, field=None, old=None, new=None, reason=None):
    def s(v):
        return None if v is None else str(v)
    db().execute(
        "INSERT INTO audit(ts,user,study_id,tbl,rec,action,field,old,new,reason) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (now(), actor(), study_id, tbl, s(rec), action, field, s(old), s(new), reason or None))


def fmt_num(v, nd=2):
    if v is None:
        return ""
    return f"{v:.{nd}f}".rstrip("0").rstrip(".")


def fmt_fixed(v, nd=2):
    return "" if v is None else f"{v:.{nd}f}"


def decimals(values, cap=3):
    """입력된 값들의 최대 소수 자릿수(표시 자릿수 통일용)."""
    nd = 0
    for v in values:
        t = fmt_num(v, cap)
        if "." in t:
            nd = max(nd, len(t.split(".")[1]))
    return nd


app.jinja_env.filters["num"] = fmt_num
app.jinja_env.filters["fixed"] = fmt_fixed


def read_links(field):
    """폼의 연계 체크박스(같은 이름 여러 개)를 저장 형식으로 바꾼다."""
    vals = request.form.getlist(field)
    if any(v not in LINKS for v in vals):
        abort(400)
    return ",".join(k for k in LINKS if k in vals)


def split_links(s):
    return [k for k in (s or "").split(",") if k in LINKS]


app.jinja_env.filters["links"] = split_links


@app.context_processor
def inject():
    def csrf():
        return Markup('<input type="hidden" name="_csrf" value="%s">') % session.get("_csrf", "")
    return dict(csrf=csrf, today=today(), STATUSES=STATUSES, DOC_STATUSES=DOC_STATUSES,
                STAGES=STAGES, LINKS=LINKS, GROUP_KINDS=GROUP_KINDS, KIND_CUSTOM=KIND_CUSTOM, ANIMAL_STATUSES=ANIMAL_STATUSES, SEVERITIES=SEVERITIES,
                COMMON_SIGNS=COMMON_SIGNS)


@app.before_request
def guard():
    g.user = None
    if request.endpoint == "static":
        return None
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    if request.method == "POST" and request.form.get("_csrf") != session.get("_csrf"):
        abort(400, "요청이 만료되었습니다. 페이지를 새로고침한 뒤 다시 시도하세요.")
    if db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        return None if request.endpoint == "setup" else redirect(url_for("setup"))
    uid = session.get("uid")
    if uid:
        g.user = db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    if g.user is None and request.endpoint not in ("login", "setup"):
        return redirect(url_for("login"))
    return None


def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if g.user["role"] != "admin":
            abort(403)
        return f(*a, **k)
    return w


def is_admin():
    return g.user["role"] == "admin"


def get_study(sid):
    s = db().execute("SELECT * FROM studies WHERE id=?", (sid,)).fetchone()
    if s is None:
        abort(404)
    return s


def locked(s):
    return s["status"] == "종료"


def deny_if_locked(s, endpoint):
    if locked(s):
        flash("종료된 시험은 자료를 수정할 수 없습니다. 관리자가 상태를 '진행'으로 되돌려야 합니다.", "error")
        return redirect(url_for(endpoint, sid=s["id"]))
    return None


def structure(sid):
    groups = db().execute("SELECT * FROM grp WHERE study_id=? ORDER BY ord, id", (sid,)).fetchall()
    animals = db().execute("SELECT * FROM animals WHERE study_id=?", (sid,)).fetchall()
    animals = sorted(animals, key=lambda a: natkey(a["animal_no"]))
    return [{"id": gr["id"], "name": gr["name"], "kind": gr["kind"], "note": gr["note"] or "",
             "animals": [a for a in animals if a["group_id"] == gr["id"]]} for gr in groups]


def parse_numbers(text, limit=500):
    """'1-10, 15, 20~22' → [1..10, 15, 20, 21, 22]. 잘못된 입력이면 ValueError."""
    out = []
    text = re.sub(r"\s*([-~])\s*", r"\1", (text or "").strip())  # '1 - 10'도 허용
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,5})(?:[-~](\d{1,5}))?", part)
        if not m:
            raise ValueError(part)
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if a < 1 or b < a or b - a >= limit:
            raise ValueError(part)
        out.extend(range(a, b + 1))
        if len(out) > limit:
            raise ValueError(part)
    if len(set(out)) != len(out):
        raise ValueError("중복")
    return out


def read_kind(suffix=""):
    """군 성격: 목록에서 고른 값, 또는 '직접 입력'이면 입력한 글자. 없거나 잘못되면 ''."""
    kind = request.form.get("kind" + suffix, "")
    if kind == KIND_CUSTOM:
        return re.sub(r"\s+", " ", request.form.get("kind_custom" + suffix, "")).strip()[:30]
    return kind if kind in GROUP_KINDS else ""


def parse_weight(raw):
    """체중 입력값 → float. 비었거나 잘못되면 None."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def animal_no(group_name, n):
    return f"{group_name}-{n}"


def ensure_docs(sid):
    """활성 문서 종류 중 이 시험에 아직 없는 항목을 체크리스트에 추가한다."""
    for t in db().execute("SELECT * FROM doc_types WHERE active=1"):
        db().execute("INSERT OR IGNORE INTO study_docs(study_id, doc_type_id, status) VALUES(?,?,?)",
                     (sid, t["id"], "미작성" if t["required"] else "해당없음"))


# ------------------------------------------------------------------ 계정
@app.route("/setup", methods=["GET", "POST"])
def setup():
    if db().execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return redirect(url_for("login"))
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        n = request.form.get("name", "").strip()
        p, p2 = request.form.get("password", ""), request.form.get("password2", "")
        if not u or not n:
            flash("아이디와 이름을 입력하세요.", "error")
        elif len(p) < 8:
            flash("비밀번호는 8자 이상이어야 합니다.", "error")
        elif p != p2:
            flash("비밀번호가 서로 다릅니다.", "error")
        else:
            db().execute("INSERT INTO users(username,name,pw_hash,role) VALUES(?,?,?, 'admin')",
                         (u, n, generate_password_hash(p)))
            audit(None, "사용자", u, "생성", "권한", None, "admin")
            db().commit()
            flash("관리자 계정을 만들었습니다. 로그인하세요.", "ok")
            return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE username=? AND active=1",
                         (request.form.get("username", "").strip(),)).fetchone()
        if u and check_password_hash(u["pw_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = u["id"]
            return redirect(url_for("index"))
        flash("아이디 또는 비밀번호가 올바르지 않습니다.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
def account():
    if request.method == "POST":
        cur, p, p2 = (request.form.get(k, "") for k in ("current", "password", "password2"))
        if not check_password_hash(g.user["pw_hash"], cur):
            flash("현재 비밀번호가 올바르지 않습니다.", "error")
        elif len(p) < 8:
            flash("비밀번호는 8자 이상이어야 합니다.", "error")
        elif p != p2:
            flash("새 비밀번호가 서로 다릅니다.", "error")
        else:
            db().execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(p), g.user["id"]))
            audit(None, "사용자", g.user["username"], "비밀번호 변경")
            db().commit()
            flash("비밀번호를 변경했습니다.", "ok")
            return redirect(url_for("index"))
    return render_template("account.html")


# ------------------------------------------------------------------ 시험
@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    st = request.args.get("status", "")
    sql = """SELECT s.*,
      (SELECT COUNT(*) FROM study_docs d WHERE d.study_id=s.id AND d.status!='해당없음') AS doc_total,
      (SELECT COUNT(*) FROM study_docs d WHERE d.study_id=s.id AND d.status='완료') AS doc_done,
      (SELECT COUNT(*) FROM animals a WHERE a.study_id=s.id) AS n_reg,
      (SELECT MAX(date) FROM weights w WHERE w.study_id=s.id) AS last_w,
      (SELECT MAX(date) FROM clinical c WHERE c.study_id=s.id) AS last_c
      FROM studies s WHERE 1=1"""
    args = []
    if q:
        sql += " AND (s.title LIKE ? OR s.study_no LIKE ? OR s.species LIKE ?)"
        args += [f"%{q}%"] * 3
    if st in STATUSES:
        sql += " AND s.status=?"
        args.append(st)
    sql += " ORDER BY CASE s.status WHEN '진행' THEN 0 WHEN '준비' THEN 1 ELSE 2 END, s.start_date DESC, s.id DESC"
    return render_template("index.html", studies=db().execute(sql, args).fetchall(), q=q, st=st)


def read_study_form():
    f = {k: (request.form.get(k) or "").strip() for k in STUDY_LABELS}
    errs = []
    for k in ("study_no", "title", "species"):
        if not f[k]:
            errs.append(f"{STUDY_LABELS[k]}을(를) 입력하세요.")
    try:
        f["n_animals"] = int(f["n_animals"])
        if f["n_animals"] < 1:
            raise ValueError
    except ValueError:
        errs.append("마리수는 1 이상의 정수여야 합니다.")
    for k in ("start_date", "end_date"):
        if f[k] and not valid_date(f[k]):
            errs.append(f"{STUDY_LABELS[k]} 형식이 올바르지 않습니다.")
    if f["start_date"] and f["end_date"] and f["end_date"] < f["start_date"]:
        errs.append("종료일이 시작일보다 빠릅니다.")
    if f["status"] not in STATUSES:
        f["status"] = "준비"
    if f["weight_unit"] not in ("g", "kg"):
        f["weight_unit"] = "g"
    return f, errs


@app.route("/studies/new", methods=["GET", "POST"])
def study_new():
    f = {"status": "준비", "weight_unit": "g"}
    if request.method == "POST":
        f, errs = read_study_form()
        if not errs and db().execute("SELECT 1 FROM studies WHERE study_no=?", (f["study_no"],)).fetchone():
            errs.append("이미 사용 중인 시험번호입니다.")
        if not errs:
            cur = db().execute(
                "INSERT INTO studies(study_no,title,species,n_animals,start_date,end_date,director,"
                "sponsor,weight_unit,status,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f["study_no"], f["title"], f["species"], f["n_animals"], f["start_date"], f["end_date"],
                 f["director"], f["sponsor"], f["weight_unit"], f["status"], f["note"], actor(), now()))
            sid = cur.lastrowid
            ensure_docs(sid)
            audit(sid, "시험", f["study_no"], "생성", None, None, f["title"])
            db().commit()
            flash("시험을 등록했습니다. 문서 체크리스트가 자동으로 만들어졌습니다.", "ok")
            return redirect(url_for("study", sid=sid))
        for e in errs:
            flash(e, "error")
    return render_template("study_form.html", f=f, s=None)


@app.route("/studies/<int:sid>/edit", methods=["GET", "POST"])
def study_edit(sid):
    s = get_study(sid)
    if locked(s) and not is_admin():
        flash("종료된 시험 정보는 관리자만 수정할 수 있습니다.", "error")
        return redirect(url_for("study", sid=sid))
    f = dict(s)
    if request.method == "POST":
        f, errs = read_study_form()
        reason = request.form.get("reason", "").strip()
        if not errs and db().execute("SELECT 1 FROM studies WHERE study_no=? AND id!=?",
                                     (f["study_no"], sid)).fetchone():
            errs.append("이미 사용 중인 시험번호입니다.")
        changed = [k for k in STUDY_LABELS if (s[k] if s[k] is not None else "") != f[k]]
        if changed and not reason:
            errs.append("변경 사유를 입력하세요.")
        if not errs:
            for k in changed:
                audit(sid, "시험", s["study_no"], "수정", STUDY_LABELS[k], s[k], f[k], reason)
            db().execute(
                "UPDATE studies SET study_no=?,title=?,species=?,n_animals=?,start_date=?,end_date=?,"
                "director=?,sponsor=?,weight_unit=?,status=?,note=? WHERE id=?",
                (f["study_no"], f["title"], f["species"], f["n_animals"], f["start_date"], f["end_date"],
                 f["director"], f["sponsor"], f["weight_unit"], f["status"], f["note"], sid))
            db().commit()
            flash("저장했습니다." if changed else "변경된 내용이 없습니다.", "ok")
            return redirect(url_for("study", sid=sid))
        for e in errs:
            flash(e, "error")
    return render_template("study_form.html", f=f, s=s)


@app.route("/studies/<int:sid>")
def study(sid):
    s = get_study(sid)
    d = db()
    info = {
        "n_reg": d.execute("SELECT COUNT(*) FROM animals WHERE study_id=?", (sid,)).fetchone()[0],
        "n_dead": d.execute("SELECT COUNT(*) FROM animals WHERE study_id=? AND status='폐사'", (sid,)).fetchone()[0],
        "w_days": d.execute("SELECT COUNT(DISTINCT date) FROM weights WHERE study_id=?", (sid,)).fetchone()[0],
        "w_last": d.execute("SELECT MAX(date) FROM weights WHERE study_id=?", (sid,)).fetchone()[0],
        "c_days": d.execute("SELECT COUNT(DISTINCT date) FROM clinical WHERE study_id=?", (sid,)).fetchone()[0],
        "c_last": d.execute("SELECT MAX(date) FROM clinical WHERE study_id=?", (sid,)).fetchone()[0],
        "c_abn": d.execute("SELECT COUNT(*) FROM clinical WHERE study_id=? AND normal=0", (sid,)).fetchone()[0],
        "doc_total": d.execute("SELECT COUNT(*) FROM study_docs WHERE study_id=? AND status!='해당없음'", (sid,)).fetchone()[0],
        "doc_done": d.execute("SELECT COUNT(*) FROM study_docs WHERE study_id=? AND status='완료'", (sid,)).fetchone()[0],
    }
    pending = d.execute(
        "SELECT t.name, t.stage, sd.status FROM study_docs sd JOIN doc_types t ON t.id=sd.doc_type_id "
        "WHERE sd.study_id=? AND sd.status IN ('미작성','작성중') "
        "ORDER BY CASE t.stage WHEN '시험 전' THEN 0 WHEN '시험 중' THEN 1 ELSE 2 END, t.ord", (sid,)).fetchall()
    return render_template("study.html", s=s, tab="overview", info=info, pending=pending,
                           groups=structure(sid))


# ------------------------------------------------------------------ 군 · 개체
@app.route("/studies/<int:sid>/animals")
def animals(sid):
    return _render_animals(get_study(sid))


def _render_animals(s, rows=None, errors=None, wdate=None):
    sid = s["id"]
    used = {r[0] for r in db().execute(
        "SELECT animal_id FROM weights WHERE study_id=? UNION SELECT animal_id FROM clinical WHERE study_id=?",
        (sid, sid))}
    groups = structure(sid)
    if rows is None:  # 군 추가: 빈 군 하나(개체 칸 하나)로 시작
        rows = [{}]
    return render_template("animals.html", s=s, tab="animals", groups=groups, used=used,
                           n_reg=sum(len(x["animals"]) for x in groups), rows=rows, errors=errors or {},
                           wdate=wdate or today())


def _check_groups(sid, rows):
    """군 추가 줄들을 검사한다. 반환: ({줄 번호: [오류]}, [(줄, 개체번호들)]).
    이미 등록된 군·개체번호뿐 아니라 같은 표 안에서 겹치는 것도 잡는다."""
    names = {r[0] for r in db().execute("SELECT name FROM grp WHERE study_id=?", (sid,))}
    exist = {r[0] for r in db().execute("SELECT animal_no FROM animals WHERE study_id=?", (sid,))}
    errors, plan = {}, []
    for i, r in enumerate(rows):
        e = []
        if not GROUP_NAME_RE.fullmatch(r["name"]):
            e.append("군 이름은 G1, C1처럼 공백과 '-' 없이 20자 이내로 입력하세요.")
        elif r["name"] in names:
            e.append(f"'{r['name']}' 군이 이미 있습니다. 개체를 더하려면 '기존 군에 개체 추가'를 쓰세요.")
        if not r["kind"]:
            e.append("군 성격을 고르세요. '직접 입력'이면 어떤 군인지 입력하세요.")
        if r.get("nums"):  # 개체 칸마다 번호 하나씩 입력한 경우
            bad = [x for x in r["nums"] if not re.fullmatch(r"\d{1,5}", x) or int(x) < 1]
            seen, twice = set(), []
            for x in r["nums"]:
                if x in seen and x not in twice:
                    twice.append(x)
                seen.add(x)
            nums = None
            if bad:
                e.append(f"개체 번호는 숫자만 입력하세요: {', '.join(bad[:5])}")
            elif twice:
                e.append(f"같은 개체 번호가 두 번 있습니다: {', '.join(twice[:5])}")
            else:
                nums = [int(x) for x in r["nums"]]
            badw = [f"{r['nums'][k]}번 '{w}'" for k, w in enumerate(r.get("weights", [])) if w and parse_weight(w) is None]
            if badw:
                e.append(f"체중은 0보다 큰 숫자로 입력하세요: {', '.join(badw[:5])}")
        else:
            try:
                nums = parse_numbers(r["numbers"])
            except ValueError:
                nums = None
                e.append("개체 번호는 숫자로 입력하세요. 예: 1-10, 1, 2, 5 (한 군에 최대 500마리)")
        nos = [animal_no(r["name"], n) for n in nums or []]
        dup = [n for n in nos if n in exist]
        if dup and not e:
            e.append(f"이미 있는 개체번호입니다: {', '.join(dup[:5])}")
        if e:
            errors[i] = e
        else:
            plan.append((r, nos))
        names.add(r["name"])
        exist.update(nos)
    return errors, plan


def _create_groups(s, plan, wdate=None):
    sid = s["id"]
    n_w = 0
    n_grp = db().execute("SELECT COUNT(*) FROM grp WHERE study_id=?", (sid,)).fetchone()[0]
    for k, (r, nos) in enumerate(plan, 1):
        gid = db().execute("INSERT INTO grp(study_id,name,ord,kind,note) VALUES(?,?,?,?,?)",
                           (sid, r["name"], n_grp + k, r["kind"], r["note"] or None)).lastrowid
        audit(sid, "군", r["name"], "생성", "성격", None, r["kind"])
        if r["note"]:
            audit(sid, "군", r["name"], "생성", "설명", None, r["note"])
        audit(sid, "군", r["name"], "생성", "마리수", None, len(nos))
        # 개체 칸마다 성별을 고른 경우(sexes)는 개체별로, 아니면 군에 지정한 성별을 쓴다
        sexes = r.get("sexes") if r.get("nums") else None
        for k2, n in enumerate(nos):
            sex = sexes[k2] if sexes else r["sex"]
            aid = db().execute("INSERT INTO animals(study_id,group_id,animal_no,sex) VALUES(?,?,?,?)",
                               (sid, gid, n, sex)).lastrowid
            audit(sid, "개체", n, "생성", "군", None, r["name"])
            if sex:
                audit(sid, "개체", n, "생성", "성별", None, sex)
            w = parse_weight(r["weights"][k2]) if sexes is not None and r.get("weights") else None
            if w is not None and wdate:
                db().execute("INSERT INTO weights(study_id,animal_id,date,value,entered_by,entered_at) "
                             "VALUES(?,?,?,?,?,?)", (sid, aid, wdate, w, actor(), now()))
                audit(sid, "체중", f"{n} / {wdate}", "입력", "체중", None, fmt_num(w, 3), "개체 등록 시 초기 체중")
                n_w += 1
    db().commit()
    total = db().execute("SELECT COUNT(*) FROM animals WHERE study_id=?", (sid,)).fetchone()[0]
    if total > s["n_animals"]:
        flash(f"등록 개체 수({total})가 계획 마리수({s['n_animals']})를 넘었습니다. 시험 정보를 확인하세요.", "warn")
    return n_w


ANIMAL_SEXES = ("", "수", "암")


def _read_group_row(suffix=""):
    f = request.form
    # 개체 칸: 번호(num)와 성별(asex)이 같은 순서로 온다. 번호가 빈 칸은 버린다.
    pad = [""] * 500
    rows = [(n.strip(), sx.strip(), w.strip()[:20]) for n, sx, w in
            zip(f.getlist("num" + suffix), f.getlist("asex" + suffix) + pad, f.getlist("aw" + suffix) + pad)
            if n.strip()][:500]
    if any(sx not in ANIMAL_SEXES for _, sx, _ in rows):
        abort(400)
    return {"name": f.get("name" + suffix, "").strip(), "kind": read_kind(suffix),
            "kind_sel": f.get("kind" + suffix, ""), "kind_custom": f.get("kind_custom" + suffix, "").strip()[:30],
            "note": f.get("note" + suffix, "").strip()[:100], "numbers": f.get("numbers" + suffix, "").strip()[:200],
            "nums": [n for n, _, _ in rows], "sexes": [sx for _, sx, _ in rows],
            "weights": [w for _, _, w in rows],
            "sex": f.get("sex" + suffix, "").strip()[:10]}


@app.route("/studies/<int:sid>/animals/add_groups", methods=["POST"])
def add_groups(sid):
    """여러 군을 한 번에 등록한다. 한 군이라도 틀리면 아무것도 등록하지 않고 입력값을 그대로 돌려준다."""
    s = get_study(sid)
    r = deny_if_locked(s, "animals")
    if r:
        return r
    try:
        n = min(int(request.form.get("n", "0")), 100)
    except ValueError:
        abort(400)
    rows = [_read_group_row(f"_{i}") for i in range(n)]
    filled = [r for r in rows if any(r[k] for k in ("name", "kind_sel", "kind_custom", "note", "numbers", "nums"))]
    if not filled:
        flash("등록할 군을 입력하세요.", "error")
        return redirect(url_for("animals", sid=sid))
    errors, plan = _check_groups(sid, filled)
    wdate = request.form.get("wdate", "").strip()
    has_w = any(w for r in filled for w in r.get("weights", []))
    date_err = None
    if has_w and not valid_date(wdate):
        date_err = "체중을 입력했으면 체중 측정일을 입력하세요."
    elif has_w and wdate > today():
        date_err = "체중 측정일은 미래 날짜일 수 없습니다."
    if errors or date_err:
        if date_err:
            flash(date_err + " 아무것도 등록하지 않았습니다.", "error")
        if errors:
            flash(f"{len(errors)}개 군에 문제가 있어 아무것도 등록하지 않았습니다. 빨간 글씨를 고친 뒤 다시 등록하세요.", "error")
        return _render_animals(s, rows=filled, errors=errors, wdate=wdate)
    n_w = _create_groups(s, plan, wdate if has_w else None)
    flash(f"군 {len(plan)}개, 개체 {sum(len(x[1]) for x in plan)}마리를 등록했습니다: " +
          ", ".join(f"{r['name']}({r['kind']}) {len(nos)}마리" for r, nos in plan) +
          (f". 초기 체중 {n_w}건을 {wdate} 날짜로 저장했습니다." if n_w else ""), "ok")
    return redirect(url_for("animals", sid=sid))


@app.route("/studies/<int:sid>/animals/add_group", methods=["POST"])
def add_group(sid):
    """군 하나 등록 (이전 화면 호환)."""
    s = get_study(sid)
    r = deny_if_locked(s, "animals")
    if r:
        return r
    errors, plan = _check_groups(sid, [_read_group_row()])
    if errors:
        for e in errors[0]:
            flash(e, "error")
        return redirect(url_for("animals", sid=sid))
    _create_groups(s, plan)
    row, nos = plan[0]
    flash(f"'{row['name']}'({row['kind']}) 군과 개체 {len(nos)}마리를 추가했습니다." if nos else
          f"'{row['name']}'({row['kind']}) 군을 추가했습니다.", "ok")
    return redirect(url_for("animals", sid=sid))


@app.route("/studies/<int:sid>/animals/add", methods=["POST"])
def add_animal(sid):
    s = get_study(sid)
    r = deny_if_locked(s, "animals")
    if r:
        return r
    gr = db().execute("SELECT * FROM grp WHERE id=? AND study_id=?",
                      (request.form.get("group_id"), sid)).fetchone()
    if gr is None:
        flash("군을 고르세요.", "error")
        return redirect(url_for("animals", sid=sid))
    try:
        nums = parse_numbers(request.form.get("numbers", ""))
    except ValueError:
        nums = []
    if not nums:
        flash("개체 번호는 숫자로 입력하세요. 예: 11 또는 11-15", "error")
        return redirect(url_for("animals", sid=sid))
    nos = [animal_no(gr["name"], n) for n in nums]
    exist = {r[0] for r in db().execute("SELECT animal_no FROM animals WHERE study_id=?", (sid,))}
    dup = [n for n in nos if n in exist]
    if dup:
        flash(f"이미 있는 개체번호입니다: {', '.join(dup[:5])}", "error")
        return redirect(url_for("animals", sid=sid))
    sex = request.form.get("sex", "").strip()
    for no in nos:
        db().execute("INSERT INTO animals(study_id,group_id,animal_no,sex) VALUES(?,?,?,?)", (sid, gr["id"], no, sex))
        audit(sid, "개체", no, "생성", "군", None, gr["name"])
    db().commit()
    flash(f"개체 {', '.join(nos[:5])}{' …' if len(nos) > 5 else ''} ({len(nos)}마리)을(를) 추가했습니다.", "ok")
    return redirect(url_for("animals", sid=sid))


@app.route("/studies/<int:sid>/groups/<int:gid>/edit", methods=["POST"])
def group_edit(sid, gid):
    """군 성격·설명 수정. 군 이름은 개체번호에 들어가 있어 바꾸지 않는다."""
    s = get_study(sid)
    r = deny_if_locked(s, "animals")
    if r:
        return r
    gr = db().execute("SELECT * FROM grp WHERE id=? AND study_id=?", (gid, sid)).fetchone()
    if gr is None:
        abort(404)
    kind = read_kind()
    note = request.form.get("note", "").strip()[:100]
    if not kind:
        flash("군 성격을 고르세요. '직접 입력'을 골랐다면 어떤 군인지 입력하세요.", "error")
        return redirect(url_for("animals", sid=sid))
    n = 0
    for field, old, new in (("성격", gr["kind"], kind), ("설명", gr["note"] or "", note)):
        if old != new:
            audit(sid, "군", gr["name"], "수정", field, old, new)
            n += 1
    if n:
        db().execute("UPDATE grp SET kind=?, note=? WHERE id=?", (kind, note or None, gid))
        db().commit()
    flash(f"'{gr['name']}' 군을 저장했습니다." if n else "변경된 내용이 없습니다.", "ok")
    return redirect(url_for("animals", sid=sid))


@app.route("/studies/<int:sid>/animals/<int:aid>/status", methods=["POST"])
def animal_status(sid, aid):
    s = get_study(sid)
    r = deny_if_locked(s, "animals")
    if r:
        return r
    a = db().execute("SELECT * FROM animals WHERE id=? AND study_id=?", (aid, sid)).fetchone()
    if a is None:
        abort(404)
    st = request.form.get("status", "")
    sd = request.form.get("status_date", "").strip()
    reason = request.form.get("reason", "").strip()
    if st not in ANIMAL_STATUSES or (sd and not valid_date(sd)):
        abort(400)
    if st != "사용중" and not sd:
        flash("폐사·제외는 발생일을 입력해야 합니다.", "error")
    elif not reason:
        flash("개체 상태 변경에는 사유가 필요합니다.", "error")
    elif st == a["status"] and sd == (a["status_date"] or ""):
        flash("변경된 내용이 없습니다.", "ok")
    else:
        if st == "사용중":
            sd = ""
        audit(sid, "개체", a["animal_no"], "수정", "상태",
              f"{a['status']} {a['status_date'] or ''}".strip(), f"{st} {sd}".strip(), reason)
        db().execute("UPDATE animals SET status=?, status_date=? WHERE id=?", (st, sd or None, aid))
        db().commit()
        flash(f"{a['animal_no']} 상태를 '{st}'(으)로 변경했습니다.", "ok")
    return redirect(url_for("animals", sid=sid))


@app.route("/studies/<int:sid>/animals/<int:aid>/sex", methods=["POST"])
def animal_sex(sid, aid):
    s = get_study(sid)
    r = deny_if_locked(s, "animals")
    if r:
        return r
    a = db().execute("SELECT * FROM animals WHERE id=? AND study_id=?", (aid, sid)).fetchone()
    if a is None:
        abort(404)
    sex = request.form.get("sex", "")
    if sex not in ANIMAL_SEXES:
        abort(400)
    if sex != (a["sex"] or ""):
        audit(sid, "개체", a["animal_no"], "수정", "성별", a["sex"] or "", sex)
        db().execute("UPDATE animals SET sex=? WHERE id=?", (sex, aid))
        db().commit()
        flash(f"{a['animal_no']} 성별을 '{sex or '-'}'(으)로 바꿨습니다.", "ok")
    return redirect(url_for("animals", sid=sid))


@app.route("/studies/<int:sid>/animals/<int:aid>/delete", methods=["POST"])
def animal_delete(sid, aid):
    s = get_study(sid)
    r = deny_if_locked(s, "animals")
    if r:
        return r
    a = db().execute("SELECT * FROM animals WHERE id=? AND study_id=?", (aid, sid)).fetchone()
    if a is None:
        abort(404)
    has = db().execute("SELECT (SELECT COUNT(*) FROM weights WHERE animal_id=?) + "
                       "(SELECT COUNT(*) FROM clinical WHERE animal_id=?)", (aid, aid)).fetchone()[0]
    if has:
        flash("기록이 있는 개체는 삭제할 수 없습니다. 상태를 '제외'로 바꾸세요.", "error")
    else:
        db().execute("DELETE FROM animals WHERE id=?", (aid,))
        audit(sid, "개체", a["animal_no"], "삭제", None, None, None, "기록 없는 개체 삭제")
        db().commit()
        flash(f"개체 {a['animal_no']}을(를) 삭제했습니다.", "ok")
    return redirect(url_for("animals", sid=sid))


@app.route("/studies/<int:sid>/groups/<int:gid>/delete", methods=["POST"])
def group_delete(sid, gid):
    s = get_study(sid)
    r = deny_if_locked(s, "animals")
    if r:
        return r
    gr = db().execute("SELECT * FROM grp WHERE id=? AND study_id=?", (gid, sid)).fetchone()
    if gr is None:
        abort(404)
    if db().execute("SELECT 1 FROM animals WHERE group_id=?", (gid,)).fetchone():
        flash("개체가 남아 있는 군은 삭제할 수 없습니다.", "error")
    else:
        db().execute("DELETE FROM grp WHERE id=?", (gid,))
        audit(sid, "군", gr["name"], "삭제")
        db().commit()
        flash(f"'{gr['name']}' 군을 삭제했습니다.", "ok")
    return redirect(url_for("animals", sid=sid))


# ------------------------------------------------------------------ 체중
def weight_data(sid):
    groups = [x for x in structure(sid) if x["animals"]]
    w = {}
    for r in db().execute("SELECT animal_id, date, value FROM weights WHERE study_id=?", (sid,)):
        w.setdefault(r["animal_id"], {})[r["date"]] = r["value"]
    dates = sorted({d for m in w.values() for d in m})
    for gr in groups:
        gr["stats"] = {}
        for d in dates:
            vals = [w[a["id"]][d] for a in gr["animals"] if d in w.get(a["id"], {})]
            if vals:
                gr["stats"][d] = {"n": len(vals), "mean": statistics.fmean(vals),
                                  "sd": statistics.stdev(vals) if len(vals) > 1 else None}
    return groups, w, dates


def weight_nd(w):
    return decimals(v for m in w.values() for v in m.values())


def day_offsets(s, dates):
    if not s["start_date"] or not valid_date(s["start_date"]):
        return {}
    d0 = date.fromisoformat(s["start_date"])
    return {d: (date.fromisoformat(d) - d0).days for d in dates}


@app.route("/studies/<int:sid>/weights")
def weights(sid):
    s = get_study(sid)
    groups, w, dates = weight_data(sid)
    chart = {"dates": dates, "unit": s["weight_unit"], "series": [
        {"name": gr["name"] + (f" ({gr['kind']})" if gr["kind"] else ""),
         "mean": [gr["stats"][d]["mean"] if d in gr["stats"] else None for d in dates],
         "sd": [gr["stats"][d]["sd"] if d in gr["stats"] else None for d in dates]}
        for gr in groups]}
    return render_template("weights.html", s=s, tab="weights", groups=groups, w=w, dates=dates,
                           days=day_offsets(s, dates), chart=chart, nd=weight_nd(w))


@app.route("/studies/<int:sid>/weights/entry", methods=["GET", "POST"])
def weights_entry(sid):
    s = get_study(sid)
    d = request.values.get("date") or today()
    if not valid_date(d):
        abort(400)
    groups = [x for x in structure(sid) if x["animals"]]
    cur = {r["animal_id"]: r["value"] for r in
           db().execute("SELECT animal_id, value FROM weights WHERE study_id=? AND date=?", (sid, d))}
    prev = {}
    for r in db().execute("SELECT animal_id, value, date FROM weights WHERE study_id=? AND date<? ORDER BY date",
                          (sid, d)):
        prev[r["animal_id"]] = r
    posted = None
    if request.method == "POST":
        r = deny_if_locked(s, "weights")
        if r:
            return r
        errs, changes = [], []
        reason = request.form.get("reason", "").strip()
        if d > today():
            errs.append("미래 날짜로는 기록할 수 없습니다.")
        for gr in groups:
            for a in gr["animals"]:
                raw = (request.form.get(f"w_{a['id']}") or "").strip()
                old = cur.get(a["id"])
                if raw == "":
                    new = None
                else:
                    try:
                        new = float(raw)
                        if not math.isfinite(new) or new <= 0:
                            raise ValueError
                    except ValueError:
                        errs.append(f"{a['animal_no']}: 올바른 체중이 아닙니다 ({raw})")
                        continue
                if new != old:
                    changes.append((a, old, new))
        if any(old is not None for _, old, _ in changes) and not reason:
            errs.append("이미 저장된 값을 수정·삭제하려면 변경 사유를 입력해야 합니다.")
        if not errs:
            for a, old, new in changes:
                rec = f"{a['animal_no']} / {d}"
                if old is None:
                    db().execute("INSERT INTO weights(study_id,animal_id,date,value,entered_by,entered_at) "
                                 "VALUES(?,?,?,?,?,?)", (sid, a["id"], d, new, actor(), now()))
                    audit(sid, "체중", rec, "입력", "체중", None, fmt_num(new, 3))
                elif new is None:
                    db().execute("DELETE FROM weights WHERE animal_id=? AND date=?", (a["id"], d))
                    audit(sid, "체중", rec, "삭제", "체중", fmt_num(old, 3), None, reason)
                else:
                    db().execute("UPDATE weights SET value=?, entered_by=?, entered_at=? "
                                 "WHERE animal_id=? AND date=?", (new, actor(), now(), a["id"], d))
                    audit(sid, "체중", rec, "수정", "체중", fmt_num(old, 3), fmt_num(new, 3), reason)
            db().commit()
            flash(f"{d} 체중 {len(changes)}건을 저장했습니다." if changes else "변경된 내용이 없습니다.", "ok")
            return redirect(url_for("weights", sid=sid))
        for e in errs:
            flash(e, "error")
        posted = request.form
    return render_template("weights_entry.html", s=s, tab="weights", groups=groups, d=d, cur=cur,
                           prev=prev, posted=posted)


@app.route("/studies/<int:sid>/weights.csv")
def weights_csv(sid):
    s = get_study(sid)
    groups, w, dates = weight_data(sid)
    nd = weight_nd(w)
    out = io.StringIO()
    c = csv.writer(out)
    c.writerow(["시험번호", s["study_no"], "실험제목", s["title"], "단위", s["weight_unit"]])
    c.writerow(["군", "군 성격", "개체번호", "성별", "상태"] + dates)
    for gr in groups:
        for a in gr["animals"]:
            c.writerow([gr["name"], gr["kind"], a["animal_no"], a["sex"] or "", a["status"]] +
                       [fmt_fixed(w.get(a["id"], {}).get(d), nd) for d in dates])
    c.writerow([])
    c.writerow(["군", "군 성격", "통계", "", ""] + dates)
    for gr in groups:
        for key, label, k in (("n", "n", 0), ("mean", "평균", nd + 1), ("sd", "SD", nd + 1)):
            c.writerow([gr["name"], gr["kind"], label, "", ""] +
                       [fmt_fixed(gr["stats"][d][key], k) if d in gr["stats"] else "" for d in dates])
    return csv_response(out, f"{s['study_no']}_weights.csv")


def csv_response(buf, filename):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    return Response(buf.getvalue().encode("utf-8-sig"), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


# ------------------------------------------------------------------ 임상증상
@app.route("/studies/<int:sid>/clinical")
def clinical(sid):
    s = get_study(sid)
    abn = request.args.get("abn") == "1"
    sql = ("SELECT c.*, a.animal_no, g.name AS gname, g.kind AS gkind FROM clinical c "
           "JOIN animals a ON a.id=c.animal_id JOIN grp g ON g.id=a.group_id WHERE c.study_id=?")
    if abn:
        sql += " AND c.normal=0"
    rows = sorted(db().execute(sql, (sid,)).fetchall(), key=lambda r: natkey(r["animal_no"]))
    rows.sort(key=lambda r: r["date"], reverse=True)  # 안정 정렬: 날짜 내림차순, 개체번호 오름차순
    summary = db().execute(
        "SELECT date, COUNT(*) AS n, SUM(CASE WHEN normal=0 THEN 1 ELSE 0 END) AS abn "
        "FROM clinical WHERE study_id=? GROUP BY date ORDER BY date DESC", (sid,)).fetchall()
    return render_template("clinical.html", s=s, tab="clinical", rows=rows, summary=summary, abn=abn,
                           days=day_offsets(s, [r["date"] for r in summary]))


@app.route("/studies/<int:sid>/clinical/entry", methods=["GET", "POST"])
def clinical_entry(sid):
    s = get_study(sid)
    d = request.values.get("date") or today()
    if not valid_date(d):
        abort(400)
    groups = [x for x in structure(sid) if x["animals"]]
    cur = {r["animal_id"]: r for r in
           db().execute("SELECT * FROM clinical WHERE study_id=? AND date=?", (sid, d))}
    posted = None
    if request.method == "POST":
        r = deny_if_locked(s, "clinical")
        if r:
            return r
        errs, changes = [], []
        reason = request.form.get("reason", "").strip()
        if d > today():
            errs.append("미래 날짜로는 기록할 수 없습니다.")
        for gr in groups:
            for a in gr["animals"]:
                i = a["id"]
                state = request.form.get(f"c_{i}", "")
                signs = (request.form.get(f"s_{i}") or "").strip()
                sev = request.form.get(f"v_{i}", "")
                note = (request.form.get(f"n_{i}") or "").strip()
                if state == "A":
                    if not signs:
                        errs.append(f"{a['animal_no']}: '이상'이면 증상을 입력해야 합니다.")
                        continue
                    if sev not in SEVERITIES:
                        errs.append(f"{a['animal_no']}: 정도를 선택하세요.")
                        continue
                    new = (0, signs, sev, note)
                elif state == "N":
                    new = (1, "", "", note)
                else:
                    new = None
                o = cur.get(i)
                old = (o["normal"], o["signs"] or "", o["severity"] or "", o["note"] or "") if o else None
                if new != old:
                    changes.append((a, old, new))
        if any(old is not None for _, old, _ in changes) and not reason:
            errs.append("이미 저장된 기록을 수정·삭제하려면 변경 사유를 입력해야 합니다.")
        if not errs:
            def text(t):
                if t is None:
                    return None
                base = "정상" if t[0] else f"{t[1]} ({t[2]})"
                return base + (f" / {t[3]}" if t[3] else "")
            for a, old, new in changes:
                rec = f"{a['animal_no']} / {d}"
                if old is None:
                    db().execute("INSERT INTO clinical(study_id,animal_id,date,normal,signs,severity,note,"
                                 "entered_by,entered_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                 (sid, a["id"], d, *new, actor(), now()))
                    audit(sid, "임상증상", rec, "입력", "관찰", None, text(new))
                elif new is None:
                    db().execute("DELETE FROM clinical WHERE animal_id=? AND date=?", (a["id"], d))
                    audit(sid, "임상증상", rec, "삭제", "관찰", text(old), None, reason)
                else:
                    db().execute("UPDATE clinical SET normal=?,signs=?,severity=?,note=?,entered_by=?,"
                                 "entered_at=? WHERE animal_id=? AND date=?",
                                 (*new, actor(), now(), a["id"], d))
                    audit(sid, "임상증상", rec, "수정", "관찰", text(old), text(new), reason)
            db().commit()
            flash(f"{d} 임상증상 {len(changes)}건을 저장했습니다." if changes else "변경된 내용이 없습니다.", "ok")
            return redirect(url_for("clinical", sid=sid))
        for e in errs:
            flash(e, "error")
        posted = request.form
    return render_template("clinical_entry.html", s=s, tab="clinical", groups=groups, d=d, cur=cur,
                           posted=posted)


@app.route("/studies/<int:sid>/clinical.csv")
def clinical_csv(sid):
    s = get_study(sid)
    rows = db().execute(
        "SELECT c.*, a.animal_no, g.name AS gname, g.kind AS gkind FROM clinical c "
        "JOIN animals a ON a.id=c.animal_id JOIN grp g ON g.id=a.group_id WHERE c.study_id=?", (sid,)).fetchall()
    rows.sort(key=lambda r: (r["date"], natkey(r["animal_no"])))
    out = io.StringIO()
    c = csv.writer(out)
    c.writerow(["시험번호", s["study_no"], "실험제목", s["title"]])
    c.writerow(["관찰일", "군", "군 성격", "개체번호", "판정", "증상", "정도", "비고", "기록자", "기록시각"])
    for r in rows:
        c.writerow([r["date"], r["gname"], r["gkind"], r["animal_no"], "정상" if r["normal"] else "이상",
                    r["signs"] or "", r["severity"] or "", r["note"] or "", r["entered_by"], r["entered_at"]])
    return csv_response(out, f"{s['study_no']}_clinical.csv")


# ------------------------------------------------------------------ 문서
@app.route("/studies/<int:sid>/docs")
def docs(sid):
    s = get_study(sid)
    if not locked(s):
        ensure_docs(sid)
        db().commit()
    rows = db().execute(
        "SELECT sd.*, t.name AS tname, t.code AS tcode, t.stage, t.required, t.link FROM study_docs sd "
        "JOIN doc_types t ON t.id=sd.doc_type_id WHERE sd.study_id=? ORDER BY t.ord, t.id", (sid,)).fetchall()
    linked = {
        "weights": db().execute("SELECT COUNT(*), COUNT(DISTINCT date), MAX(date) FROM weights WHERE study_id=?",
                                (sid,)).fetchone(),
        "clinical": db().execute("SELECT COUNT(*), COUNT(DISTINCT date), MAX(date) FROM clinical WHERE study_id=?",
                                 (sid,)).fetchone(),
    }
    files = {}
    for f in db().execute("SELECT * FROM study_doc_files WHERE study_id=? ORDER BY uploaded_at, id", (sid,)):
        files.setdefault(f["study_doc_id"], []).append(f)
    by_stage = [(st, [r for r in rows if r["stage"] == st]) for st in STAGES]
    total = sum(1 for r in rows if r["status"] != "해당없음")
    done = sum(1 for r in rows if r["status"] == "완료")
    return render_template("docs.html", s=s, tab="docs", by_stage=by_stage, linked=linked,
                           total=total, done=done, files=files)


@app.route("/studies/<int:sid>/docs/<int:did>", methods=["POST"])
def doc_update(sid, did):
    get_study(sid)
    d = db().execute("SELECT sd.*, t.name AS tname FROM study_docs sd JOIN doc_types t "
                     "ON t.id=sd.doc_type_id WHERE sd.id=? AND sd.study_id=?", (did, sid)).fetchone()
    if d is None:
        abort(404)
    new = {k: (request.form.get(k) or "").strip() for k in DOC_LABELS}
    if new["status"] not in DOC_STATUSES:
        abort(400)
    if new["doc_date"] and not valid_date(new["doc_date"]):
        abort(400)
    n = 0
    for k in DOC_LABELS:
        if (d[k] or "") != new[k]:
            audit(sid, "문서", d["tname"], "수정", DOC_LABELS[k], d[k], new[k])
            n += 1
    db().execute("UPDATE study_docs SET status=?,doc_no=?,doc_date=?,owner=?,note=? WHERE id=?",
                 (new["status"], new["doc_no"], new["doc_date"], new["owner"], new["note"], did))
    # 파일은 덮어쓰지 않고 목록에 더한다. 종료된 시험도 문서는 계속 갱신할 수 있다(결과보고서 등).
    file_note = (request.form.get("file_note") or "").strip()[:200]
    n_files = 0
    for f in request.files.getlist("file"):
        if not f or not f.filename:
            continue
        ext = re.sub(r"[^.a-z0-9]", "", os.path.splitext(f.filename)[1].lower())[:10]
        os.makedirs(os.path.join(UPLOADS, str(sid)), exist_ok=True)
        stored = f"{sid}/{uuid.uuid4().hex}{ext}"
        f.save(os.path.join(UPLOADS, stored))
        name = os.path.basename(f.filename.replace("\\", "/"))[:200]
        db().execute("INSERT INTO study_doc_files(study_doc_id,study_id,file_name,file_path,note,uploaded_by,"
                     "uploaded_at) VALUES(?,?,?,?,?,?,?)", (did, sid, name, stored, file_note or None, actor(), now()))
        audit(sid, "문서", d["tname"], "파일 추가", "파일", None, name, file_note or None)
        n += 1
        n_files += 1
    # 미작성 문서에 파일을 올리면 작성중으로 바꾼다. 사용자가 다른 상태에서 '미작성'으로 직접 고른 경우는 따른다.
    auto = n_files and d["status"] == "미작성" and new["status"] == "미작성"
    if auto:
        db().execute("UPDATE study_docs SET status='작성중' WHERE id=?", (did,))
        audit(sid, "문서", d["tname"], "수정", "상태", "미작성", "작성중", "파일 등록으로 자동 변경")
    db().commit()
    msg = f"'{d['tname']}' 저장했습니다." if n else "변경된 내용이 없습니다."
    flash(msg + (" 파일을 올려 상태를 '작성중'으로 바꿨습니다." if auto else ""), "ok")
    return redirect(url_for("docs", sid=sid) + f"#doc{did}")


@app.route("/studies/<int:sid>/docs/<int:did>/files/<int:fid>/hide", methods=["POST"])
def doc_file_hide(sid, did, fid):
    """잘못 올린 파일을 목록에서 숨긴다. 파일과 기록은 지우지 않는다."""
    f = db().execute("SELECT f.*, t.name AS tname FROM study_doc_files f JOIN study_docs sd ON sd.id=f.study_doc_id "
                     "JOIN doc_types t ON t.id=sd.doc_type_id WHERE f.id=? AND f.study_doc_id=? AND f.study_id=?",
                     (fid, did, sid)).fetchone()
    if f is None:
        abort(404)
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("파일을 숨기려면 사유를 입력하세요.", "error")
    elif not f["hidden"]:
        db().execute("UPDATE study_doc_files SET hidden=1, hidden_by=?, hidden_at=?, hidden_reason=? WHERE id=?",
                     (actor(), now(), reason, fid))
        audit(sid, "문서", f["tname"], "파일 숨김", "파일", f["file_name"], None, reason)
        db().commit()
        flash(f"'{f['file_name']}'을(를) 목록에서 숨겼습니다. 파일은 서버에 보존되고 '숨긴 파일'에서 볼 수 있습니다.", "ok")
    return redirect(url_for("docs", sid=sid) + f"#doc{did}")


@app.route("/files/<int:fid>")
def doc_file(fid):
    f = db().execute("SELECT * FROM study_doc_files WHERE id=?", (fid,)).fetchone()
    if f is None:
        abort(404)
    path = os.path.realpath(os.path.join(UPLOADS, f["file_path"]))
    if not path.startswith(os.path.realpath(UPLOADS) + os.sep) or not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=f["file_name"])


# ------------------------------------------------------------------ 변경이력
@app.route("/studies/<int:sid>/audit")
def study_audit(sid):
    s = get_study(sid)
    tbl = request.args.get("tbl", "")
    sql, args = "SELECT * FROM audit WHERE study_id=?", [sid]
    if tbl:
        sql += " AND tbl=?"
        args.append(tbl)
    rows = db().execute(sql + " ORDER BY id DESC LIMIT 2000", args).fetchall()
    tbls = [r[0] for r in db().execute("SELECT DISTINCT tbl FROM audit WHERE study_id=? ORDER BY tbl", (sid,))]
    return render_template("audit.html", s=s, tab="audit", rows=rows, tbls=tbls, tbl=tbl)


# ------------------------------------------------------------------ 관리자
@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    if request.method == "POST":
        act = request.form.get("action")
        if act == "add":
            u = request.form.get("username", "").strip()
            n = request.form.get("name", "").strip()
            p = request.form.get("password", "")
            role = "admin" if request.form.get("role") == "admin" else "user"
            if not u or not n or len(p) < 8:
                flash("아이디·이름을 입력하고 비밀번호는 8자 이상으로 하세요.", "error")
            elif db().execute("SELECT 1 FROM users WHERE username=?", (u,)).fetchone():
                flash("이미 있는 아이디입니다.", "error")
            else:
                db().execute("INSERT INTO users(username,name,pw_hash,role) VALUES(?,?,?,?)",
                             (u, n, generate_password_hash(p), role))
                audit(None, "사용자", u, "생성", "권한", None, role)
                flash(f"사용자 {u}을(를) 추가했습니다.", "ok")
        else:
            t = db().execute("SELECT * FROM users WHERE id=?", (request.form.get("id"),)).fetchone()
            if t is None:
                abort(404)
            if act == "toggle":
                if t["id"] == g.user["id"]:
                    flash("본인 계정은 비활성화할 수 없습니다.", "error")
                else:
                    db().execute("UPDATE users SET active=? WHERE id=?", (0 if t["active"] else 1, t["id"]))
                    audit(None, "사용자", t["username"], "수정", "활성",
                          "예" if t["active"] else "아니오", "아니오" if t["active"] else "예")
            elif act == "reset":
                p = request.form.get("password", "")
                if len(p) < 8:
                    flash("비밀번호는 8자 이상이어야 합니다.", "error")
                else:
                    db().execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(p), t["id"]))
                    audit(None, "사용자", t["username"], "비밀번호 초기화")
                    flash(f"{t['username']} 비밀번호를 변경했습니다.", "ok")
        db().commit()
        return redirect(url_for("admin_users"))
    return render_template("admin_users.html", users=db().execute("SELECT * FROM users ORDER BY id").fetchall())


@app.route("/admin/doctypes", methods=["GET", "POST"])
@admin_required
def admin_doctypes():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        stage = request.form.get("stage", "")
        link = read_links("link")
        code = request.form.get("code", "").strip()
        req = 1 if request.form.get("required") == "1" else 0
        active = 1 if request.form.get("active", "1") == "1" else 0
        try:
            ordv = int(request.form.get("ord", "0"))
        except ValueError:
            ordv = 0
        if not name or stage not in STAGES:
            flash("문서명과 단계를 확인하세요.", "error")
        elif request.form.get("id"):
            t = db().execute("SELECT * FROM doc_types WHERE id=?", (request.form["id"],)).fetchone()
            if t is None:
                abort(404)
            new = {"name": name, "code": code, "stage": stage, "required": req, "link": link, "ord": ordv,
                   "active": active}
            for k, v in new.items():
                if t[k] != v:
                    audit(None, "문서 종류", t["name"], "수정", k, t[k], v)
            db().execute("UPDATE doc_types SET name=?,code=?,stage=?,required=?,link=?,ord=?,active=? WHERE id=?",
                         (name, code, stage, req, link, ordv, active, t["id"]))
            flash(f"'{name}' 저장했습니다.", "ok")
        else:
            db().execute("INSERT INTO doc_types(name,code,stage,required,link,ord,active) VALUES(?,?,?,?,?,?,1)",
                         (name, code, stage, req, link, ordv))
            audit(None, "문서 종류", name, "생성", "단계", None, stage)
            flash(f"'{name}' 추가했습니다. 진행 중인 시험의 체크리스트에도 반영됩니다.", "ok")
        for s in db().execute("SELECT id FROM studies WHERE status!='종료'").fetchall():
            ensure_docs(s["id"])
        db().commit()
        return redirect(url_for("admin_doctypes"))
    types = db().execute("SELECT * FROM doc_types ORDER BY ord, id").fetchall()
    return render_template("admin_doctypes.html", types=types)


# ------------------------------------------------------------------ SOP에서 문서 종류 가져오기
SOP_IMPORT_DIR = os.path.join(UPLOADS, "sop")


def _match_existing(forms):
    """이미 등록된 문서 종류와 서식 번호로, 번호가 없는 문서 종류는 이름으로 짝짓는다."""
    types = db().execute("SELECT * FROM doc_types").fetchall()
    by_code = {t["code"]: t for t in types if t["code"]}
    by_name = {sop_import.squash(t["name"]): t for t in types if not t["code"]}
    for f in forms:
        t = by_code.get(f["code"]) or by_name.get(sop_import.squash(f["name"]))
        f["existing"] = t
        if t is not None:
            f["include"] = False
    return forms


@app.route("/admin/doctypes/import", methods=["GET", "POST"])
@admin_required
def doctypes_import():
    sop_code = (request.form.get("sop_code") or sop_import.DEFAULT_SOP_CODE).strip()
    form_suffix = (request.form.get("form_suffix") or sop_import.DEFAULT_FORM_SUFFIX).strip()
    opts = dict(sop_code=sop_code, form_suffix=form_suffix, forms=None,
                default_sop_code=sop_import.DEFAULT_SOP_CODE, default_form_suffix=sop_import.DEFAULT_FORM_SUFFIX)
    if request.method == "GET":
        return render_template("doctypes_import.html", **opts)
    if request.form.get("action") == "apply":
        return _doctypes_import_apply()

    f = request.files.get("file")
    if not f or not f.filename:
        flash("SOP 파일을 고르세요.", "error")
        return render_template("doctypes_import.html", **opts)
    filename = os.path.basename(f.filename.replace("\\", "/"))
    data = f.read()
    try:
        forms = sop_import.extract_forms(sop_import.read_blocks(filename, data), sop_code, form_suffix)
    except sop_import.SopImportError as e:
        flash(str(e), "error")
        return render_template("doctypes_import.html", **opts)
    if not forms:
        flash("서식 번호를 하나도 찾지 못했습니다. 아래 '서식 번호 형식'이 회사 SOP의 번호 체계와 맞는지 확인하세요.",
              "error")
        return render_template("doctypes_import.html", **opts)
    # 어떤 SOP에서 가져왔는지 근거로 남기려고 원본을 보관한다.
    os.makedirs(SOP_IMPORT_DIR, exist_ok=True)
    stored = uuid.uuid4().hex + os.path.splitext(filename)[1].lower()
    with open(os.path.join(SOP_IMPORT_DIR, stored), "wb") as out:
        out.write(data)
    hints = sop_import.load_hints()
    opts["forms"] = _match_existing([sop_import.suggest(x, hints) for x in forms])
    return render_template("doctypes_import.html", filename=filename, stored=stored, **opts)


def _doctypes_import_apply():
    filename = request.form.get("filename", "")[:200]
    stored = request.form.get("stored", "")
    if not re.fullmatch(r"[0-9a-f]{32}\.(?:hwpx|hwp|pdf)", stored):
        abort(400)
    try:
        n = int(request.form.get("n", "0"))
    except ValueError:
        abort(400)
    rows, errors = [], []
    for i in range(min(n, 2000)):
        if request.form.get(f"include_{i}") != "1":
            continue
        r = {k: (request.form.get(f"{k}_{i}") or "").strip()
             for k in ("code", "sop", "name", "stage", "existing")}
        r["link"] = read_links(f"link_{i}")
        r["required"] = 1 if request.form.get(f"required_{i}") == "1" else 0
        if not r["name"]:
            errors.append(f"{r['code']}: 문서명이 비어 있습니다.")
        elif r["stage"] not in STAGES:
            errors.append(f"{r['code']} {r['name']}: 단계를 고르세요.")
        rows.append(r)
    if errors or not rows:
        for e in errors[:10] or ["체크한 서식이 없습니다."]:
            flash(e, "error")
        flash("아무것도 등록하지 않았습니다. 브라우저의 '뒤로'를 눌러 미리보기로 돌아가 고친 뒤 다시 등록하세요.", "error")
        return redirect(url_for("admin_doctypes"))

    reason = f"SOP 가져오기: {filename}"
    max_ord = db().execute("SELECT COALESCE(MAX(ord),0) FROM doc_types").fetchone()[0]
    rows.sort(key=lambda r: (STAGES.index(r["stage"]), natkey(r["code"])))
    added = updated = 0
    for r in rows:
        if r["existing"]:
            t = db().execute("SELECT * FROM doc_types WHERE id=?", (r["existing"],)).fetchone()
            if t is None:
                abort(400)
            new = {"name": r["name"], "code": r["code"], "sop": r["sop"], "stage": r["stage"],
                   "required": r["required"], "link": r["link"], "active": 1}
            diff = [(k, t[k], v) for k, v in new.items() if t[k] != v]
            for k, old, v in diff:
                audit(None, "문서 종류", t["name"], "수정", k, old, v, reason)
            if diff:
                db().execute("UPDATE doc_types SET name=?,code=?,sop=?,stage=?,required=?,link=?,active=1 "
                             "WHERE id=?", (r["name"], r["code"], r["sop"], r["stage"], r["required"],
                                            r["link"], t["id"]))
                updated += 1
        else:
            max_ord += 10
            db().execute("INSERT INTO doc_types(name,code,sop,stage,required,link,ord,active) "
                         "VALUES(?,?,?,?,?,?,?,1)",
                         (r["name"], r["code"], r["sop"], r["stage"], r["required"], r["link"], max_ord))
            audit(None, "문서 종류", r["name"], "생성", "단계", None, r["stage"], reason)
            added += 1
    audit(None, "문서 종류", filename, "SOP 가져오기", "보관 파일", None, f"uploads/sop/{stored}",
          f"추가 {added}건, 갱신 {updated}건")
    for s in db().execute("SELECT id FROM studies WHERE status!='종료'").fetchall():
        ensure_docs(s["id"])
    db().commit()
    flash(f"SOP에서 문서 종류 {added}건을 추가하고 {updated}건을 갱신했습니다. "
          "종료되지 않은 시험의 체크리스트에도 반영했습니다.", "ok")
    return redirect(url_for("admin_doctypes"))


@app.route("/admin/audit")
@admin_required
def admin_audit():
    rows = db().execute(
        "SELECT a.*, s.study_no FROM audit a LEFT JOIN studies s ON s.id=a.study_id "
        "ORDER BY a.id DESC LIMIT 1000").fetchall()
    return render_template("audit.html", s=None, tab=None, rows=rows, tbls=[], tbl="")


@app.route("/admin/backup")
@admin_required
def admin_backup():
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    dst = sqlite3.connect(tmp)
    db().backup(dst)
    dst.close()
    with open(tmp, "rb") as f:
        data = f.read()
    os.remove(tmp)
    audit(None, "시스템", "DB", "백업 다운로드")
    db().commit()
    return Response(data, mimetype="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="cro_backup_{datetime.now():%Y%m%d_%H%M}.db"'})


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def err(e):
    msg = {400: "잘못된 요청입니다.", 403: "권한이 없습니다.", 404: "찾을 수 없습니다.",
           413: "파일이 너무 큽니다(최대 100MB)."}.get(e.code, "오류")
    desc = e.description if e.code == 400 and isinstance(e.description, str) and "요청이" in e.description else msg
    return render_template("error.html", code=e.code, msg=desc), e.code


init_db()

if __name__ == "__main__":
    host = os.environ.get("CRO_HOST", "0.0.0.0")
    port = int(os.environ.get("CRO_PORT", "8000"))
    print(f"CRO 실험 관리 프로그램: http://localhost:{port}  (다른 PC에서는 http://<이 PC의 IP>:{port})")
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, threaded=True)
