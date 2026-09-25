#!/usr/bin/env python3
"""Local teaching workload calculator for SUET faculty."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from copy import copy
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from xml.etree import ElementTree as ET


ROOT = Path(sys.executable).resolve().parent.parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = ROOT / "data"
DB_PATH = Path(os.getenv("WORKCOUNT_DB", DATA / "workcount.db"))
TEMPLATES = ROOT / "templates"
HOST = os.getenv("WORKCOUNT_HOST", "127.0.0.1")
PORT = int(os.getenv("WORKCOUNT_PORT", "8765"))
SECURE_COOKIES = os.getenv("WORKCOUNT_SECURE_COOKIES", "0") == "1"
MAX_CONCURRENT_LOGINS = int(os.getenv("WORKCOUNT_MAX_LOGINS", "4"))
MAX_UPLOAD = 25 * 1024 * 1024
SESSION_TTL = 8 * 60 * 60
SESSIONS: dict[str, dict] = {}
LOGIN_JOBS: dict[str, dict] = {}
LOGIN_LOCK = threading.Lock()
LOGIN_JOB_TTL = 10 * 60
LOGIN_TIMEOUT = 150
REQUIRED_TEMPLATES = (
    "个人学时统计表模板.xlsx",
    "决算工作量表模板.xlsx",
    "预算工作量表模板.xlsx",
)


def connector_python() -> str:
    configured = os.getenv("WORKCOUNT_CONNECTOR_PYTHON")
    if configured:
        return configured
    local = ROOT / ".venv" / "bin" / "python"
    return str(local if local.exists() else sys.executable)


def connector_command() -> list[str]:
    if getattr(sys, "frozen", False):
        name = "PlatformConnector.exe" if os.name == "nt" else "PlatformConnector"
        return [str(ROOT / "PlatformConnector" / name)]
    return [connector_python(), str(ROOT / "tools" / "platform_sync.py")]


def connector_environment() -> dict[str, str]:
    """Use a writable native temp directory and a fixed pipe encoding on every OS."""
    return {
        **os.environ,
        "PYTHONPYCACHEPREFIX": str(Path(tempfile.gettempdir()) / "workcount-platform-pycache"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }


def connector_result(stdout: str, stderr: str, returncode: int, fallback: str) -> dict:
    """Decode the connector's final JSON response while preserving a useful error."""
    try:
        lines = (stdout or "").strip().splitlines()
        if not lines:
            raise ValueError("connector produced no JSON")
        result = json.loads(lines[-1])
    except (json.JSONDecodeError, IndexError, ValueError) as exc:
        detail = next((line.strip() for line in reversed((stderr or "").splitlines()) if line.strip()), "")
        LOG.error("platform connector failed without JSON: returncode=%s stderr=%s", returncode, (stderr or "")[-1000:])
        message = f"{fallback}：{detail[:300]}" if detail else fallback
        raise AppError(message, "PLATFORM_RESPONSE_INVALID", 502) from exc
    return result


def session_cookie(token: str, max_age: int) -> str:
    secure = "; Secure" if SECURE_COOKIES else ""
    return f"workcount_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

logging.basicConfig(level=logging.INFO, format='{"time":"%(asctime)s","level":"%(levelname)s","message":"%(message)s"}')
LOG = logging.getLogger("workcount")


class AppError(Exception):
    def __init__(self, message: str, code: str = "BAD_REQUEST", status: int = 400):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK(kind IN ('theory','training','internship','thesis','defense','social','manual')),
  employee_id TEXT NOT NULL DEFAULT '',
  employee_name TEXT NOT NULL DEFAULT '',
  semester TEXT NOT NULL DEFAULT '2025-2026-2',
  course TEXT NOT NULL,
  class_name TEXT NOT NULL DEFAULT '',
  course_code TEXT NOT NULL DEFAULT '',
  student_count REAL NOT NULL DEFAULT 0,
  total_hours REAL NOT NULL DEFAULT 0,
  experiment_hours REAL NOT NULL DEFAULT 0,
  experiment_students REAL NOT NULL DEFAULT 0,
  category_coeff REAL NOT NULL DEFAULT 1,
  repeat_coeff REAL NOT NULL DEFAULT 1,
  course_coeff REAL NOT NULL DEFAULT 1,
  online_coeff REAL NOT NULL DEFAULT 1,
  practice_coeff REAL NOT NULL DEFAULT 0,
  weeks REAL NOT NULL DEFAULT 0,
  instructors REAL NOT NULL DEFAULT 1,
  enterprise_coeff REAL NOT NULL DEFAULT 1,
  manual_workload REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS monthly_hours (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  semester TEXT NOT NULL,
  employee_id TEXT NOT NULL,
  employee_name TEXT NOT NULL,
  college TEXT NOT NULL DEFAULT '',
  department TEXT NOT NULL DEFAULT '',
  month_key TEXT NOT NULL,
  theory_hours REAL NOT NULL DEFAULT 0,
  practice_hours REAL NOT NULL DEFAULT 0,
  UNIQUE(semester, employee_id, employee_name, month_key)
);
CREATE TABLE IF NOT EXISTS schedule_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  semester TEXT NOT NULL,
  employee_id TEXT NOT NULL,
  employee_name TEXT NOT NULL,
  event_date TEXT NOT NULL,
  academic_week INTEGER NOT NULL,
  weekday INTEGER NOT NULL,
  course TEXT NOT NULL,
  class_name TEXT NOT NULL DEFAULT '',
  student_count REAL NOT NULL DEFAULT 0,
  periods TEXT NOT NULL,
  hours REAL NOT NULL DEFAULT 0,
  category TEXT NOT NULL CHECK(category IN ('theory','practice')),
  source TEXT NOT NULL DEFAULT 'platform'
);
CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);
CREATE INDEX IF NOT EXISTS idx_schedule_teacher_term ON schedule_events(semester, employee_id, employee_name);
"""

THEORY_SEED = [
    ("Python高级应用", "24大数据本1（春）", "3244107507", 60, 46, 18, 60, 1.2, 1.0),
    ("Python高级应用", "24大数据本2（春）", "3244107507", 54, 42, 14, 54, 1.2, 0.9),
    ("Python高级应用", "24大数据本3（夏）", "3244107502", 50, 38, 10, 50, 1.2, 1.0),
    ("Python高级应用", "24大数据本4（夏）", "3244107502", 47, 42, 14, 50, 1.2, 0.9),
    ("Python程序设计", "24大数据专1（夏）", "3243101601", 47, 54, 26, 54, 1.0, 1.0),
]

PRACTICE_SEED = [
    ("training", "Python程序设计实战", "24大数据专1（夏）", "3243101808", 47, 10, .5, 1, 2, 1, 0),
    ("training", "认知实习", "25大数据本3（夏）", "0325412801", 55, 20, .5, 2, 2, 1, 0),
    ("thesis", "毕业论文（设计）", "24大数据（专升本）1", "324207808", 6, 36, 6, 10, 1, 1, 0),
    ("defense", "开题答辩", "24大数据（专升本）1", "", 15, 0, 0, 0, 3, 1, 0),
    ("defense", "毕业答辩", "24大数据（专升本）1", "", 24, 0, 0, 0, 5, 1, 0),
    ("thesis", "毕业设计", "23大数据专1（A类）", "3243102807", 7, 21, .3, 11, 1, 1, 0),
]


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(seed_demo: bool = False) -> None:
    DATA.mkdir(exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
        for name in ("employee_id", "employee_name", "experiment_students", "semester"):
            if name not in columns:
                if name == "experiment_students": col_type, default = "REAL", "0"
                elif name == "semester": col_type, default = "TEXT", "'2025-2026-2'"
                else: col_type, default = "TEXT", "''"
                conn.execute(f"ALTER TABLE items ADD COLUMN {name} {col_type} NOT NULL DEFAULT {default}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_teacher_term ON items(semester, employee_id, employee_name)")
        if not seed_demo:
            return
        count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        for month, theory, practice in [("3月",34,32),("4月",36,62),("5月",28,16),("6-7月",42,26)]:
            conn.execute("""INSERT OR IGNORE INTO monthly_hours(semester,employee_id,employee_name,college,department,month_key,theory_hours,practice_hours)
            VALUES('2025-2026-2','0000000','示例教师','示例学院','示例教研室',?,?,?)""", (month,theory,practice))
        if count:
            return
        now = datetime.now(timezone.utc).isoformat()
        for course, class_name, code, students, hours, exp_hours, exp_students, cat, repeat in THEORY_SEED:
            conn.execute(
                """INSERT INTO items(kind,employee_id,employee_name,course,class_name,course_code,student_count,total_hours,
                experiment_hours,experiment_students,category_coeff,repeat_coeff,course_coeff,online_coeff,source,created_at,updated_at)
                VALUES('theory','0000000','示例教师',?,?,?,?,?,?,?,?,?,1,1,'示例决算表',?,?)""",
                (course, class_name, code, students, hours, exp_hours, exp_students, cat, repeat, now, now),
            )
        for kind, course, class_name, code, students, hours, coeff, weeks, instructors, enterprise, manual in PRACTICE_SEED:
            conn.execute(
                """INSERT INTO items(kind,employee_id,employee_name,course,class_name,course_code,student_count,total_hours,
                practice_coeff,weeks,instructors,enterprise_coeff,manual_workload,source,created_at,updated_at)
                VALUES(?,'0000000','示例教师',?,?,?,?,?,?,?,?,?,?,'示例决算表',?,?)""",
                (kind, course, class_name, code, students, hours, coeff, weeks, instructors, enterprise, manual, now, now),
            )


def clamp_students(value: float) -> float:
    return min(75.0, max(50.0, value))


def calculated_practice_hours(row: dict, students: float, weeks: float, coeff: float, fallback: float) -> float:
    kind = row["kind"]
    if kind == "training":
        return weeks * 10
    if kind == "internship":
        return weeks * students * .5
    if kind == "thesis":
        return students * (6 if coeff >= 1 else 3)
    if kind == "defense":
        return 0.0
    return fallback


def calculate(row: dict) -> dict:
    kind = row["kind"]
    students = max(0.0, float(row.get("student_count") or 0))
    hours = max(0.0, float(row.get("total_hours") or 0))
    details = ""
    workload = 0.0
    theory_workload = practice_workload = theory_hours = practice_hours = 0.0

    if kind == "theory":
        exp_hours = min(hours, max(0.0, float(row.get("experiment_hours") or 0)))
        adjusted = clamp_students(students)
        experiment_students = float(row.get("experiment_students") or adjusted)
        size_coeff = 1 + max(0, adjusted - 50) * .01
        category = float(row.get("category_coeff") or 1)
        repeat = float(row.get("repeat_coeff") or 1)
        course = float(row.get("course_coeff") or 1)
        online = float(row.get("online_coeff") or 1)
        exp_work = experiment_students * exp_hours * category * repeat * .02 * course
        lecture_hours = hours - exp_hours
        lecture_coeff = repeat * size_coeff * course * category * online
        lecture_work = lecture_hours * lecture_coeff
        workload = exp_work + lecture_work
        theory_workload, theory_hours = workload, hours
        details = f"理论 {lecture_work:.4f} + 课内实验 {exp_work:.4f}"
    else:
        coeff = float(row.get("practice_coeff") or 0)
        weeks = max(0.0, float(row.get("weeks") or 0))
        instructors = max(1.0, float(row.get("instructors") or 1))
        enterprise = float(row.get("enterprise_coeff") or 1)
        practice_hours = calculated_practice_hours(row, students, weeks, coeff, hours)
        if kind == "training":
            workload = coeff * students * weeks * enterprise / instructors
            details = "系数 × 学生数 × 周数 × 校企系数 ÷ 指导教师数"
        elif kind == "internship":
            workload = coeff * students / instructors
            details = "岗位实习系数 × 学生数 ÷ 指导教师数"
        elif kind == "thesis":
            workload = coeff * students * (weeks if coeff < 1 and weeks else 1) / instructors
            details = "毕业设计系数 × 学生数" + (" × 周数" if coeff < 1 and weeks else "")
        elif kind == "defense":
            workload = students / instructors
            details = "答辩学生数 ÷ 指导教师数"
        elif kind == "social":
            workload = students * hours * (coeff or .02)
            details = "学生数 × 学时 × 社会实践系数"
        elif kind == "manual":
            workload = float(row.get("manual_workload") or 0)
            details = "手工核定"
        practice_workload = workload

    return {
        **row,
        "workload": round(workload, 4),
        "theory_workload": round(theory_workload, 4),
        "practice_workload": round(practice_workload, 4),
        "theory_hours": round(theory_hours, 4),
        "practice_hours": round(practice_hours, 4),
        "display_hours": round(theory_hours + practice_hours, 4),
        "calculation": details,
    }


FIELDS = {
    "kind", "employee_id", "employee_name", "semester", "course", "class_name", "course_code", "student_count", "total_hours",
    "experiment_hours", "experiment_students", "category_coeff", "repeat_coeff", "course_coeff", "online_coeff",
    "practice_coeff", "weeks", "instructors", "enterprise_coeff", "manual_workload", "source",
}
NUMERIC_FIELDS = FIELDS - {"kind", "employee_id", "employee_name", "semester", "course", "class_name", "course_code", "source"}
KINDS = {"theory", "training", "internship", "thesis", "defense", "social", "manual"}


def validate_item(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise AppError("请求内容必须是对象", "VALIDATION_ERROR", 422)
    clean = {key: payload.get(key) for key in FIELDS if key in payload}
    clean["kind"] = str(clean.get("kind") or "").strip()
    clean["course"] = str(clean.get("course") or "").strip()
    clean["semester"] = str(clean.get("semester") or "2025-2026-2").strip()
    if clean["kind"] not in KINDS:
        raise AppError("工作量类型无效", "VALIDATION_ERROR", 422)
    if not clean["course"]:
        raise AppError("课程或项目名称不能为空", "VALIDATION_ERROR", 422)
    for key in FIELDS - NUMERIC_FIELDS - {"kind", "course"}:
        clean[key] = str(clean.get(key) or "").strip()
    for key in NUMERIC_FIELDS:
        try:
            clean[key] = float(clean.get(key) or (1 if key in {"category_coeff", "repeat_coeff", "course_coeff", "online_coeff", "instructors", "enterprise_coeff"} else 0))
        except (TypeError, ValueError):
            raise AppError(f"{key} 必须是数字", "VALIDATION_ERROR", 422)
        if clean[key] < 0:
            raise AppError(f"{key} 不能小于 0", "VALIDATION_ERROR", 422)
    return clean


def item_list(employee_id: str = "", employee_name: str = "", semester: str = "") -> list[dict]:
    with db() as conn:
        sql = "SELECT * FROM items WHERE 1=1"
        params = []
        if employee_id:
            sql += " AND employee_id=?"; params.append(employee_id.strip())
        if employee_name:
            sql += " AND employee_name LIKE ?"; params.append(f"%{employee_name.strip()}%")
        if semester:
            sql += " AND semester=?"; params.append(semester.strip())
        sql += " ORDER BY employee_name, kind='theory' DESC, id"
        return [calculate(dict(r)) for r in conn.execute(sql, params)]


def summary(items: list[dict]) -> dict:
    totals = {
        "theory_hours": sum(x["theory_hours"] for x in items),
        "theory_workload": sum(x["theory_workload"] for x in items),
        "practice_hours": sum(x["practice_hours"] for x in items),
        "practice_workload": sum(x["practice_workload"] for x in items),
    }
    totals["total_hours"] = totals["theory_hours"] + totals["practice_hours"]
    totals["total_workload"] = totals["theory_workload"] + totals["practice_workload"]
    totals["item_count"] = len(items)
    return {k: round(v, 4) if isinstance(v, float) else v for k, v in totals.items()}


def semester_catalog(employee_id: str = "", employee_name: str = "") -> list[dict]:
    current_year = datetime.now().year
    semesters = [f"{year}-{year + 1}-{term}" for year in range(current_year, 2014, -1) for term in (2, 1)]
    with db() as conn:
        workload_sql = "SELECT semester, COUNT(*) count FROM items WHERE 1=1"
        monthly_sql = "SELECT semester, COUNT(*) count FROM monthly_hours WHERE 1=1"
        params, monthly_params = [], []
        if employee_id:
            workload_sql += " AND employee_id=?"; monthly_sql += " AND employee_id=?"; params.append(employee_id); monthly_params.append(employee_id)
        if employee_name:
            workload_sql += " AND employee_name LIKE ?"; monthly_sql += " AND employee_name LIKE ?"; params.append(f"%{employee_name}%"); monthly_params.append(f"%{employee_name}%")
        workload_sql += " GROUP BY semester"; monthly_sql += " GROUP BY semester"
        work_counts = {row["semester"]: row["count"] for row in conn.execute(workload_sql, params)}
        hour_counts = {row["semester"]: row["count"] for row in conn.execute(monthly_sql, monthly_params)}
    known = set(semesters) | set(work_counts) | set(hour_counts)
    return [{"value": value, "label": semester_text(value), "workload_count": work_counts.get(value, 0), "monthly_count": hour_counts.get(value, 0), "has_data": bool(work_counts.get(value) or hour_counts.get(value))} for value in sorted(known, reverse=True)]


def canonical_teacher_name(employee_id: str, fallback: str = "") -> str:
    with db() as conn:
        for table in ("items", "monthly_hours", "schedule_events"):
            row = conn.execute(
                f"SELECT employee_name FROM {table} WHERE employee_id=? AND employee_name<>'' ORDER BY id DESC LIMIT 1",
                (employee_id,),
            ).fetchone()
            if row and row["employee_name"]:
                return row["employee_name"]
    return fallback or employee_id


def sync_from_platform(payload: dict, persist: bool = False, progress_callback=None) -> dict:
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    requested_id = str(payload.get("employee_id") or username).strip()
    requested_name = str(payload.get("employee_name") or "").strip()
    if not username or (not password and not payload.get("cookies")):
        raise AppError("请输入平台账号和密码", "PLATFORM_CREDENTIALS_REQUIRED", 422)
    command = connector_command()
    request = json.dumps({"username": username, "password": password, "employee_id": requested_id, "employee_name": requested_name, "semester": str(payload.get("semester") or ""), "cookies": payload.get("cookies") or []}, ensure_ascii=False) + "\n"
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", cwd=ROOT,
            env=connector_environment(),
        )
        proc.stdin.write(request); proc.stdin.close()
        stderr_lines = []
        def read_stderr() -> None:
            for line in proc.stderr:
                line = line.rstrip("\n"); stderr_lines.append(line)
                if line.startswith("PROGRESS ") and progress_callback:
                    try:
                        update = json.loads(line[9:])
                        progress_callback(int(update.get("percent", 0)), str(update.get("message", "正在同步")))
                    except (ValueError, TypeError, json.JSONDecodeError):
                        pass
        stderr_thread = threading.Thread(target=read_stderr, daemon=True); stderr_thread.start()
        stdout = proc.stdout.read()
        proc.wait(timeout=300)
        stderr_thread.join(timeout=2)
        proc.stdout.close(); proc.stderr.close()
        stderr = "\n".join(stderr_lines)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        raise AppError("平台响应超时，请稍后重试", "PLATFORM_TIMEOUT", 504) from exc
    finally:
        password = ""
        request = ""
    result = connector_result(stdout, stderr, proc.returncode, "平台连接器未能正常启动")
    if proc.returncode or result.get("error"):
        raise AppError(result.get("error") or "平台同步失败", "PLATFORM_SYNC_FAILED", 502)
    actual_id = str(result.get("employee_id") or "").strip()
    actual_name = str(result.get("employee_name") or "").strip()
    if requested_id and actual_id and requested_id != actual_id:
        raise AppError(f"当前平台账号只能读取工号 {actual_id} 的数据", "PLATFORM_ID_MISMATCH", 403)
    if requested_name and actual_name and requested_name != actual_name:
        raise AppError(f"当前平台账号对应教师为 {actual_name}", "PLATFORM_NAME_MISMATCH", 403)
    records = [validate_item(item) for item in result.get("items", [])]
    semesters = sorted(set(result.get("semesters", [])) | {x["semester"] for x in records})
    monthly_rows = [dict(row) for row in result.get("monthly_hours", [])]
    schedule_events = [dict(row) for row in result.get("schedule_events", [])]
    public = {"employee_id": actual_id, "employee_name": actual_name, "semesters": semesters, "items": len(records), "monthly_rows": len(monthly_rows), "schedule_events": len(schedule_events)}
    if persist:
        now = datetime.now(timezone.utc).isoformat()
        with db() as conn:
            for semester in semesters:
                conn.execute("DELETE FROM items WHERE semester=? AND employee_id=?", (semester, actual_id))
                conn.execute("DELETE FROM monthly_hours WHERE semester=? AND employee_id=?", (semester, actual_id))
                conn.execute("DELETE FROM schedule_events WHERE semester=? AND employee_id=?", (semester, actual_id))
            for rec in records:
                cols = sorted(rec)
                conn.execute(f"INSERT INTO items({','.join(cols)},created_at,updated_at) VALUES({','.join('?' for _ in cols)},?,?)", [rec[c] for c in cols] + [now, now])
            for row in monthly_rows:
                conn.execute("""INSERT INTO monthly_hours(semester,employee_id,employee_name,college,department,month_key,theory_hours,practice_hours)
                    VALUES(?,?,?,?,?,?,?,?)""", (row["semester"], actual_id, actual_name, row.get("college", ""), row.get("department", ""), row["month_key"], float(row.get("theory_hours", 0)), float(row.get("practice_hours", 0))))
            for event in schedule_events:
                conn.execute("""INSERT INTO schedule_events(semester,employee_id,employee_name,event_date,academic_week,weekday,course,class_name,student_count,periods,hours,category,source)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (event["semester"], actual_id, actual_name, event["event_date"], int(event["academic_week"]), int(event["weekday"]), event["course"], event.get("class_name", ""), float(event.get("student_count", 0)), event["periods"], float(event.get("hours", 0)), event["category"], event.get("source", "platform")))
    return {**public, "records": records, "monthly_data": monthly_rows, "event_data": schedule_events, "platform_cookies": result.get("cookies") or []}


def authenticate_platform(payload: dict) -> dict:
    """Validate the platform account quickly without waiting for all-term sync."""
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        raise AppError("请输入平台账号和密码", "PLATFORM_CREDENTIALS_REQUIRED", 422)
    command = connector_command()
    request = json.dumps({"username": username, "password": password, "employee_id": username, "mode": "auth"}, ensure_ascii=False) + "\n"
    try:
        proc = subprocess.run(
            command,
            input=request, text=True, encoding="utf-8", errors="replace", capture_output=True, cwd=ROOT, timeout=60,
            env=connector_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError("平台登录响应超时，请稍后重试", "PLATFORM_TIMEOUT", 504) from exc
    finally:
        password = ""
        request = ""
    result = connector_result(proc.stdout, proc.stderr, proc.returncode, "平台登录组件未能正常启动")
    if proc.returncode or result.get("error") or not result.get("authenticated"):
        raise AppError(result.get("error") or "平台账号验证失败", "PLATFORM_LOGIN_FAILED", 401)
    return result


def platform_catalog(payload: dict, progress_callback=None) -> dict:
    """Read only the teacher identity and available semesters."""
    username = str(payload.get("username") or "").strip(); password = str(payload.get("password") or "")
    command = connector_command()
    request = json.dumps({"username": username, "password": password, "employee_id": username, "mode": "catalog"}, ensure_ascii=False) + "\n"
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", cwd=ROOT,
            env=connector_environment(),
        )
        proc.stdin.write(request); proc.stdin.close()
        stdout_lines=[]; stderr_lines=[]
        def read_stdout() -> None:
            stdout_lines.extend(proc.stdout.readlines())
        def read_stderr() -> None:
            for line in proc.stderr:
                line=line.rstrip("\n"); stderr_lines.append(line)
                if line.startswith("PROGRESS ") and progress_callback:
                    try:
                        update=json.loads(line[9:]); progress_callback(int(update.get("percent",0)),str(update.get("message","正在登录青果")))
                    except (ValueError,TypeError,json.JSONDecodeError): pass
        stdout_thread=threading.Thread(target=read_stdout,daemon=True); stderr_thread=threading.Thread(target=read_stderr,daemon=True)
        stdout_thread.start(); stderr_thread.start(); proc.wait(timeout=LOGIN_TIMEOUT)
        stdout_thread.join(timeout=2); stderr_thread.join(timeout=2)
        stdout="".join(stdout_lines); stderr="\n".join(stderr_lines)
        proc.stdout.close(); proc.stderr.close()
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait(timeout=5)
        raise AppError("平台登录组件超时。请确认 Windows 已安装最新版 Chrome 或 Edge，并能访问青果平台", "PLATFORM_TIMEOUT", 504) from exc
    finally:
        password=""; request=""
    result = connector_result(stdout, stderr, proc.returncode, "平台登录组件未能正常启动")
    if proc.returncode or result.get("error"):
        raise AppError(result.get("error") or "平台登录失败", "PLATFORM_LOGIN_FAILED", 401)
    if not result.get("semesters"):
        raise AppError("青果已登录，但未能读取学期列表，请稍后重试", "PLATFORM_SEMESTERS_EMPTY", 502)
    return result


def start_login_job(payload: dict) -> str:
    """Run the slow platform sync outside the HTTP request connection."""
    now = time.time()
    job_id = secrets.token_urlsafe(24)
    with LOGIN_LOCK:
        for key, job in list(LOGIN_JOBS.items()):
            if job["status"] != "running" and now - job["created"] > LOGIN_JOB_TTL:
                LOGIN_JOBS.pop(key, None)
        if sum(job["status"] == "running" for job in LOGIN_JOBS.values()) >= MAX_CONCURRENT_LOGINS:
            raise AppError("当前登录请求较多，请稍后重试", "LOGIN_BUSY", 429)
        LOGIN_JOBS[job_id] = {"status": "running", "created": now, "percent": 2, "message": "正在启动登录任务"}

    def worker() -> None:
        try:
            def update_progress(percent: int, message: str) -> None:
                with LOGIN_LOCK:
                    if job_id in LOGIN_JOBS:
                        LOGIN_JOBS[job_id].update({"percent": max(int(LOGIN_JOBS[job_id].get("percent",0)),min(99,percent)), "message": message})
            catalog = platform_catalog(payload, update_progress)
            actual_id = str(catalog.get("employee_id") or payload["username"])
            actual_name = str(catalog.get("employee_name") or actual_id)
            token = secrets.token_urlsafe(32)
            SESSIONS[token] = {
                "employee_id": actual_id,
                "employee_name": actual_name,
                "expires": time.time() + SESSION_TTL,
                "records": [], "monthly_data": [], "event_data": [], "semesters": [],
                "available_semesters": sorted(set(catalog.get("semesters", [])), reverse=True),
                "loaded_semesters": set(), "syncing": False, "sync_percent": 0, "sync_message": "请选择学期读取数据",
                "platform_username": payload["username"], "platform_password": payload["password"],
                "platform_cookies": catalog.get("cookies") or [],
            }
            public = {"employee_id": actual_id, "employee_name": actual_name, "semesters": SESSIONS[token]["available_semesters"], "items": 0, "monthly_rows": 0, "schedule_events": 0, "syncing": False}
            update = {"status": "done", "percent": 100, "message": "登录成功，学期列表已就绪", "result": public, "session_token": token}
        except AppError as exc:
            update = {"status": "error", "error": {"message": exc.message, "code": exc.code, "http_status": exc.status}}
        except Exception:
            LOG.exception("background platform login failed")
            update = {"status": "error", "error": {"message": "平台登录或同步失败，请稍后重试", "code": "PLATFORM_LOGIN_FAILED", "http_status": 502}}
        finally:
            payload["password"] = ""
        with LOGIN_LOCK:
            if job_id in LOGIN_JOBS:
                LOGIN_JOBS[job_id].update(update)

    threading.Thread(target=worker, name=f"platform-login-{job_id[:8]}", daemon=True).start()
    return job_id


def start_semester_sync(session: dict, semester: str) -> None:
    if session.get("syncing"):
        raise AppError("已有学期正在更新，请等待完成", "SYNC_IN_PROGRESS", 409)
    if semester not in session.get("available_semesters", []):
        raise AppError("该学期不在当前账号的青果学期列表中", "SEMESTER_NOT_AVAILABLE", 404)
    session.update({"syncing": True, "sync_percent": 3, "sync_message": f"正在准备读取 {semester_text(semester)}", "sync_error": None, "sync_result": None, "sync_semester": semester})
    payload = {"username": session["platform_username"], "password": session["platform_password"], "cookies": session.get("platform_cookies") or [], "employee_id": session["employee_id"], "employee_name": "", "semester": semester}

    def update_progress(percent: int, message: str) -> None:
        session["sync_percent"] = max(session.get("sync_percent", 0), min(99, percent)); session["sync_message"] = message

    def worker() -> None:
        try:
            result = sync_from_platform(payload, persist=False, progress_callback=update_progress)
            session["records"] = [item for item in session.get("records", []) if item.get("semester") != semester] + result["records"]
            session["monthly_data"] = [row for row in session.get("monthly_data", []) if row.get("semester") != semester] + result["monthly_data"]
            session["event_data"] = [row for row in session.get("event_data", []) if row.get("semester") != semester] + result["event_data"]
            session.setdefault("loaded_semesters", set()).add(semester)
            if result.get("platform_cookies"):
                session["platform_cookies"] = result["platform_cookies"]
            session.update({"employee_id": result["employee_id"], "employee_name": result["employee_name"], "syncing": False, "sync_percent": 100, "sync_message": f"{semester_text(semester)}更新完成", "sync_result": {key: result[key] for key in ("employee_id", "employee_name", "semesters", "items", "monthly_rows", "schedule_events")}})
        except Exception as exc:
            LOG.exception("semester platform sync failed")
            session.update({"syncing": False, "sync_error": str(exc), "sync_message": f"{semester_text(semester)}更新失败"})
        finally:
            payload["password"] = ""

    threading.Thread(target=worker, name=f"semester-sync-{semester}", daemon=True).start()


def xlsx_rows(content: bytes, wanted: str) -> list[list[object]]:
    """Read visible values from one OOXML worksheet without third-party packages."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    rel_ns = {"p": "http://schemas.openxmlformats.org/package/2006/relationships"}
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.findall(".//m:t", ns)) for si in root.findall("m:si", ns)]
        book = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        targets = {r.attrib["Id"]: r.attrib["Target"] for r in rels.findall("p:Relationship", rel_ns)}
        target = None
        for sheet in book.findall("m:sheets/m:sheet", ns):
            if sheet.attrib.get("name") == wanted:
                target = targets[sheet.attrib[f'{{{ns["r"]}}}id']]
                break
        if not target:
            return []
        path = "xl/" + target.lstrip("/").replace("xl/", "")
        root = ET.fromstring(zf.read(path))
        result = []
        for row in root.findall(".//m:sheetData/m:row", ns):
            values = {}
            for cell in row.findall("m:c", ns):
                ref = cell.attrib.get("r", "A1")
                col = 0
                for ch in re.match(r"[A-Z]+", ref).group(0):
                    col = col * 26 + ord(ch) - 64
                value_node = cell.find("m:v", ns)
                inline = cell.find("m:is", ns)
                value: object = ""
                if inline is not None:
                    value = "".join(t.text or "" for t in inline.findall(".//m:t", ns))
                elif value_node is not None:
                    raw = value_node.text or ""
                    if cell.attrib.get("t") == "s": value = shared[int(raw)]
                    else:
                        try: value = float(raw)
                        except ValueError: value = raw
                values[col] = value
            if values:
                result.append([values.get(i, "") for i in range(1, max(values) + 1)])
        return result


def cell(row: list, index: int, default=0):
    return row[index] if index < len(row) and row[index] not in (None, "") else default


def import_workbook(content: bytes, filename: str, semester: str = "2025-2026-2") -> tuple[int, list[str]]:
    if not filename.lower().endswith(".xlsx"):
        raise AppError("当前直接导入支持 .xlsx；旧版 .xls 请先另存为 .xlsx", "UNSUPPORTED_FILE", 415)
    try:
        theory_rows = xlsx_rows(content, "校内教师理论工作量")
        practice_rows = xlsx_rows(content, "实践工作量")
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise AppError("文件不是有效的 Excel 工作簿", "INVALID_XLSX", 422) from exc
    if not theory_rows and not practice_rows:
        raise AppError("未找到“校内教师理论工作量”或“实践工作量”工作表", "UNKNOWN_TEMPLATE", 422)
    imported, warnings, records = 0, [], []
    theory_identity = {"employee_id": "", "employee_name": "", "category": ""}
    for row in theory_rows:
        if cell(row, 3, ""): theory_identity["employee_id"] = cell(row, 3, "")
        if cell(row, 4, ""): theory_identity["employee_name"] = cell(row, 4, "")
        if cell(row, 6, ""): theory_identity["category"] = cell(row, 6, "")
        if str(theory_identity["employee_id"]) in {"", "工号"} or str(theory_identity["category"]) != "课堂教学" or not cell(row, 7, "") or not cell(row, 11, ""):
            continue
        records.append(validate_item({
            "kind": "theory", "employee_id": theory_identity["employee_id"], "employee_name": theory_identity["employee_name"], "semester": semester,
            "course": cell(row, 7, ""), "course_code": cell(row, 8, ""),
            "class_name": str(cell(row, 11, "")).strip(), "student_count": cell(row, 12),
            "total_hours": cell(row, 18), "experiment_hours": cell(row, 20), "experiment_students": cell(row, 19),
            "category_coeff": cell(row, 21, 1), "repeat_coeff": cell(row, 23, 1),
            "course_coeff": cell(row, 25, 1), "online_coeff": cell(row, 34, 1), "source": filename,
        }))
    kind_map = {"集中实训": "training", "认知实习": "training", "岗位实习": "internship", "毕业论文": "thesis", "社会实践": "social"}
    practice_identity = {"employee_id": "", "employee_name": ""}
    for row in practice_rows:
        if cell(row, 3, ""): practice_identity["employee_id"] = cell(row, 3, "")
        if cell(row, 4, ""): practice_identity["employee_name"] = cell(row, 4, "")
        label = str(cell(row, 6, ""))
        course = str(cell(row, 7, ""))
        if str(practice_identity["employee_id"]) in {"", "工号"} or not label or not course:
            continue
        if cell(row, 32, ""):
            kind, coeff, students, weeks, instructors, hours = "defense", 0, cell(row, 33), 0, cell(row, 34, 1), 0
        else:
            kind = kind_map.get(label)
            if not kind:
                continue
            if kind == "training": coeff, students, weeks, instructors, hours = cell(row, 10), cell(row, 13, cell(row, 11)), cell(row, 15), cell(row, 16, 1), cell(row, 18)
            elif kind == "internship": coeff, students, weeks, instructors, hours = cell(row, 20), cell(row, 21), cell(row, 23), 1, cell(row, 24)
            elif kind == "thesis": coeff, students, weeks, instructors, hours = cell(row, 26), cell(row, 27), cell(row, 28), cell(row, 29, 1), cell(row, 31)
            else: coeff, students, weeks, instructors, hours = cell(row, 38, .02), cell(row, 37), 0, 1, cell(row, 36)
        records.append(validate_item({"kind": kind, "employee_id": practice_identity["employee_id"], "employee_name": practice_identity["employee_name"], "semester": semester, "course": course, "course_code": cell(row, 8, ""), "class_name": cell(row, 9, ""), "student_count": students, "total_hours": hours, "practice_coeff": coeff, "weeks": weeks, "instructors": instructors, "enterprise_coeff": cell(row, 14, 1), "source": filename}))
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute("DELETE FROM items WHERE semester=?", (semester,))
        for rec in records:
            cols = sorted(rec)
            conn.execute(f"INSERT INTO items({','.join(cols)},created_at,updated_at) VALUES({','.join('?' for _ in cols)},?,?)", [rec[c] for c in cols] + [now, now])
            imported += 1
    if not imported:
        warnings.append("模板中没有识别到可导入的记录")
    return imported, warnings


def import_personal_hours_workbook(content: bytes, semester: str) -> tuple[int, str, str]:
    rows = xlsx_rows(content, "表1-统计表")
    target = None
    for row in rows:
        if len(row) >= 13 and str(cell(row, 3, "")) not in {"", "工号"} and str(cell(row, 4, "")) not in {"", "教师姓名"}:
            target = row
            break
    if not target:
        raise AppError("个人学时表中没有识别到教师统计行", "UNKNOWN_PERSONAL_TEMPLATE", 422)
    employee_id, employee_name = str(cell(target, 3, "")).strip(), str(cell(target, 4, "")).strip()
    college, department = str(cell(target, 1, "")).strip(), str(cell(target, 2, "")).strip()
    monthly = [("3月", cell(target, 5), cell(target, 6)), ("4月", cell(target, 7), cell(target, 8)), ("5月", cell(target, 9), cell(target, 10)), ("6-7月", cell(target, 11), cell(target, 12))]
    with db() as conn:
        for month, theory, practice in monthly:
            conn.execute("""INSERT INTO monthly_hours(semester,employee_id,employee_name,college,department,month_key,theory_hours,practice_hours)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(semester,employee_id,employee_name,month_key) DO UPDATE SET
            college=excluded.college,department=excluded.department,theory_hours=excluded.theory_hours,practice_hours=excluded.practice_hours""",
            (semester, employee_id, employee_name, college, department, month, float(theory or 0), float(practice or 0)))
    return 4, employee_id, employee_name


def xml_escape(value: object) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def excel_col(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def xlsx_cell(ref: str, value: object = "", style: int = 0, formula: str | None = None, cached: float = 0) -> str:
    style_attr = f' s="{style}"' if style else ""
    if formula is not None:
        return f'<c r="{ref}"{style_attr}><f>{xml_escape(formula)}</f><v>{cached}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"{style_attr}><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"{style_attr}><is><t>{xml_escape(value)}</t></is></c>'


def make_formula_xlsx(items: list[dict]) -> bytes:
    """Create an auditable workbook whose detail and summary results are Excel formulas."""
    kind_labels = {"theory":"理论课程", "training":"集中实训", "internship":"岗位实习", "thesis":"毕业论文/设计", "defense":"答辩", "social":"社会实践", "manual":"手工核定"}
    headers = ["工号/学号","姓名","类型","课程/项目","班级","学生数","总学时","实验学时","类别系数","重复系数","课程系数","在线系数","实践系数","周数","指导教师数","校企系数","规模系数","理论工作量","实践工作量","合计工作量","手工核定","来源","实验计算学生数"]
    rows = []
    rows.append('<row r="1" ht="34" customHeight="1">' + xlsx_cell("A1", "教师教学工作量明细（公式版）", 1) + '</row>')
    rows.append('<row r="2" ht="30" customHeight="1">' + ''.join(xlsx_cell(f"{excel_col(i)}2", h, 2) for i,h in enumerate(headers,1)) + '</row>')
    for r, item in enumerate(items, 3):
        values = [item.get("employee_id",""),item.get("employee_name",""),kind_labels[item["kind"]],item["course"],item["class_name"],item["student_count"],item["total_hours"],item["experiment_hours"],item["category_coeff"],item["repeat_coeff"],item["course_coeff"],item["online_coeff"],item["practice_coeff"],item["weeks"],item["instructors"],item["enterprise_coeff"]]
        cells = [xlsx_cell(f"{excel_col(c)}{r}", v, 4 if c >= 6 else 0) for c,v in enumerate(values,1)]
        size_coeff = 1 + max(0, min(75, item["student_count"]) - 50) * .01 if item["kind"] == "theory" else 0
        cells.append(xlsx_cell(f"Q{r}", style=5, formula=f'IF(C{r}="理论课程",1+MAX(0,MIN(75,F{r})-50)*0.01,0)', cached=size_coeff))
        cells.append(xlsx_cell(f"R{r}", style=5, formula=f'IF(C{r}="理论课程",(G{r}-H{r})*J{r}*Q{r}*K{r}*I{r}*L{r}+IF(W{r}>0,W{r},MAX(50,MIN(75,F{r})))*H{r}*I{r}*J{r}*0.02*K{r},0)', cached=item["theory_workload"]))
        practice_formula = f'IF(C{r}="集中实训",M{r}*F{r}*N{r}*P{r}/MAX(1,O{r}),IF(C{r}="岗位实习",M{r}*F{r}/MAX(1,O{r}),IF(C{r}="毕业论文/设计",M{r}*F{r}*IF(AND(M{r}<1,N{r}>0),N{r},1)/MAX(1,O{r}),IF(C{r}="答辩",F{r}/MAX(1,O{r}),IF(C{r}="社会实践",F{r}*G{r}*IF(M{r}=0,0.02,M{r}),IF(C{r}="手工核定",U{r},0))))))'
        cells.append(xlsx_cell(f"S{r}", style=5, formula=practice_formula, cached=item["practice_workload"]))
        cells.append(xlsx_cell(f"T{r}", style=5, formula=f"R{r}+S{r}", cached=item["workload"]))
        cells.append(xlsx_cell(f"U{r}", item["manual_workload"], 4))
        cells.append(xlsx_cell(f"V{r}", item["source"]))
        cells.append(xlsx_cell(f"W{r}", item.get("experiment_students", 0), 4))
        rows.append(f'<row r="{r}" ht="23" customHeight="1">' + ''.join(cells) + '</row>')
    last = max(3, len(items) + 2)
    detail_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:W{last}"/><sheetViews><sheetView showGridLines="0" workbookViewId="0"><pane ySplit="2" topLeftCell="A3" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetFormatPr defaultRowHeight="20"/><cols>{''.join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i,w in enumerate([14,12,16,24,24,10,10,11,11,11,11,11,11,9,13,11,11,14,14,14,11,20,15],1))}</cols><sheetData>{''.join(rows)}</sheetData><autoFilter ref="A2:W{last}"/><mergeCells count="1"><mergeCell ref="A1:I1"/></mergeCells><pageMargins left="0.25" right="0.25" top="0.5" bottom="0.5" header="0.2" footer="0.2"/><pageSetup paperSize="9" orientation="landscape" fitToWidth="1" fitToHeight="0"/></worksheet>'''
    totals = summary(items)
    summary_rows = [
        '<row r="1" ht="34" customHeight="1">'+xlsx_cell("A1","教师教学工作量汇总（公式版）",1)+'</row>',
        '<row r="3">'+xlsx_cell("A3","指标",2)+xlsx_cell("B3","数值",2)+'</row>',
        '<row r="4" ht="24" customHeight="1">'+xlsx_cell("A4","理论课程学时",3)+xlsx_cell("B4",style=4,formula=f'SUMIF(明细!C3:C{last},"理论课程",明细!G3:G{last})',cached=totals["theory_hours"])+'</row>',
        '<row r="5" ht="24" customHeight="1">'+xlsx_cell("A5","理论课程工作量",3)+xlsx_cell("B5",style=4,formula=f'SUM(明细!R3:R{last})',cached=totals["theory_workload"])+'</row>',
        '<row r="6" ht="24" customHeight="1">'+xlsx_cell("A6","实践课程学时",3)+xlsx_cell("B6",style=4,formula=f'SUMIF(明细!C3:C{last},"<>理论课程",明细!G3:G{last})',cached=totals["practice_hours"])+'</row>',
        '<row r="7" ht="24" customHeight="1">'+xlsx_cell("A7","实践教学工作量",3)+xlsx_cell("B7",style=4,formula=f'SUM(明细!S3:S{last})',cached=totals["practice_workload"])+'</row>',
        '<row r="9" ht="27" customHeight="1">'+xlsx_cell("A9","合计总学时",2)+xlsx_cell("B9",style=6,formula='B4+B6',cached=totals["total_hours"])+'</row>',
        '<row r="10" ht="27" customHeight="1">'+xlsx_cell("A10","合计总工作量",2)+xlsx_cell("B10",style=6,formula='B5+B7',cached=totals["total_workload"])+'</row>',
    ]
    summary_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:B10"/><sheetViews><sheetView showGridLines="0" workbookViewId="0"/></sheetViews><sheetFormatPr defaultRowHeight="20"/><cols><col min="1" max="1" width="25" customWidth="1"/><col min="2" max="2" width="18" customWidth="1"/></cols><sheetData>{''.join(summary_rows)}</sheetData><mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells><pageMargins left="0.5" right="0.5" top="0.5" bottom="0.5" header="0.2" footer="0.2"/></worksheet>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'''
    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'''
    workbook = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="汇总" sheetId="1" r:id="rId1"/><sheet name="明细" sheetId="2" r:id="rId2"/></sheets><definedNames><definedName name="_xlnm.Print_Area" localSheetId="1">'明细'!$A$1:$T${last}</definedName></definedNames><calcPr calcId="191029" calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"/></workbook>'''
    workbook_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'''
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="1"><numFmt numFmtId="164" formatCode="0.0000"/></numFmts><fonts count="4"><font><sz val="10"/><color rgb="FF17201D"/><name val="Microsoft YaHei"/></font><font><b/><sz val="16"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font><font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font><font><b/><sz val="10"/><color rgb="FF246B55"/><name val="Microsoft YaHei"/></font></fonts><fills count="5"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF246B55"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FF17201D"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFE8F0EC"/></patternFill></fill></fills><borders count="2"><border/><border><left style="thin"><color rgb="FFDDE4E1"/></left><right style="thin"><color rgb="FFDDE4E1"/></right><top style="thin"><color rgb="FFDDE4E1"/></top><bottom style="thin"><color rgb="FFDDE4E1"/></bottom></border></borders><cellStyleXfs count="1"><xf fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="7"><xf fontId="0" fillId="0" borderId="0"/><xf fontId="1" fillId="2" borderId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf fontId="2" fillId="3" borderId="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf><xf fontId="0" fillId="0" borderId="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf><xf fontId="0" fillId="0" borderId="1" numFmtId="164" applyBorder="1" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf><xf fontId="3" fillId="4" borderId="1" numFmtId="164" applyFont="1" applyFill="1" applyBorder="1" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf><xf fontId="2" fillId="2" borderId="1" numFmtId="164" applyFont="1" applyFill="1" applyBorder="1" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles><dxfs count="0"/><tableStyles count="0" defaultTableStyle="TableStyleMedium2" defaultPivotStyle="PivotStyleLight16"/></styleSheet>'''
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types); zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook); zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels); zf.writestr("xl/styles.xml", styles)
        zf.writestr("xl/worksheets/sheet1.xml", summary_xml); zf.writestr("xl/worksheets/sheet2.xml", detail_xml)
    return out.getvalue()


def make_personal_hours_xlsx(employee_id: str, employee_name: str) -> bytes:
    """Generate the monthly personal-hours statement with live Excel totals."""
    # These are the monthly declarations from the supplied personal-hours sheet.
    # Other teachers receive an editable zeroed template until monthly detail is imported.
    seed = {
        ("0000000", "示例教师"): [("3月", 34, 32), ("4月", 36, 62), ("5月", 28, 16), ("6-7月", 42, 26)],
    }
    months = seed.get((employee_id, employee_name), [("3月", 0, 0), ("4月", 0, 0), ("5月", 0, 0), ("6-7月", 0, 0)])
    rows = [
        '<row r="1" ht="34" customHeight="1">' + xlsx_cell("A1", "2025-2026学年第二学期教师个人学时统计表", 1) + '</row>',
        '<row r="2" ht="24" customHeight="1">' + xlsx_cell("A2", "工号/学号", 3) + xlsx_cell("B2", employee_id) + xlsx_cell("C2", "教师姓名", 3) + xlsx_cell("D2", employee_name) + '</row>',
        '<row r="4" ht="30" customHeight="1">' + ''.join(xlsx_cell(f"{excel_col(i)}4", h, 2) for i,h in enumerate(["月份", "理论课时数", "实践类课时数", "月合计"], 1)) + '</row>',
    ]
    for idx, (month, theory, practice) in enumerate(months, 5):
        rows.append(f'<row r="{idx}" ht="24" customHeight="1">' + xlsx_cell(f"A{idx}", month, 3) + xlsx_cell(f"B{idx}", theory, 4) + xlsx_cell(f"C{idx}", practice, 4) + xlsx_cell(f"D{idx}", style=5, formula=f"B{idx}+C{idx}", cached=theory + practice) + '</row>')
    rows.extend([
        '<row r="9" ht="27" customHeight="1">' + xlsx_cell("A9", "学期合计", 2) + xlsx_cell("B9", style=6, formula="SUM(B5:B8)", cached=sum(x[1] for x in months)) + xlsx_cell("C9", style=6, formula="SUM(C5:C8)", cached=sum(x[2] for x in months)) + xlsx_cell("D9", style=6, formula="SUM(D5:D8)", cached=sum(x[1] + x[2] for x in months)) + '</row>',
        '<row r="11"><c r="A11" t="inlineStr"><is><t>说明：月度数据为可编辑输入，月合计和学期合计由公式自动计算。</t></is></c></row>',
    ])
    sheet = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:D11"/><sheetViews><sheetView showGridLines="0" workbookViewId="0"><pane ySplit="4" topLeftCell="A5" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetFormatPr defaultRowHeight="20"/><cols><col min="1" max="1" width="17" customWidth="1"/><col min="2" max="4" width="18" customWidth="1"/></cols><sheetData>{''.join(rows)}</sheetData><mergeCells count="1"><mergeCell ref="A1:D1"/></mergeCells><pageMargins left="0.5" right="0.5" top="0.5" bottom="0.5" header="0.2" footer="0.2"/></worksheet>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'''
    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'''
    workbook = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="个人学时统计" sheetId="1" r:id="rId1"/></sheets><calcPr calcId="191029" calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"/></workbook>'''
    workbook_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'''
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="1"><numFmt numFmtId="164" formatCode="0.0000"/></numFmts><fonts count="4"><font><sz val="10"/><color rgb="FF17201D"/><name val="Microsoft YaHei"/></font><font><b/><sz val="16"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font><font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font><font><b/><sz val="10"/><color rgb="FF246B55"/><name val="Microsoft YaHei"/></font></fonts><fills count="5"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF246B55"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FF17201D"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFE8F0EC"/></patternFill></fill></fills><borders count="2"><border/><border><left style="thin"><color rgb="FFDDE4E1"/></left><right style="thin"><color rgb="FFDDE4E1"/></right><top style="thin"><color rgb="FFDDE4E1"/></top><bottom style="thin"><color rgb="FFDDE4E1"/></bottom></border></borders><cellStyleXfs count="1"><xf fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="7"><xf fontId="0" fillId="0" borderId="0"/><xf fontId="1" fillId="2" borderId="0"/><xf fontId="2" fillId="3" borderId="1"/><xf fontId="0" fillId="0" borderId="1"/><xf fontId="0" fillId="0" borderId="1" numFmtId="164"/><xf fontId="3" fillId="4" borderId="1" numFmtId="164"/><xf fontId="2" fillId="2" borderId="1" numFmtId="164"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles><dxfs count="0"/><tableStyles count="0"/></styleSheet>'''
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in {"[Content_Types].xml":content_types,"_rels/.rels":root_rels,"xl/workbook.xml":workbook,"xl/_rels/workbook.xml.rels":workbook_rels,"xl/styles.xml":styles,"xl/worksheets/sheet1.xml":sheet}.items(): archive.writestr(name, data)
    return out.getvalue()


def semester_text(semester: str) -> str:
    match = re.fullmatch(r"(\d{4})-(\d{4})-([12])", semester or "")
    if not match:
        return semester or "2025-2026学年第二学期"
    return f"{match.group(1)}-{match.group(2)}学年第{'一' if match.group(3) == '1' else '二'}学期"


def personal_month_key(event_date: str, is_fall: bool) -> str:
    month = int(event_date[5:7])
    if is_fall:
        return "9月" if month in (8, 9) else "10月" if month == 10 else "11月" if month == 11 else "12-1月"
    return "3月" if month in (2, 3) else "4月" if month == 4 else "5月" if month == 5 else "6-7月"


def chinese_week(week: int) -> str:
    digits = "零一二三四五六七八九"
    if week < 10:
        text = digits[week]
    elif week == 10:
        text = "十"
    elif week < 20:
        text = "十" + digits[week - 10]
    else:
        text = "二十" + (digits[week - 20] if week > 20 else "")
    return f"第{text}周"


def validate_export_formulas(workbook) -> None:
    formula_count = 0
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                value = cell.value
                if isinstance(value, str) and value.startswith("="):
                    formula_count += 1
                    if any(token in value.upper() for token in ("#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A")):
                        raise AppError(f"导出公式校验失败：{sheet.title}!{cell.coordinate}", "EXPORT_FORMULA_INVALID", 500)
    if formula_count == 0:
        raise AppError("导出公式校验失败：工作簿中没有公式", "EXPORT_FORMULA_MISSING", 500)


def exact_personal_hours_xlsx(employee_id: str, employee_name: str, semester: str, monthly_data: list[dict] | None = None, event_data: list[dict] | None = None) -> bytes:
    from openpyxl import load_workbook
    template = TEMPLATES / "个人学时统计表模板.xlsx"
    workbook = load_workbook(template)
    if monthly_data is None or event_data is None:
        with db() as conn:
            rows = conn.execute("""SELECT * FROM monthly_hours WHERE semester=? AND employee_id=? ORDER BY id""", (semester, employee_id)).fetchall()
            event_rows = conn.execute("""SELECT * FROM schedule_events WHERE semester=? AND employee_id=? ORDER BY event_date,weekday,course,class_name""", (semester, employee_id)).fetchall()
    else:
        rows = [row for row in monthly_data if row.get("semester") == semester and row.get("employee_id") == employee_id]
        event_rows = sorted(
            [row for row in event_data if row.get("semester") == semester and row.get("employee_id") == employee_id],
            key=lambda row: (row.get("event_date", ""), int(row.get("weekday", 0)), row.get("course", ""), row.get("class_name", "")),
        )
    if not rows:
        raise AppError(f"{semester_text(semester)}没有从平台查询到该教师的个人学时数据", "SEMESTER_DATA_MISSING", 404)
    by_month = {r["month_key"]: dict(r) for r in rows}
    first = dict(rows[0]) if rows else {"college":"", "department":""}
    summary_sheet = workbook["表1-统计表"]
    summary_sheet["A1"] = f"{semester_text(semester)}教师分月课时统计表"
    summary_sheet["B5"] = first.get("college", "")
    summary_sheet["C5"] = first.get("department", "")
    summary_sheet["D5"] = employee_id
    summary_sheet["E5"] = employee_name
    is_fall = semester.endswith("-1")
    month_names = ["9月", "10月", "11月", "12-1月"] if is_fall else ["3月", "4月", "5月", "6-7月"]
    mapping = [(month_names[0], "F", "G"), (month_names[1], "H", "I"), (month_names[2], "J", "K"), (month_names[3], "L", "M")]
    for month, theory_col, practice_col in mapping:
        data = by_month.get(month, {})
        summary_sheet[f"{theory_col}5"] = float(data.get("theory_hours", 0))
        summary_sheet[f"{practice_col}5"] = float(data.get("practice_hours", 0))
    for cell_ref, month in zip(("F3", "H3", "J3", "L3"), month_names):
        summary_sheet[cell_ref] = month
    summary_sheet["N5"] = "=F5+H5+J5+L5"
    summary_sheet["O5"] = "=G5+I5+K5+M5"
    summary_sheet["P5"] = "=N5+O5"
    original_names = ("3月", "4月", "5月", "6-7月")
    layouts = (
        [(["D","E","F","G","H"],"I"),(["J","K","L","M","N"],"O"),(["P","Q","R","S","T"],"U"),(["V","W"],"X")],
        [(["D","E","F"],"G"),(["H","I","J","K"],"L"),(["M","N","O","P","Q"],"R"),(["S","T","U","V","W"],"X"),(["Y","Z"],"AA")],
        [(["D","E","F"],"G"),(["H","I","J","K","L"],"M"),(["N","O","P","Q","R"],"S"),(["T","U","V","W","X"],"Y")],
        [(["D","E","F","G","H"],"I"),(["J","K","L","M","N"],"O"),(["P","Q","R","S"],"T"),(["U","V","W","X","Y"],"Z"),(["AA","AB","AC","AD","AE"],"AF")],
    )
    row_total_columns = ("Y", "AB", "Z", "AG")
    aggregate_columns = ("Z", "AC", "AA", "AH")
    month_events = {month: [] for month in month_names}
    for row in event_rows:
        data = dict(row)
        key = personal_month_key(data["event_date"], is_fall)
        month_events[key].append(data)
    term_start = None
    max_academic_week = 0
    for data in event_rows:
        event_day = datetime.fromisoformat(data["event_date"]).date()
        inferred_start = event_day - timedelta(days=(int(data["academic_week"]) - 1) * 7 + int(data["weekday"]) - 1)
        term_start = inferred_start if term_start is None else min(term_start, inferred_start)
        max_academic_week = max(max_academic_week, int(data["academic_week"]))
    for index, (old_name, month, theory_col, practice_col) in enumerate(zip(original_names, month_names, ("F","H","J","L"), ("G","I","K","M"))):
        ws = workbook[old_name]
        if ws.title != month:
            ws.title = month
        # The supplied template contains fixed spring-holiday notes. They are
        # not Qingguo data and become actively misleading in a fall export.
        # Keep one provenance note and remove every hard-coded calendar note.
        for row_number in range(21, min(ws.max_row, 25) + 1):
            for column in range(1, ws.max_column + 1):
                cell_obj = ws.cell(row_number, column)
                if cell_obj.__class__.__name__ != "MergedCell":
                    cell_obj.value = None
        ws["A21"] = "备注：课程、周次和上课日期均依据青果教学安排生成。"
        for row_number in list(range(5, 11)) + list(range(14, 21)):
            for column in range(1, ws.max_column + 1):
                cell_obj = ws.cell(row_number, column)
                if cell_obj.coordinate not in ws.merged_cells:
                    cell_obj.value = None
        events = month_events[month]
        week_dates = {}
        if term_start is not None:
            for academic_week in range(1, max_academic_week + 1):
                week_start = term_start + timedelta(days=(academic_week - 1) * 7)
                expanded = []
                for day_offset in range(5):
                    iso_day = (week_start + timedelta(days=day_offset)).isoformat()
                    if personal_month_key(iso_day, is_fall) == month:
                        expanded.append(iso_day)
                if expanded:
                    week_dates[academic_week] = expanded
        weeks = sorted(week_dates)
        layout = layouts[index]
        week_slot = {week: min(position, len(layout)-1) for position, week in enumerate(weeks)}
        weekday_columns = {}
        for day_cols, weekly_col in layout:
            for header_row in (3, 12):
                ws[f"{day_cols[0]}{header_row}"] = None
                ws[f"{weekly_col}{header_row}"] = None
            for day_col in day_cols:
                ws[f"{day_col}4"] = None
                ws[f"{day_col}13"] = None
        for position, (day_cols, total_col) in enumerate(layout):
            assigned = [week for week, slot in week_slot.items() if slot == position]
            dates = sorted({d for week in assigned for d in week_dates[week]})
            if dates:
                date_label = f"{int(dates[0][5:7])}月{int(dates[0][8:10])}日--{int(dates[-1][5:7])}月{int(dates[-1][8:10])}日"
                week_label = "、".join(chinese_week(week) for week in assigned)
                ws[f"{day_cols[0]}3"] = date_label
                ws[f"{day_cols[0]}12"] = date_label
                ws[f"{total_col}3"] = week_label
                ws[f"{total_col}12"] = week_label
            present_weekdays = sorted({int(datetime.fromisoformat(d).isoweekday()) for d in dates})
            if len(day_cols) >= 5:
                present_weekdays = [1,2,3,4,5]
            for col, weekday in zip(day_cols, present_weekdays):
                weekday_columns[(position, weekday)] = col
                ws[f"{col}4"] = f"周{'一二三四五六日'[weekday-1]}"
                ws[f"{col}13"] = f"周{'一二三四五六日'[weekday-1]}"
        grouped = {"theory": {}, "practice": {}}
        for event in events:
            key = (event["course"], event["class_name"], event["student_count"])
            grouped[event["category"]].setdefault(key, []).append(event)
        if len(grouped["theory"]) > 6 or len(grouped["practice"]) > 7:
            raise AppError(
                f"{month}个人学时明细超过模板容量（理论最多6行、实践最多7行），已停止导出以避免漏数据",
                "PERSONAL_TEMPLATE_OVERFLOW", 422,
            )
        total_col = row_total_columns[index]
        for category, start_row, max_rows in (("theory",5,6),("practice",14,7)):
            for offset, (key, course_events) in enumerate(list(grouped[category].items())[:max_rows]):
                row_number = start_row + offset; course, class_name, students = key
                ws[f"A{row_number}"] = course; ws[f"B{row_number}"] = class_name; ws[f"C{row_number}"] = students
                by_slot = {}
                for event in course_events:
                    slot = week_slot.get(event["academic_week"])
                    col = weekday_columns.get((slot, event["weekday"]))
                    if col:
                        cell = ws[f"{col}{row_number}"]
                        # Several academic weeks can share the template's final
                        # displayed week group. Keep the daily label readable by
                        # listing each period once; weekly formulas below still
                        # count every underlying event.
                        existing = [part.strip() for part in str(cell.value or "").split(",") if part.strip()]
                        if event["periods"] not in existing:
                            existing.append(event["periods"])
                        cell.value = ",".join(existing)
                    by_slot.setdefault(slot, []).append(float(event["hours"]))
                weekly_cells = []
                for slot, (_, weekly_col) in enumerate(layout):
                    values = by_slot.get(slot, [])
                    if values:
                        ws[f"{weekly_col}{row_number}"] = "=" + "+".join(str(int(v) if v.is_integer() else v) for v in values)
                        weekly_cells.append(f"{weekly_col}{row_number}")
                ws[f"{total_col}{row_number}"] = "=" + "+".join(weekly_cells) if weekly_cells else 0
        aggregate_col = aggregate_columns[index]
        ws[f"{aggregate_col}5"] = f'="一共"&SUM({total_col}5:{total_col}10)&"节"'
        ws[f"{aggregate_col}13"] = f'="一共"&SUM({total_col}14:{total_col}20)&"节"'
        summary_sheet[f"{theory_col}5"] = f"=SUM('{month}'!{total_col}5:{total_col}10)"
        summary_sheet[f"{practice_col}5"] = f"=SUM('{month}'!{total_col}14:{total_col}20)"
    output = io.BytesIO()
    workbook.calculation.fullCalcOnLoad = True
    workbook.calculation.forceFullCalc = True
    validate_export_formulas(workbook)
    workbook.save(output)
    return output.getvalue()


def exact_workload_xlsx(items: list[dict], employee_id: str, employee_name: str, semester: str) -> bytes:
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter
    workbook = load_workbook(TEMPLATES / "决算工作量表模板.xlsx")
    theory = [item for item in items if item["kind"] == "theory"]
    practice = [item for item in items if item["kind"] != "theory"]
    term = semester_text(semester)

    theory_ws = workbook["校内教师理论工作量"]
    theory_end = 4 + max(5, len(theory))
    if theory_end > 9:
        extra = theory_end - 9
        shifted_merges = []
        for merged in list(theory_ws.merged_cells.ranges):
            if merged.min_row >= 10:
                shifted_merges.append((merged.min_col, merged.min_row, merged.max_col, merged.max_row))
                theory_ws.unmerge_cells(str(merged))
        theory_ws.insert_rows(10, extra)
        for min_col, min_row, max_col, max_row in shifted_merges:
            theory_ws.merge_cells(
                f"{get_column_letter(min_col)}{min_row + extra}:{get_column_letter(max_col)}{max_row + extra}"
            )
        for new_row in range(10, 10 + extra):
            theory_ws.row_dimensions[new_row].height = theory_ws.row_dimensions[9].height
            for col in range(1, theory_ws.max_column + 1):
                source, target = theory_ws.cell(9, col), theory_ws.cell(new_row, col)
                if source.has_style:
                    target._style = copy(source._style)
                target.number_format = source.number_format
                target.alignment = copy(source.alignment)
                target.border = copy(source.border)
                target.fill = copy(source.fill)
                target.font = copy(source.font)
        for col in ("A", "B", "C", "D", "E", "F", "G", "AN", "AO"):
            old_range = f"{col}5:{col}9"
            if old_range in theory_ws.merged_cells:
                theory_ws.unmerge_cells(old_range)
            theory_ws.merge_cells(f"{col}5:{col}{theory_end}")
    for row in range(5, theory_end + 1):
        for col in range(8, 42):
            target = theory_ws.cell(row, col)
            if target.__class__.__name__ != "MergedCell": target.value = None
    for index, item in enumerate(theory, 5):
        theory_ws.cell(index, 8).value = item["course"]
        theory_ws.cell(index, 9).value = item["course_code"]
        theory_ws.cell(index, 10).value = "/"
        theory_ws.cell(index, 11).value = "人工智能学院"
        theory_ws.cell(index, 12).value = item["class_name"]
        theory_ws.cell(index, 13).value = item["student_count"]
        theory_ws.cell(index, 14).value = 0
        theory_ws.cell(index, 16).value = "否"
        theory_ws.cell(index, 17).value = item["student_count"]
        theory_ws.cell(index, 19).value = item["total_hours"]
        theory_ws.cell(index, 20).value = item.get("experiment_students") or max(50, min(75, item["student_count"]))
        theory_ws.cell(index, 21).value = item["experiment_hours"]
        theory_ws.cell(index, 22).value = item["category_coeff"]
        theory_ws.cell(index, 23).value = "是" if item["repeat_coeff"] < 1 else "否"
        theory_ws.cell(index, 24).value = item["repeat_coeff"]
        theory_ws.cell(index, 25).value = .02
        theory_ws.cell(index, 26).value = item["course_coeff"]
        theory_ws.cell(index, 27).value = f"=T{index}*U{index}*V{index}*X{index}*Y{index}*Z{index}"
        theory_ws.cell(index, 28).value = 50
        theory_ws.cell(index, 29).value = f"=S{index}-U{index}"
        theory_ws.cell(index, 30).value = theory_ws.cell(index, 23).value
        theory_ws.cell(index, 31).value = item["repeat_coeff"]
        theory_ws.cell(index, 32).value = 1 + max(0, min(75, item["student_count"]) - 50) * .01
        theory_ws.cell(index, 33).value = item["course_coeff"]
        theory_ws.cell(index, 34).value = item["category_coeff"]
        theory_ws.cell(index, 35).value = item["online_coeff"]
        theory_ws.cell(index, 36).value = f"=AE{index}*AF{index}*AG{index}*AH{index}*AI{index}"
        theory_ws.cell(index, 37).value = f"=AC{index}*AJ{index}"
        theory_ws.cell(index, 38).value = f"=U{index}+AC{index}"
        theory_ws.cell(index, 39).value = f"=AA{index}+AK{index}"
    if theory:
        theory_ws["AN5"] = "=" + "+".join(f"AL{x}" for x in range(5, 5 + len(theory)))
        theory_ws["AO5"] = "=" + "+".join(f"AM{x}" for x in range(5, 5 + len(theory)))
    for col, value in [(1,"人工智能学院"),(2,"人工智能学院"),(3,"大数据教研室"),(4,employee_id),(5,employee_name),(6,"校内专任"),(7,"课堂教学")]: theory_ws.cell(5,col).value = value

    practice_ws = workbook["实践工作量"]
    practice_end = 5 + max(6, len(practice))
    if len(practice) > 6:
        extra = len(practice) - 6
        shifted_merges = []
        for merged in list(practice_ws.merged_cells.ranges):
            if merged.min_row >= 12:
                shifted_merges.append((merged.min_col, merged.min_row + extra, merged.max_col, merged.max_row + extra))
                practice_ws.unmerge_cells(str(merged))
        for merged in list(practice_ws.merged_cells.ranges):
            if merged.min_row == 6 and merged.max_row == 11:
                practice_ws.unmerge_cells(str(merged))
        practice_ws.insert_rows(12, extra)
        for min_col, min_row, max_col, max_row in shifted_merges:
            practice_ws.merge_cells(
                f"{get_column_letter(min_col)}{min_row}:{get_column_letter(max_col)}{max_row}"
            )
        for new_row in range(12, 12 + extra):
            practice_ws.row_dimensions[new_row].height = practice_ws.row_dimensions[11].height
            for col in range(1, practice_ws.max_column + 1):
                source, target = practice_ws.cell(11, col), practice_ws.cell(new_row, col)
                if source.has_style:
                    target._style = copy(source._style)
                target.number_format = source.number_format
                target.alignment = copy(source.alignment)
                target.border = copy(source.border)
                target.fill = copy(source.fill)
                target.font = copy(source.font)
        for col in ("A", "B", "C", "D", "E", "F", "AO", "AP"):
            practice_ws.merge_cells(f"{col}6:{col}{practice_end}")
    # The source template merges the student-type cell across three example rows.
    # Real platform data can contain several independent thesis/internship records,
    # so each data row needs its own editable cell while retaining the same style.
    if "Z8:Z10" in practice_ws.merged_cells:
        source_style = copy(practice_ws["Z8"]._style)
        practice_ws.unmerge_cells("Z8:Z10")
        for row in range(8, 11):
            practice_ws.cell(row, 26)._style = copy(source_style)
    for row in range(6, practice_end + 1):
        for col in range(7, 41):
            target = practice_ws.cell(row, col)
            if target.__class__.__name__ != "MergedCell": target.value = None
    for index, item in enumerate(practice, 6):
        practice_ws.cell(index, 7).value = {"training":"集中实训","internship":"岗位实习","thesis":"毕业论文","defense":"毕业论文","social":"社会实践","manual":"其他"}.get(item["kind"], item["kind"])
        practice_ws.cell(index, 8).value = item["course"]
        practice_ws.cell(index, 9).value = item["course_code"]
        practice_ws.cell(index, 10).value = item["class_name"]
        if item["kind"] == "training":
            for col,val in [(11,item["practice_coeff"]),(12,item["student_count"]),(13,"否"),(14,item["student_count"]),(15,item["enterprise_coeff"]),(16,item["weeks"]),(17,item["instructors"])]: practice_ws.cell(index,col).value=val
            practice_ws.cell(index,18).value=f"=K{index}*N{index}*P{index}*O{index}/Q{index}"; practice_ws.cell(index,19).value=f"=P{index}*10"
        elif item["kind"] == "internship":
            practice_ws.cell(index,20).value="本科" if item["practice_coeff"] >= 4 else "专科"
            practice_ws.cell(index,21).value=item["practice_coeff"]
            practice_ws.cell(index,22).value=item["student_count"]
            practice_ws.cell(index,23).value=f"=U{index}*V{index}/{max(1, item['instructors'])}"
            practice_ws.cell(index,24).value=item["weeks"]
            practice_ws.cell(index,25).value=f"=X{index}*V{index}*0.5"
        elif item["kind"] == "thesis":
            practice_ws.cell(index,26).value="本科" if item["practice_coeff"] >= 1 else "专科"; practice_ws.cell(index,27).value=item["practice_coeff"]; practice_ws.cell(index,28).value=item["student_count"]; practice_ws.cell(index,29).value=item["weeks"]; practice_ws.cell(index,30).value=item["instructors"]; practice_ws.cell(index,31).value=f"=AA{index}*AB{index}*IF(AA{index}<1,AC{index},1)/AD{index}"; practice_ws.cell(index,32).value=f"=AB{index}*IF(AA{index}<1,3,6)"
        elif item["kind"] == "defense":
            practice_ws.cell(index,33).value=item["course"]; practice_ws.cell(index,34).value=item["student_count"]; practice_ws.cell(index,35).value=item["instructors"]; practice_ws.cell(index,36).value=f"=AH{index}/AI{index}"
        elif item["kind"] == "social":
            practice_ws.cell(index,37).value=item["total_hours"]; practice_ws.cell(index,38).value=item["student_count"]; practice_ws.cell(index,39).value=item["practice_coeff"] or .02; practice_ws.cell(index,40).value=f"=AK{index}*AL{index}*AM{index}"
    for col, value in [(1,"人工智能学院"),(2,"人工智能学院"),(3,"大数据教研室"),(4,employee_id),(5,employee_name),(6,"校内专任")]: practice_ws.cell(6,col).value = value
    practice_ws["AO6"] = f"=SUM(S6:S{practice_end})+SUM(Y6:Y{practice_end})+SUM(AF6:AF{practice_end})+SUM(AK6:AK{practice_end})"
    practice_ws["AP6"] = f"=SUM(R6:R{practice_end})+SUM(W6:W{practice_end})+SUM(AE6:AE{practice_end})+SUM(AJ6:AJ{practice_end})+SUM(AN6:AN{practice_end})"

    total_ws = workbook["总工作量统计表"]
    total_ws["A1"] = f"山东工程职业技术大学{term}教师教学工作量决算统计表"
    for cell_ref, value in {"B3":"人工智能学院","C3":"人工智能学院","D3":"大数据教研室","E3":employee_id,"F3":employee_name,"G3":"校内专任","H3":"='校内教师理论工作量'!AN5","I3":"='校内教师理论工作量'!AO5","J3":"='实践工作量'!AO6","K3":"='实践工作量'!AP6","L3":"=H3+J3","M3":"=I3+K3"}.items(): total_ws[cell_ref] = value
    workbook.calculation.fullCalcOnLoad = True; workbook.calculation.forceFullCalc = True
    validate_export_formulas(workbook)
    output = io.BytesIO(); workbook.save(output); return output.getvalue()


def exact_budget_xlsx(items: list[dict], events: list[dict], employee_id: str, employee_name: str, semester: str) -> bytes:
    """Fill the supplied 2026-2027 budget workbook without changing its layout."""
    from openpyxl import load_workbook
    from openpyxl.utils import column_index_from_string

    template = TEMPLATES / "预算工作量表模板.xlsx"
    workbook = load_workbook(template)
    theory = [item for item in items if item["kind"] == "theory"]
    practice = [item for item in items if item["kind"] != "theory"]
    term_events = [event for event in events if event.get("semester") == semester]

    def shift_merges(ws, insertion_row: int, amount: int) -> None:
        shifted = []
        for merged in list(ws.merged_cells.ranges):
            if merged.min_row >= insertion_row:
                shifted.append((merged.min_col, merged.min_row + amount, merged.max_col, merged.max_row + amount))
                ws.unmerge_cells(str(merged))
        for min_col, min_row, max_col, max_row in shifted:
            ws.merge_cells(start_row=min_row, start_column=min_col, end_row=max_row, end_column=max_col)

    theory_ws = workbook["校内教师理论工作量"]
    theory_end = 3 + max(5, len(theory))
    if len(theory) > 5:
        extra = len(theory) - 5
        shift_merges(theory_ws, 9, extra)
        theory_ws.insert_rows(9, extra)
        for new_row in range(9, 9 + extra):
            theory_ws.row_dimensions[new_row].height = theory_ws.row_dimensions[8].height
            for col in range(1, 43):
                source, target = theory_ws.cell(8, col), theory_ws.cell(new_row, col)
                target._style = copy(source._style); target.number_format = source.number_format
                target.alignment = copy(source.alignment); target.border = copy(source.border)
                target.fill = copy(source.fill); target.font = copy(source.font)
    for col in ("A", "B", "C", "D", "E", "AN", "AO"):
        for merged in list(theory_ws.merged_cells.ranges):
            if merged.min_col == merged.max_col == column_index_from_string(col) and merged.min_row == 4:
                theory_ws.unmerge_cells(str(merged))
        theory_ws.merge_cells(f"{col}4:{col}{theory_end}")
    for row in range(4, theory_end + 1):
        for col in range(6, 42):
            cell = theory_ws.cell(row, col)
            if cell.__class__.__name__ != "MergedCell": cell.value = None
    event_weeks = {}
    for event in term_events:
        event_weeks.setdefault((event.get("course", ""), event.get("class_name", "")), set()).add(int(event.get("academic_week", 0)))
    for index, item in enumerate(theory, 4):
        weeks = len(event_weeks.get((item["course"], item["class_name"]), set())) or 16
        total_hours = float(item["total_hours"])
        weekly = int(round(total_hours / weeks)) if weeks else 0
        adjustment = total_hours - weekly * weeks
        student_type = "专升本" if "专升本" in item["class_name"] else "本科" if "本" in item["class_name"] else "专科"
        values = {6:"校内专任",7:"课堂教学",8:item["course"],9:item["course_code"],10:"/",11:"人工智能学院",12:item["class_name"],13:item["student_count"],14:student_type,15:"否",16:item["student_count"],17:weekly,18:weeks}
        for col, value in values.items(): theory_ws.cell(index, col).value = value
        if abs(adjustment) < 1e-9:
            theory_ws.cell(index,19).value=f"=Q{index}*R{index}"
        else:
            sign = "+" if adjustment > 0 else "-"
            amount = abs(adjustment)
            amount_text = str(int(amount)) if amount.is_integer() else str(amount)
            theory_ws.cell(index,19).value=f"=Q{index}*R{index}{sign}{amount_text}"
        theory_ws.cell(index,20).value=item.get("experiment_students") or max(50,min(75,item["student_count"]))
        theory_ws.cell(index,21).value=item["experiment_hours"]
        theory_ws.cell(index,22).value=item["category_coeff"]
        theory_ws.cell(index,23).value="是" if item["repeat_coeff"] < 1 else "否"
        theory_ws.cell(index,24).value=item["repeat_coeff"]; theory_ws.cell(index,25).value=.02; theory_ws.cell(index,26).value=item["course_coeff"]
        theory_ws.cell(index,27).value=f"=U{index}*T{index}*V{index}*X{index}*Y{index}*Z{index}"
        theory_ws.cell(index,28).value=max(50,min(75,item["student_count"])); theory_ws.cell(index,29).value=f"=S{index}-U{index}"
        theory_ws.cell(index,30).value=theory_ws.cell(index,23).value; theory_ws.cell(index,31).value=item["repeat_coeff"]
        theory_ws.cell(index,32).value=1+max(0,min(75,item["student_count"])-50)*.01; theory_ws.cell(index,33).value=item["course_coeff"]
        theory_ws.cell(index,34).value=item["category_coeff"]; theory_ws.cell(index,35).value=item["online_coeff"]
        theory_ws.cell(index,36).value=f"=AE{index}*AF{index}*AG{index}*AH{index}*AI{index}"
        theory_ws.cell(index,37).value=f"=AJ{index}*AC{index}"; theory_ws.cell(index,38).value=f"=AC{index}+U{index}"; theory_ws.cell(index,39).value=f"=AA{index}+AK{index}"
    for col, value in [(1,"人工智能学院"),(2,"人工智能学院"),(3,"大数据教研室"),(4,employee_id),(5,employee_name)]: theory_ws.cell(4,col).value=value
    theory_ws["AN4"] = f"=SUM(AL4:AL{theory_end})"; theory_ws["AO4"] = f"=SUM(AM4:AM{theory_end})"

    practice_ws = workbook["实践工作量"]
    practice_end = 12 + max(8, len(practice))
    if len(practice) > 8:
        extra = len(practice) - 8
        shift_merges(practice_ws, 22, extra)
        for merged in list(practice_ws.merged_cells.ranges):
            if merged.min_row == 13 and merged.max_row >= 20:
                practice_ws.unmerge_cells(str(merged))
        practice_ws.insert_rows(21, extra)
        for new_row in range(21, 21 + extra):
            practice_ws.row_dimensions[new_row].height = practice_ws.row_dimensions[20].height
            for col in range(1, 49):
                source, target = practice_ws.cell(20, col), practice_ws.cell(new_row, col)
                target._style = copy(source._style); target.alignment = copy(source.alignment)
                target.border = copy(source.border); target.fill = copy(source.fill); target.font = copy(source.font)
    for merged in list(practice_ws.merged_cells.ranges):
        if merged.min_row == 13 and merged.min_col in (1,2,3,4,5,6,47,48): practice_ws.unmerge_cells(str(merged))
    for col in ("A","B","C","D","E","F"): practice_ws.merge_cells(f"{col}13:{col}{practice_end}")
    practice_ws.merge_cells(f"AU13:AU{practice_end+1}"); practice_ws.merge_cells(f"AV13:AV{practice_end+1}")
    for row in range(13, practice_end + 1):
        for col in range(7, 47):
            cell = practice_ws.cell(row, col)
            if cell.__class__.__name__ != "MergedCell": cell.value = None
    for index, item in enumerate(practice, 13):
        practice_ws.cell(index,7).value={"training":"集中实训","internship":"岗位实习","thesis":"毕业论文","defense":"毕业论文","social":"社会实践","manual":"其他"}.get(item["kind"],item["kind"])
        practice_ws.cell(index,8).value=item["course"]; practice_ws.cell(index,9).value=item["course_code"]; practice_ws.cell(index,10).value=item["class_name"]
        if item["kind"] == "training":
            for col,val in [(11,item["practice_coeff"]),(12,item["student_count"]),(13,"否"),(14,item["student_count"]),(15,item["enterprise_coeff"]),(16,item["weeks"]),(17,item["instructors"])]: practice_ws.cell(index,col).value=val
            practice_ws.cell(index,18).value=f"=K{index}*N{index}*P{index}*O{index}/Q{index}"; practice_ws.cell(index,19).value=f"=P{index}*10"
        elif item["kind"] == "internship":
            practice_ws.cell(index,20).value="本科" if item["practice_coeff"] >= 4 else "专科"; practice_ws.cell(index,21).value=item["practice_coeff"]
            practice_ws.cell(index,22).value=item["student_count"]; practice_ws.cell(index,23).value=f"=U{index}*V{index}"; practice_ws.cell(index,24).value=item["weeks"]; practice_ws.cell(index,25).value=f"=X{index}*V{index}*0.5"
        elif item["kind"] == "thesis":
            practice_ws.cell(index,26).value="本科" if item["practice_coeff"] >= 1 else "专科"; practice_ws.cell(index,27).value=item["practice_coeff"]
            practice_ws.cell(index,28).value=item["student_count"]; practice_ws.cell(index,29).value=item["weeks"]; practice_ws.cell(index,30).value=item["instructors"]
            practice_ws.cell(index,31).value=f"=AA{index}*AB{index}*IF(AA{index}<1,AC{index},1)/AD{index}"; practice_ws.cell(index,32).value=f"=AB{index}*IF(AA{index}<1,3,6)"
        elif item["kind"] == "defense":
            practice_ws.cell(index,33).value=item["course"]; practice_ws.cell(index,34).value=item["student_count"]; practice_ws.cell(index,35).value=item["instructors"]
            practice_ws.cell(index,36).value=f"=20*AH{index}/60*3/AI{index}"
        elif item["kind"] == "social":
            practice_ws.cell(index,37).value=item["total_hours"]; practice_ws.cell(index,38).value=item["student_count"]; practice_ws.cell(index,39).value=item["practice_coeff"] or .02
            practice_ws.cell(index,40).value=f"=AK{index}*AL{index}*AM{index}"
    for col,value in [(1,"人工智能学院"),(2,"人工智能学院"),(3,"大数据教研室"),(4,employee_id),(5,employee_name),(6,"校内专任")]: practice_ws.cell(13,col).value=value
    practice_ws["AU13"] = f"=SUM(S13:S{practice_end})+SUM(Y13:Y{practice_end})+SUM(AF13:AF{practice_end})+SUM(AK13:AK{practice_end})+SUM(AT13:AT{practice_end})"
    practice_ws["AV13"] = f"=SUM(R13:R{practice_end})+SUM(W13:W{practice_end})+SUM(AE13:AE{practice_end})+SUM(AJ13:AJ{practice_end})+SUM(AN13:AN{practice_end})+SUM(AS13:AS{practice_end})"
    practice_ws["A1"] = f"{semester_text(semester)}实践教学工作量预算表"

    total_ws = workbook["总工作量统计表"]
    for cell_ref,value in {"B2":"人工智能学院","C2":"人工智能学院","D2":"大数据教研室","E2":employee_id,"F2":employee_name,"G2":"校内专任","H2":"='校内教师理论工作量'!AN4","I2":"='实践工作量'!AU13","J2":"='校内教师理论工作量'!AO4","K2":"='实践工作量'!AV13","L2":"=SUM(J2:K2)"}.items(): total_ws[cell_ref]=value
    workbook.calculation.fullCalcOnLoad = True; workbook.calculation.forceFullCalc = True
    validate_export_formulas(workbook)
    output=io.BytesIO(); workbook.save(output); return output.getvalue()


class Handler(SimpleHTTPRequestHandler):
    server_version = "WorkCount/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, fmt, *args):
        LOG.info(json.dumps({"request_id": getattr(self, "request_id", "-"), "client": self.client_address[0], "message": fmt % args}, ensure_ascii=False))

    def send_json(self, data, status=200, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", self.request_id)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def session_user(self):
        cookie = self.headers.get("Cookie", "")
        match = re.search(r"(?:^|;\s*)workcount_session=([^;]+)", cookie)
        if not match:
            return None
        token = match.group(1); session = SESSIONS.get(token)
        if not session:
            return None
        if session["expires"] < time.time():
            session["platform_password"] = ""
            session["platform_cookies"] = []
            SESSIONS.pop(token, None)
            return None
        session["expires"] = time.time() + SESSION_TTL
        self.session_token = token
        return session

    def body(self) -> bytes:
        size = int(self.headers.get("Content-Length", 0))
        if size > MAX_UPLOAD:
            raise AppError("文件超过 25MB 限制", "FILE_TOO_LARGE", 413)
        return self.rfile.read(size)

    def json_body(self) -> dict:
        try:
            return json.loads(self.body() or b"{}")
        except json.JSONDecodeError as exc:
            raise AppError("JSON 格式无效", "INVALID_JSON", 400) from exc

    def route(self):
        self.request_id = self.headers.get("X-Request-ID", str(uuid.uuid4()))
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/health": return self.send_json({"status": "ok"})
        if path == "/ready":
            with db() as conn: conn.execute("SELECT 1")
            missing = [name for name in REQUIRED_TEMPLATES if not (TEMPLATES / name).is_file()]
            return self.send_json({"status": "not_ready" if missing else "ok", "checks": {"database": "ok", "templates": "ok" if not missing else missing}}, 503 if missing else 200)
        if path == "/api/login" and self.command == "POST":
            payload = self.json_body()
            username = str(payload.get("username") or "").strip()
            password = str(payload.get("password") or "")
            if not username or not password:
                raise AppError("请输入平台账号和密码", "PLATFORM_CREDENTIALS_REQUIRED", 422)
            job_id = start_login_job({"username": username, "password": password, "employee_id": username, "employee_name": ""})
            return self.send_json({"job_id": job_id, "status": "running"}, 202)
        if path == "/api/login-status" and self.command == "GET":
            job_id = parse_qs(parsed.query).get("job", [""])[0]
            with LOGIN_LOCK:
                job = dict(LOGIN_JOBS.get(job_id) or {})
            if not job:
                raise AppError("登录任务不存在或已过期，请重新登录", "LOGIN_JOB_NOT_FOUND", 404)
            if job["status"] == "running":
                if time.time() - job["created"] > LOGIN_TIMEOUT + 10:
                    with LOGIN_LOCK:
                        LOGIN_JOBS.pop(job_id, None)
                    raise AppError("平台登录已超时，请检查 Chrome/Edge 和网络后重试", "PLATFORM_TIMEOUT", 504)
                return self.send_json({"status": "running", "elapsed": int(time.time() - job["created"]), "percent": int(job.get("percent", 2)), "message": job.get("message", "正在验证青果账号")})
            with LOGIN_LOCK:
                LOGIN_JOBS.pop(job_id, None)
            if job["status"] == "error":
                error = job["error"]
                raise AppError(error["message"], error["code"], error["http_status"])
            result = job["result"]
            token = job["session_token"]
            return self.send_json(
                {"status": "done", "user": {"employee_id": result["employee_id"], "employee_name": result["employee_name"]}, "sync": result},
                headers={"Set-Cookie": session_cookie(token, SESSION_TTL)},
            )
        if path == "/api/connector/status" and self.command == "GET":
            return self.send_json({"target": "https://qgjw.suet.edu.cn", "login": "/cas/logon.action", "encoding": "GBK", "auth": "浏览器临时会话", "mode": "实时读取已发布教学任务、教学安排和实践环节", "ready": True, "stores_password": False})
        user = self.session_user()
        if path == "/api/session" and self.command == "GET":
            if not user: raise AppError("尚未登录", "AUTH_REQUIRED", 401)
            return self.send_json({"user": {"employee_id": user["employee_id"], "employee_name": user["employee_name"]}, "syncing": bool(user.get("syncing")), "sync_percent": user.get("sync_percent", 100), "sync_message": user.get("sync_message", "")})
        if path == "/api/logout" and self.command == "POST":
            if getattr(self, "session_token", None):
                old = SESSIONS.pop(self.session_token, None)
                if old:
                    old["platform_password"] = ""
                    old["platform_cookies"] = []
            return self.send_json({}, headers={"Set-Cookie": session_cookie("", 0)})
        if path.startswith("/api/") and not user:
            raise AppError("请先使用青果账号登录", "AUTH_REQUIRED", 401)
        if path == "/api/sync-status" and self.command == "GET":
            return self.send_json({"syncing": bool(user.get("syncing")), "percent": user.get("sync_percent", 100), "message": user.get("sync_message", ""), "result": user.get("sync_result"), "error": user.get("sync_error")})
        if path == "/api/items" and self.command == "GET":
            query = parse_qs(parsed.query)
            semester = query.get("semester", [""])[0]
            items = [calculate(dict(item)) for item in user.get("records", []) if not semester or item.get("semester") == semester]
            return self.send_json({"items": items, "summary": summary(items), "syncing": bool(user.get("syncing")), "loaded": bool(semester and semester in user.get("loaded_semesters", set()))})
        if path == "/api/semesters" and self.command == "GET":
            records = user.get("records", []); monthly = user.get("monthly_data", [])
            available = user.get("available_semesters", [])
            work_counts = {semester: sum(1 for item in records if item.get("semester") == semester) for semester in available}
            hour_counts = {semester: sum(1 for row in monthly if row.get("semester") == semester) for semester in available}
            terms = [{"value": semester, "label": semester_text(semester), "workload_count": work_counts.get(semester, 0), "monthly_count": hour_counts.get(semester, 0), "has_data": semester in user.get("loaded_semesters", set()), "loaded": semester in user.get("loaded_semesters", set())} for semester in available]
            return self.send_json({"semesters": terms, "syncing": bool(user.get("syncing"))})
        if path == "/api/platform/load-semester" and self.command == "POST":
            semester = str(self.json_body().get("semester") or "").strip()
            if not semester: raise AppError("请选择需要读取的学期", "SEMESTER_REQUIRED", 422)
            if semester in user.get("loaded_semesters", set()):
                return self.send_json({"status": "ready", "semester": semester})
            start_semester_sync(user, semester)
            return self.send_json({"status": "running", "semester": semester}, 202)
        if path == "/api/platform/sync" and self.command == "POST":
            payload = self.json_body()
            if str(payload.get("username") or "").strip() != user["employee_id"]:
                raise AppError("只能使用当前登录教师的青果账号重新同步", "ACCOUNT_MISMATCH", 403)
            semester = str(payload.get("semester") or "").strip()
            if not semester: raise AppError("请选择需要更新的学期", "SEMESTER_REQUIRED", 422)
            start_semester_sync(user, semester)
            return self.send_json({"sync": {"semester": semester, "status": "running"}}, 202)
        if path == "/api/items" and self.command == "POST":
            raise AppError("实时平台模式不允许新增本地记录", "REALTIME_READ_ONLY", 405)
        match = re.fullmatch(r"/api/items/(\d+)", path)
        if match and self.command in {"PUT", "DELETE"}:
            raise AppError("实时平台模式不允许修改或删除平台记录", "REALTIME_READ_ONLY", 405)
        if path == "/api/import-xlsx" and self.command == "POST":
            self.session_user()
            raise AppError("系统仅使用教务平台实时数据，不接受 Excel 导入", "IMPORT_DISABLED", 405)
        if path == "/api/export.csv" and self.command == "GET":
            output = io.StringIO(); writer = csv.writer(output)
            writer.writerow(["类型", "课程/项目", "班级", "课程代码", "学生数", "学时", "工作量", "计算说明", "来源"])
            for x in [calculate(dict(item)) for item in user.get("records", [])]: writer.writerow([x["kind"], x["course"], x["class_name"], x["course_code"], x["student_count"], x["display_hours"], x["workload"], x["calculation"], x["source"]])
            raw = ("\ufeff" + output.getvalue()).encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "text/csv; charset=utf-8"); self.send_header("Content-Disposition", "attachment; filename=workload.csv"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        if path == "/api/export.xlsx" and self.command == "GET":
            if user.get("syncing"): raise AppError("青果数据仍在更新，请等待进度完成后再导出", "SYNC_IN_PROGRESS", 409)
            query = parse_qs(parsed.query)
            employee_id = user["employee_id"]; employee_name = user["employee_name"]; semester = query.get("semester", ["2025-2026-2"])[0]
            if semester not in user.get("loaded_semesters", set()): raise AppError("请先点击“查询并计算”读取该学期青果数据", "SEMESTER_NOT_LOADED", 409)
            term_items = [calculate(dict(item)) for item in user.get("records", []) if item.get("semester") == semester]
            if not term_items:
                raise AppError(f"{semester_text(semester)}没有从平台查询到该教师的工作量数据", "SEMESTER_DATA_MISSING", 404)
            raw = exact_workload_xlsx(term_items, employee_id, employee_name, semester)
            self.send_response(200); self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"); self.send_header("Content-Disposition", "attachment; filename=workload-formulas.xlsx"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        if path == "/api/export-budget.xlsx" and self.command == "GET":
            if user.get("syncing"): raise AppError("青果数据仍在更新，请等待进度完成后再生成预算表", "SYNC_IN_PROGRESS", 409)
            query = parse_qs(parsed.query); semester = query.get("semester", [""])[0]
            if not semester: raise AppError("请选择需要生成预算的学期", "SEMESTER_REQUIRED", 422)
            if semester not in user.get("loaded_semesters", set()): raise AppError("请先点击“查询并计算”读取该学期青果数据", "SEMESTER_NOT_LOADED", 409)
            term_items = [dict(item) for item in user.get("records", []) if item.get("semester") == semester]
            if not term_items: raise AppError(f"{semester_text(semester)}没有从青果查询到预算数据", "SEMESTER_DATA_MISSING", 404)
            raw = exact_budget_xlsx(term_items, user.get("event_data", []), user["employee_id"], user["employee_name"], semester)
            self.send_response(200); self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"); self.send_header("Content-Disposition", "attachment; filename=workload-budget.xlsx"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        if path == "/api/export-personal-hours.xlsx" and self.command == "GET":
            if user.get("syncing"): raise AppError("青果数据仍在更新，请等待进度完成后再生成个人学时", "SYNC_IN_PROGRESS", 409)
            query = parse_qs(parsed.query)
            employee_id = user["employee_id"]
            employee_name = user["employee_name"]
            semester = query.get("semester", ["2025-2026-2"])[0]
            if semester not in user.get("loaded_semesters", set()): raise AppError("请先点击“查询并计算”读取该学期青果数据", "SEMESTER_NOT_LOADED", 409)
            raw = exact_personal_hours_xlsx(employee_id, employee_name, semester, user.get("monthly_data", []), user.get("event_data", []))
            self.send_response(200); self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"); self.send_header("Content-Disposition", "attachment; filename=personal-hours-formulas.xlsx"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        if path.startswith("/api/"):
            raise AppError("接口不存在", "NOT_FOUND", 404)
        if self.command != "GET": raise AppError("方法不允许", "METHOD_NOT_ALLOWED", 405)
        self.path = "/index.html" if path == "/" else parsed.path
        return super().do_GET()

    def _dispatch(self):
        try:
            return self.route()
        except AppError as exc:
            self.send_json({"error": {"code": exc.code, "message": exc.message, "request_id": self.request_id}}, exc.status)
        except Exception:
            LOG.exception("unhandled request error")
            self.send_json({"error": {"code": "INTERNAL_ERROR", "message": "服务器内部错误", "request_id": self.request_id}}, 500)

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_DELETE = _dispatch


def main() -> None:
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    LOG.info(json.dumps({"event": "server_started", "url": f"http://{HOST}:{PORT}", "database": str(DB_PATH)}, ensure_ascii=False))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
