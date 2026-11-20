# -*- coding: utf-8 -*-
"""SOP 파일(HWPX·HWP·PDF)에서 서식(관리 문서) 목록을 뽑는다.

SOP_scheduler(TypeScript)의 HWPX 파서에서 서식 목록을 찾는 부분을 옮기고,
표 구조가 없는 PDF·HWP는 글자 줄에서 서식 번호와 이름을 찾는다.
결과는 후보일 뿐이며, 관리자가 미리보기 화면에서 확인한 뒤 등록한다.
"""
import io
import json
import logging
import os
import re
import struct
import zipfile
import zlib
from collections import Counter, OrderedDict
from xml.etree import ElementTree as ET

# 컬프 SOP 형식: KULF/COM/001 (SOP), KULF/COM/001-1 (서식). 다른 회사는 화면에서 바꾼다.
DEFAULT_SOP_CODE = r"[A-Z]+/[A-Z]+/\d{3}"
DEFAULT_FORM_SUFFIX = r"-\d+"

STAGE_BEFORE, STAGE_DURING, STAGE_AFTER = "시험 전", "시험 중", "시험 후"
HINTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sop_hints.json")
logging.getLogger("pypdf").setLevel(logging.ERROR)  # 손상 PDF 경고가 서버 창을 어지럽히지 않게


class SopImportError(Exception):
    """사용자에게 그대로 보여줄 수 있는 오류"""


# ------------------------------------------------------------------ 글자 정리
def _norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def squash(s):
    """공백을 모두 뺀 문자열 (이름 비교용: 'SOP 관리대장' = 'SOP관리대장')"""
    return re.sub(r"\s+", "", s or "")


# ------------------------------------------------------------------ HWPX
def _local(tag):
    return tag.rsplit("}", 1)[-1]


_HWPX_SKIP = {"header", "footer", "linesegarray"}
_HWPX_SPACE = {"tab", "lineBreak", "nbSpace", "fwSpace"}


def _hwpx_t(el):
    s = el.text or ""
    for c in el:
        s += " " if _local(c.tag) in _HWPX_SPACE else _hwpx_t(c)
        s += c.tail or ""
    return s


def _hwpx_walk(node, out):
    for c in node:
        name = _local(c.tag)
        if name == "p":
            _hwpx_para(c, out)
        elif name == "tbl":
            out.append(("table", _hwpx_table(c)))
        elif name not in _HWPX_SKIP:
            _hwpx_walk(c, out)


def _hwpx_para(p, out):
    text, embedded = "", []
    for run in p:
        if _local(run.tag) != "run":
            continue
        for c in run:
            if _local(c.tag) == "t":
                text += _hwpx_t(c)
            else:
                embedded.append(c)
    text = _norm(text)
    if text:
        out.append(("para", text))
    for e in embedded:
        name = _local(e.tag)
        if name == "tbl":
            out.append(("table", _hwpx_table(e)))
        elif name not in _HWPX_SKIP:
            _hwpx_walk(e, out)


def _int_attr(el, name, default):
    try:
        return int(el.get(name)) if el is not None and el.get(name) is not None else default
    except ValueError:
        return default


def _child(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _hwpx_table(tbl):
    """병합 셀을 풀어 모든 칸을 채운 표 (rows[r][c] = 칸의 글자)"""
    rows_n, cols_n = _int_attr(tbl, "rowCnt", 0), _int_attr(tbl, "colCnt", 0)
    grid = [[""] * cols_n for _ in range(rows_n)]
    r = 0
    for tr in tbl:
        if _local(tr.tag) != "tr":
            continue
        c = 0
        for tc in tr:
            if _local(tc.tag) != "tc":
                continue
            addr, span = _child(tc, "cellAddr"), _child(tc, "cellSpan")
            row, col = _int_attr(addr, "rowAddr", r), _int_attr(addr, "colAddr", c)
            rs = max(1, _int_attr(span, "rowSpan", 1))
            cs = max(1, _int_attr(span, "colSpan", 1))
            inner = []
            _hwpx_walk(tc, inner)
            text = " ".join(b[1] if b[0] == "para" else " ".join(" ".join(x) for x in b[1])
                            for b in inner).strip()
            for i in range(row, min(row + rs, rows_n)):
                for j in range(col, min(col + cs, cols_n)):
                    grid[i][j] = text
            c = col + cs
        r += 1
    return grid


def read_hwpx(data):
    """HWPX → [('para', 글자) | ('table', 표)]"""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SopImportError("HWPX 파일을 열 수 없습니다. 파일이 손상되었거나 HWPX 형식이 아닙니다.")
    names = sorted((n for n in z.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)),
                   key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
    if not names:
        raise SopImportError("HWPX 본문을 찾지 못했습니다. 한글에서 HWPX로 저장한 파일인지 확인하세요.")
    blocks = []
    for n in names:
        _hwpx_walk(ET.fromstring(z.read(n)), blocks)
    return blocks


# ------------------------------------------------------------------ HWP (한글 97~2022 바이너리)
# 문단 글자 레코드 안의 제어 문자. 이 문자들은 1글자, 나머지 0~31은 8글자(16바이트)를 차지한다.
_HWP_CHAR_CTRL = {0, 10, 13} | set(range(24, 32))
_HWPTAG_PARA_TEXT = 16 + 51


def _hwp_para_text(buf):
    out, i, n = [], 0, len(buf) // 2
    while i < n:
        ch = struct.unpack_from("<H", buf, i * 2)[0]
        if ch >= 32:
            out.append(chr(ch))
            i += 1
        elif ch in _HWP_CHAR_CTRL:
            if ch in (10, 13):
                out.append(" ")
            i += 1
        else:
            if ch == 9:  # 탭
                out.append(" ")
            i += 8
    return _norm("".join(out))


def read_hwp(data):
    """HWP 바이너리 → [('para', 글자)]. 표 구조는 없이 칸 안의 문단도 차례대로 나온다."""
    try:
        import olefile
    except ImportError:
        raise SopImportError("HWP를 읽는 데 필요한 olefile 패키지가 없습니다. run.bat을 다시 실행해 패키지를 설치하세요.")
    if not olefile.isOleFile(io.BytesIO(data)):
        raise SopImportError("HWP 파일을 열 수 없습니다. 한글 97 이후 형식(.hwp)인지 확인하세요.")
    ole = olefile.OleFileIO(io.BytesIO(data))
    try:
        header = ole.openstream("FileHeader").read()
        if not header.startswith(b"HWP Document File"):
            raise SopImportError("HWP 파일 머리를 읽지 못했습니다.")
        flags = struct.unpack_from("<I", header, 36)[0]
        if flags & 0x2:
            raise SopImportError("암호가 걸린 HWP 파일입니다. 암호를 풀고 저장한 뒤 올려 주세요.")
        if flags & 0x4:
            raise SopImportError("배포용 문서(읽기 전용) HWP는 읽을 수 없습니다. PDF로 저장해서 올려 주세요.")
        compressed = bool(flags & 0x1)
        sections = sorted((e for e in ole.listdir() if len(e) == 2 and e[0] == "BodyText"
                           and re.fullmatch(r"Section\d+", e[1])), key=lambda e: int(e[1][7:]))
        if not sections:
            raise SopImportError("HWP 본문을 찾지 못했습니다.")
        blocks = []
        for e in sections:
            raw = ole.openstream(e).read()
            if compressed:
                raw = zlib.decompress(raw, -15)
            pos = 0
            while pos + 4 <= len(raw):
                h = struct.unpack_from("<I", raw, pos)[0]
                pos += 4
                tag, size = h & 0x3FF, (h >> 20) & 0xFFF
                if size == 0xFFF:
                    size = struct.unpack_from("<I", raw, pos)[0]
                    pos += 4
                if tag == _HWPTAG_PARA_TEXT:
                    t = _hwp_para_text(raw[pos:pos + size])
                    if t:
                        blocks.append(("para", t))
                pos += size
        return blocks
    except (OSError, zlib.error, struct.error):
        raise SopImportError("HWP 본문을 해석하지 못했습니다. 한글에서 HWPX나 PDF로 저장해서 올려 주세요.")
    finally:
        ole.close()


# ------------------------------------------------------------------ PDF
def read_pdf(data):
    """글자 PDF → [('para', 줄)]. 스캔(이미지) PDF는 글자가 없어 읽지 못한다."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        raise SopImportError("PDF를 읽는 데 필요한 pypdf 패키지가 없습니다. run.bat을 다시 실행해 패키지를 설치하세요.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise SopImportError("암호가 걸린 PDF입니다. 암호를 풀고 저장한 뒤 올려 주세요.")
        blocks, chars = [], 0
        for page in reader.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                line = _norm(line)
                if line:
                    blocks.append(("para", line))
                    chars += len(line)
    except SopImportError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError):
        raise SopImportError("PDF를 열 수 없습니다. 파일이 손상되었는지 확인하세요.")
    if chars < 50:
        raise SopImportError("PDF에서 글자를 찾지 못했습니다. 스캔한 이미지 PDF는 읽을 수 없으니 원본(HWP·HWPX)이나 글자 PDF를 올려 주세요.")
    return blocks


def read_blocks(filename, data):
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".hwpx":
        return read_hwpx(data)
    if ext == ".hwp":
        return read_hwp(data)
    if ext == ".pdf":
        return read_pdf(data)
    raise SopImportError("HWPX, HWP, PDF 파일만 올릴 수 있습니다.")


# ------------------------------------------------------------------ 서식 찾기
_NAME_STRIP = " \t·:：-–—|‘’'\"“”「」『』<>《》[]【】,;"


def _clean_name(s):
    s = _norm(s)
    s = re.sub(r"^(?:\(\d{1,2}\)|\d{1,2}[.)]|[①-⑳]|[가-하]\.)\s*", "", s)  # 앞 번호 "(1)", "1.", "①"
    s = s.strip(_NAME_STRIP)
    s = re.sub(r"[(（]\s*$", "", s).strip(_NAME_STRIP)
    return s


def _good_name(s):
    if not s or len(s) > 40 or not re.search(r"[가-힣A-Za-z]", s):
        return False
    if re.search(r"(?:한다|된다|있다|없다|이다|하여|하고|에서|으로|에게|및$)", s):
        return False
    return True


def extract_forms(blocks, sop_code=DEFAULT_SOP_CODE, form_suffix=DEFAULT_FORM_SUFFIX):
    """블록 열에서 서식 목록을 뽑는다.

    이름의 근거는 신뢰도 순서로
      3) 표에서 서식 번호 칸의 앞 칸 (HWPX의 서식 목록 표)
      2) "(1) 서식명 (번호)" 같은 SOP 본문의 서식 항목 줄
      1) 한 줄 안에서 번호 바로 앞의 글자 (PDF로 펼쳐진 서식 목록 표, 본문 인용)
      1) 번호만 있는 줄 바로 앞 줄 (서식 페이지 제목)
    이다. 같은 신뢰도에서는 여러 번 나온 이름을 고른다.
    """
    try:
        form_re = re.compile(rf"{sop_code}{form_suffix}")
        sop_re = re.compile(rf"{sop_code}(?!{form_suffix})")
        re.compile(rf"(?:{sop_code}|{form_suffix})")
    except re.error:
        raise SopImportError("서식 번호 형식(정규식)이 올바르지 않습니다.")
    any_code = re.compile(rf"{sop_code}(?:{form_suffix})?")

    votes = OrderedDict()  # code → Counter((weight, name))
    defined = set()  # 서식 목록 표·서식 항목 줄·서식 페이지에 나온 번호 (본문 인용만 있는 번호와 구분)
    sop_titles = {}

    def vote(code, name, weight, define=False):
        votes.setdefault(code, Counter())
        if define:
            defined.add(code)
        name = _clean_name(name)
        if _good_name(name) and not any_code.search(name):
            votes[code][(weight, name)] += 1

    prev_para = ""
    for kind, payload in blocks:
        if kind == "table":
            for row in payload:
                for i, cell in enumerate(row):
                    cell = cell.strip()
                    if form_re.fullmatch(cell):
                        name = row[i - 1] if i > 0 else ""
                        vote(cell, name if name != cell else "", 3, True)
                    elif sop_re.fullmatch(cell) and i > 0 and row[i - 1] and not sop_re.search(row[i - 1]):
                        sop_titles.setdefault(cell, _clean_name(row[i - 1]))
            prev_para = ""
            continue
        line = payload
        codes = list(form_re.finditer(line))
        if not codes:
            prev_para = line
            continue
        if len(codes) == 1 and form_re.fullmatch(squash(line)):
            vote(squash(line), prev_para, 1, True)  # 서식 페이지: 번호만 있는 줄
            prev_para = line
            continue
        item = re.fullmatch(rf"\s*(?:\(\d{{1,2}}\)|\d{{1,2}}[.)])\s*(.+?)\s*[(（]\s*({sop_code}{form_suffix})\s*[)）]\s*", line)
        if item:
            vote(item.group(2), item.group(1), 2, True)
            prev_para = line
            continue
        start = 0
        for m in any_code.finditer(line):
            seg = line[start:m.start()]
            start = m.end()
            if form_re.fullmatch(m.group()):
                seg = re.sub(r"[(（‘'「『]\s*$", "", seg.rstrip())
                quoted = re.search(r"[‘'「『“\"]([^‘'「『“\"]+?)\s*$", seg)
                if quoted:
                    vote(m.group(), quoted.group(1), 1)
                elif seg.strip() and len(_clean_name(seg)) <= 25:
                    vote(m.group(), seg, 1)
                else:
                    votes.setdefault(m.group(), Counter())
            elif seg.strip():
                sop_titles.setdefault(m.group(), _clean_name(seg))
        prev_para = line

    forms = []
    for code, c in votes.items():
        name = ""
        if c:
            best_w = max(w for w, _ in c)
            name = max(((n, cnt) for (w, n), cnt in c.items() if w == best_w),
                       key=lambda x: (x[1], -len(x[0])))[0]
        sop = sop_re.match(code)
        sop = sop.group() if sop else ""
        forms.append({"code": code, "name": name, "sop": sop, "sop_title": sop_titles.get(sop, ""),
                      "defined": code in defined})
    forms.sort(key=lambda f: _natkey(f["code"]))
    return forms


def _natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


# ------------------------------------------------------------------ 단계·연계 추정
_STAGE_WORDS = [
    (STAGE_AFTER, r"결과\s*보고|보관|이관|종료|현황"),
    (STAGE_BEFORE, r"의뢰|계약|수탁|접수|계획서|승인|동의|개시|지정|업무\s*분담|이해\s*관계|입수|검역|순화|군\s*분리"),
    (STAGE_DURING, r"관찰|체중|채혈|투여|부검|검체|마취|안락사|이상\s*반응|일탈|변경|시료|증상|사육|병원체"),
]
_OPTIONAL_WORDS = r"이상\s*반응|일탈|조기\s*종료|일시\s*중지|중지|변경|사고|폐기"
_FACILITY_WORDS = r"SOP|임\s*명\s*장|개인\s*기록|교육|훈련|장비|기기|교정|출입|청소|소독|시설|비상|연간|월간|관리\s*대장"


def load_hints():
    try:
        with open(HINTS_PATH, encoding="utf-8") as f:
            return json.load(f).get("forms", {})
    except (OSError, ValueError):
        return {}


def suggest(form, hints=None):
    """서식 하나에 단계·필수·연계·포함 여부 초안을 붙인다."""
    hints = load_hints() if hints is None else hints
    name, code = form["name"], form["code"]
    h = hints.get(code)
    stage, required, basis = "", 1, ""
    if h and squash(h.get("name", "")) == squash(name):
        stage, required, basis = h.get("stage", ""), int(h.get("required", 1)), "기본 템플릿"
    if not basis:
        if re.search(_FACILITY_WORDS, name) and not re.search(r"시험\s*(?:접수|의뢰)|동물", name):
            stage, basis = "", "기관 공통 문서로 추정"
        else:
            for st, pat in _STAGE_WORDS:
                if re.search(pat, name):
                    stage, basis = st, "이름으로 추정"
                    break
        if re.search(_OPTIONAL_WORDS, name):
            required = 0
    links = []
    if re.search(r"체중", name):
        links.append("weights")
    if re.search(r"증상|관찰", name):
        links.append("clinical")
    link = ",".join(links)
    if basis == "기본 템플릿" and "link" in h:
        link = h["link"]
    if not form.get("defined", True):
        basis = "본문 언급만 있음 (서식 목록·서식 페이지에 없음, 번호 오기 확인)"
    return dict(form, stage=stage, required=required, link=link,
                include=bool(stage) and form.get("defined", True), basis=basis or "추정 못함")
