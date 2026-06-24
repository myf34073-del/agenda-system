"""
北京市朝阳区体育局 议题一体化系统
Flask REST API 后端服务
端口: 5001
"""

import os
import json
import sqlite3
import hashlib
from datetime import datetime, date
from functools import wraps
from flask import Flask, request, jsonify, g, send_from_directory
from flask_cors import CORS

# ──────────────────────────────────────────────
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
CORS(app, supports_credentials=True)

BASE_DIR   = os.path.dirname(__file__)
DB_PATH    = os.path.join(BASE_DIR, "agendas.db")
STATIC_DIR = os.environ.get("STATIC_DIR", "..")   # Railway 上设为 "."，本地为 ".."
IS_CLOUD   = os.environ.get("RAILWAY_ENV") or os.environ.get("RENDER")

# ──────────────────────────────────────────────
# 数据库连接
# ──────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()

def query(sql, params=(), one=False):
    cur = get_db().execute(sql, params)
    rv  = cur.fetchone() if one else cur.fetchall()
    return (dict(rv) if rv else None) if one else [dict(r) for r in rv]

def execute(sql, params=()):
    db  = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur.lastrowid

def hash_pw(p): return hashlib.sha256(p.encode()).hexdigest()
def ok(data=None, **kw): return jsonify({"code": 0, "data": data, **kw})
def err(msg, code=400):  return jsonify({"code": -1, "msg": msg}), code

# ──────────────────────────────────────────────
# Session 管理（SQLite 持久化，重启不丢失）
# ──────────────────────────────────────────────
SESSION_HOURS = 2   # token 有效期（小时）

def _clean_expired_sessions():
    """清理过期 session"""
    execute("DELETE FROM sessions WHERE expires_at < datetime('now','localtime')")

def _create_session(user_id: int, ip_addr: str = None) -> str:
    """创建 session 并返回 token"""
    import secrets
    _clean_expired_sessions()
    token = secrets.token_hex(24)
    execute(
        "INSERT OR REPLACE INTO sessions (token, user_id, expires_at, ip_addr) "
        "VALUES (?, ?, datetime('now','localtime','+{} hours'), ?)".format(SESSION_HOURS),
        (token, user_id, ip_addr)
    )
    return token

def _refresh_session(token: str):
    """刷新 token 过期时间"""
    execute(
        "UPDATE sessions SET expires_at=datetime('now','localtime','+{} hours') "
        "WHERE token=? AND expires_at > datetime('now','localtime')".format(SESSION_HOURS),
        (token,)
    )

def _validate_session(token: str):
    """验证 token 有效性，返回 user_id 或 None"""
    row = query(
        "SELECT user_id FROM sessions WHERE token=? AND expires_at > datetime('now','localtime')",
        (token,), one=True
    )
    return row["user_id"] if row else None

def require_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-Token") or request.args.get("token")
        if not token:
            return err("未登录或登录已过期", 401)
        user_id = _validate_session(token)
        if not user_id:
            return err("未登录或登录已过期", 401)
        g.current_user = query("SELECT * FROM users WHERE id=?", (user_id,), one=True)
        if not g.current_user or not g.current_user["is_active"]:
            return err("用户不存在或已禁用", 401)
        g.current_token = token
        _refresh_session(token)  # 活跃则自动续期
        return f(*args, **kwargs)
    return wrapper

def auto_serial(dept):
    """生成议题编号: YYMM-DEPT-SEQ"""
    now  = datetime.now()
    ym   = now.strftime("%y%m")
    dept_code = {"场馆中心": "CGZ", "局办公室": "JB", "组织人事科": "ZR",
                 "信息中心": "XX"}.get(dept, dept[:2])
    # 用 LIKE 匹配编号前缀，避免 strftime 对 TEXT 时间戳返回 None
    count = query("SELECT COUNT(*) cnt FROM agendas WHERE serial_no LIKE ?", (f"{ym}-%",))
    seq   = (count[0]["cnt"] if count else 0) + 1
    return f"{ym}-{dept_code}-{seq:03d}"

# ══════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════
@app.post("/api/auth/login")
def login():
    d    = request.json or {}
    user = query("SELECT * FROM users WHERE username=? AND is_active=1",
                 (d.get("username",""),), one=True)
    if not user or user["password"] != hash_pw(d.get("password","")):
        execute("INSERT INTO login_logs (username,ip_addr,action,result) VALUES (?,?,?,?)",
                (d.get("username"), request.remote_addr, "login", "fail"))
        return err("用户名或密码错误")
    token = _create_session(user["id"], request.remote_addr)
    execute("INSERT INTO login_logs (user_id,username,ip_addr,action,result) VALUES (?,?,?,?,?)",
            (user["id"], user["username"], request.remote_addr, "login", "success"))
    return ok({
        "token":     token,
        "id":        user["id"],
        "real_name": user["real_name"],
        "role":      user["role"],
        "dept":      user["dept"],
    })

@app.post("/api/auth/register")
def register():
    """用户自助注册，默认角色为 staff，注册后自动登录"""
    d = request.json or {}
    username  = (d.get("username") or "").strip()
    password  = (d.get("password") or "").strip()
    real_name = (d.get("real_name") or "").strip()
    dept      = (d.get("dept") or "").strip()

    # 校验必填字段
    if not username or not password or not real_name or not dept:
        return err("用户名、密码、姓名、部门均为必填项")

    if len(username) < 3:
        return err("用户名至少 3 个字符")
    if len(password) < 6:
        return err("密码至少 6 个字符")

    # 检查用户名唯一性
    existing = query("SELECT id FROM users WHERE username=?", (username,), one=True)
    if existing:
        return err("该用户名已被注册")

    # 创建用户（默认角色：staff）
    try:
        uid = execute(
            "INSERT INTO users (username, password, real_name, role, dept) VALUES (?,?,?,?,?)",
            (username, hash_pw(password), real_name, "staff", dept)
        )
    except Exception as e:
        return err(f"注册失败: {str(e)}")

    # 注册后自动登录
    token = _create_session(uid, request.remote_addr)
    user  = query("SELECT id, username, real_name, role, dept FROM users WHERE id=?", (uid,), one=True)

    execute("INSERT INTO login_logs (user_id, username, ip_addr, action, result) VALUES (?,?,?,?,?)",
            (uid, username, request.remote_addr, "register", "success"))

    return ok({
        "token":     token,
        "id":        user["id"],
        "real_name": user["real_name"],
        "role":      user["role"],
        "dept":      user["dept"],
    })

@app.post("/api/auth/logout")
@require_login
def logout():
    token = request.headers.get("X-Token")
    execute("DELETE FROM sessions WHERE token=?", (token,))
    return ok("已退出")

@app.get("/api/auth/me")
@require_login
def me():
    u = dict(g.current_user)
    u.pop("password", None)
    return ok(u)

@app.get("/api/auth/check")
def check_token():
    """前端检查 token 是否有效（不触发 require_login 的错误响应）"""
    token = request.headers.get("X-Token") or request.args.get("token")
    if not token:
        return ok({"valid": False, "msg": "未登录"})
    user_id = _validate_session(token)
    if not user_id:
        return ok({"valid": False, "msg": "登录已过期"})
    user = query("SELECT id,username,real_name,role,dept FROM users WHERE id=? AND is_active=1",
                 (user_id,), one=True)
    if not user:
        return ok({"valid": False, "msg": "用户已禁用"})
    _refresh_session(token)
    return ok({"valid": True, "user": dict(user)})

# ══════════════════════════════════════════════
# 健康检查（Railway 部署用）
# ══════════════════════════════════════════════
@app.get("/health")
def health():
    try:
        db = sqlite3.connect(DB_PATH)
        db.execute("SELECT 1")
        db.close()
        db_ok = True
    except Exception:
        db_ok = False
    return jsonify({"status": "ok", "db": db_ok})

# ══════════════════════════════════════════════
# 用户管理（admin）
# ══════════════════════════════════════════════
@app.get("/api/users")
@require_login
def list_users():
    if g.current_user["role"] != "admin":
        return err("无权限", 403)
    return ok(query("SELECT id,username,real_name,role,dept,is_active,created_at FROM users"))

@app.post("/api/users")
@require_login
def create_user():
    if g.current_user["role"] != "admin":
        return err("无权限", 403)
    d = request.json or {}
    uid = execute(
        "INSERT INTO users (username,password,real_name,role,dept) VALUES (?,?,?,?,?)",
        (d["username"], hash_pw(d.get("password","abc123")), d["real_name"], d["role"], d["dept"])
    )
    return ok({"id": uid})

@app.put("/api/users/<int:uid>")
@require_login
def update_user(uid):
    if g.current_user["role"] != "admin":
        return err("无权限", 403)
    d = request.json or {}
    fields, vals = [], []
    for k in ("real_name", "role", "dept", "is_active"):
        if k in d:
            fields.append(f"{k}=?")
            vals.append(d[k])
    if "password" in d:
        fields.append("password=?")
        vals.append(hash_pw(d["password"]))
    if fields:
        vals.append(uid)
        execute(f"UPDATE users SET {','.join(fields)},updated_at=CURRENT_TIMESTAMP WHERE id=?", vals)
    return ok()

# ══════════════════════════════════════════════
# 模板
# ══════════════════════════════════════════════
@app.get("/api/templates")
@require_login
def list_templates():
    dept = request.args.get("dept", g.current_user["dept"])
    rows = query("SELECT * FROM templates WHERE is_active=1 AND (dept=? OR dept='全局') ORDER BY id",
                 (dept,))
    for r in rows:
        r["fields"] = json.loads(r["fields_json"])
    return ok(rows)

@app.get("/api/templates/<int:tid>")
@require_login
def get_template(tid):
    t = query("SELECT * FROM templates WHERE id=?", (tid,), one=True)
    if not t: return err("模板不存在", 404)
    t["fields"] = json.loads(t["fields_json"])
    return ok(t)

# ══════════════════════════════════════════════
# 议题 CRUD
# ══════════════════════════════════════════════
@app.get("/api/agendas")
@require_login
def list_agendas():
    u      = g.current_user
    role   = u["role"]
    dept   = u["dept"]
    status = request.args.get("status")
    kw     = request.args.get("q", "").strip()
    page   = int(request.args.get("page", 1))
    size   = int(request.args.get("size", 20))

    wheres, params = [], []
    if role in ("staff", "venue_staff"):
        wheres.append("a.created_by=?"); params.append(u["id"])
    elif role in ("dept_head", "venue_office", "venue_head"):
        wheres.append("a.dept=?"); params.append(dept)
    elif role in ("office_staff", "office_leader"):
        pass   # 全量
    elif role == "leader":
        wheres.append("a.status IN ('meeting','minutes_draft','minutes_signed','archived','scheduled')")
    # admin: 全量

    if status:
        wheres.append("a.status=?"); params.append(status)
    if kw:
        wheres.append("(a.title LIKE ? OR a.summary LIKE ?)"); params += [f"%{kw}%", f"%{kw}%"]

    where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
    total = query(f"SELECT COUNT(*) cnt FROM agendas a {where_sql}", params)[0]["cnt"]
    offset = (page - 1) * size
    rows   = query(
        f"""SELECT a.*, u.real_name creator_name FROM agendas a
            JOIN users u ON u.id=a.created_by
            {where_sql} ORDER BY a.created_at DESC LIMIT ? OFFSET ?""",
        params + [size, offset]
    )
    return ok({"list": rows, "total": total, "page": page, "size": size})

@app.post("/api/agendas")
@require_login
def create_agenda():
    u = g.current_user
    d = request.json or {}
    serial = auto_serial(u["dept"])
    aid = execute(
        """INSERT INTO agendas
           (serial_no, title, dept, category, template_id, content, summary, amount,
            status, source, created_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (serial, d["title"], u["dept"],
         d.get("category", "行政事项"),
         d.get("template_id"),
         d.get("content", ""),
         d.get("summary", ""),
         d.get("amount", 0),
         "draft",
         d.get("source", "write"),
         u["id"])
    )
    return ok({"id": aid, "serial_no": serial})

@app.get("/api/agendas/<int:aid>")
@require_login
def get_agenda(aid):
    a = query("""
        SELECT a.*, u.real_name creator_name, u.dept creator_dept
        FROM agendas a JOIN users u ON u.id=a.created_by
        WHERE a.id=?""", (aid,), one=True)
    if not a: return err("议题不存在", 404)
    a["approvals"] = query(
        "SELECT ap.*, u.real_name op_name FROM approvals ap JOIN users u ON u.id=ap.operator_id WHERE ap.agenda_id=? ORDER BY ap.created_at",
        (aid,)
    )
    return ok(a)

@app.put("/api/agendas/<int:aid>")
@require_login
def update_agenda(aid):
    u = g.current_user
    a = query("SELECT * FROM agendas WHERE id=?", (aid,), one=True)
    if not a: return err("议题不存在", 404)
    if a["status"] not in ("draft", "rejected") and u["role"] not in ("admin", "office_leader"):
        return err("当前状态不允许编辑")
    d = request.json or {}
    fields, vals = [], []
    for k in ("title", "category", "content", "summary", "amount", "remarks"):
        if k in d:
            fields.append(f"{k}=?"); vals.append(d[k])
    if fields:
        vals.append(aid)
        execute(f"UPDATE agendas SET {','.join(fields)},updated_at=CURRENT_TIMESTAMP WHERE id=?", vals)
    return ok()

@app.delete("/api/agendas/<int:aid>")
@require_login
def delete_agenda(aid):
    u = g.current_user
    a = query("SELECT * FROM agendas WHERE id=?", (aid,), one=True)
    if not a: return err("议题不存在", 404)
    if a["created_by"] != u["id"] and u["role"] != "admin":
        return err("无权限", 403)
    if a["status"] not in ("draft", "rejected"):
        return err("非草稿/驳回状态不可删除")
    execute("UPDATE agendas SET status='abandoned' WHERE id=?", (aid,))
    return ok()

# ──────────────────────────────────────────────
# 议题状态流转（提交/审批/通过/驳回）
# ──────────────────────────────────────────────
STATUS_FLOW = {
    "draft":         ("staff","venue_staff","office_staff","dept_head","admin"),
    "submitted":     ("dept_head","venue_head","admin"),
    "dept_review":   ("office_leader","admin"),
    "office_review": ("office_staff","office_leader","admin"),
    "converted":     ("office_staff","office_leader","admin"),
    "scheduled":     ("leader","office_leader","admin"),
    "meeting":       ("leader","office_leader","admin"),
    "minutes_draft": ("leader","admin"),
    "minutes_signed":("office_leader","admin"),
}

@app.post("/api/agendas/<int:aid>/action")
@require_login
def agenda_action(aid):
    u      = g.current_user
    d      = request.json or {}
    action = d.get("action")    # submit / approve / reject / comment / schedule / sign
    comment = d.get("comment", "")
    a = query("SELECT * FROM agendas WHERE id=?", (aid,), one=True)
    if not a: return err("议题不存在", 404)

    transitions = {
        # action: (required_status, new_status, step_name)
        "submit":   ("draft",        "submitted",    "submit"),
        "submit2":  ("rejected",     "submitted",    "resubmit"),
        "dept_pass":("submitted",    "dept_review",  "dept_review"),
        "dept_reject":("submitted",  "rejected",     "dept_review"),
        "office_pass":("dept_review","office_review","office_review"),
        "office_reject":("dept_review","rejected",   "office_review"),
        "schedule": ("office_review","scheduled",    "schedule"),
        "meeting_pass":("scheduled", "meeting",      "leader_review"),
        "minutes_draft":("meeting",  "minutes_draft","minutes"),
        "sign":     ("minutes_draft","minutes_signed","sign"),
        "archive":  ("minutes_signed","archived",    "archive"),
    }
    if action not in transitions:
        return err(f"不支持的操作: {action}")

    req_status, new_status, step = transitions[action]
    if a["status"] != req_status:
        return err(f"当前状态 '{a['status']}' 不允许此操作（需要: {req_status}）")

    execute("UPDATE agendas SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_status, aid))
    execute("INSERT INTO approvals (agenda_id, step, action, operator_id, comment) VALUES (?,?,?,?,?)",
            (aid, step, action, u["id"], comment))

    # 通知下一处理人
    notif_targets = {
        "submit":      ("dept_head", f"新议题待审核: {a['title']}"),
        "dept_pass":   ("office_leader", f"议题待初审: {a['title']}"),
        "office_pass": ("office_staff", f"议题待排期上会: {a['title']}"),
        "schedule":    ("leader", f"议题待审议: {a['title']}"),
        "meeting_pass":("office_staff", f"会议纪要待撰写: {a['title']}"),
        "minutes_draft":("leader", f"会议纪要待签发: {a['title']}"),
    }
    if action in notif_targets:
        role_target, notif_title = notif_targets[action]
        targets = query("SELECT id FROM users WHERE role=? AND dept=? AND is_active=1",
                        (role_target, a["dept"]))
        if not targets:  # fallback：角色全局匹配
            targets = query("SELECT id FROM users WHERE role=? AND is_active=1", (role_target,))
        for t in targets:
            execute("INSERT INTO notifications (user_id,type,title,agenda_id) VALUES (?,?,?,?)",
                    (t["id"], "approval", notif_title, aid))
    return ok({"new_status": new_status})

# ══════════════════════════════════════════════
# 会议管理
# ══════════════════════════════════════════════
@app.get("/api/meetings")
@require_login
def list_meetings():
    rows = query("SELECT * FROM meetings ORDER BY meeting_date DESC")
    return ok(rows)

@app.post("/api/meetings")
@require_login
def create_meeting():
    if g.current_user["role"] not in ("admin", "office_staff", "office_leader"):
        return err("无权限", 403)
    d = request.json or {}
    mid = execute(
        "INSERT INTO meetings (batch_no, title, meeting_date, location, status, created_by) VALUES (?,?,?,?,?,?)",
        (d["batch_no"], d["title"], d["meeting_date"], d.get("location",""), "planning", g.current_user["id"])
    )
    return ok({"id": mid})

@app.get("/api/meetings/<int:mid>")
@require_login
def get_meeting(mid):
    m = query("SELECT * FROM meetings WHERE id=?", (mid,), one=True)
    if not m: return err("会议不存在", 404)
    m["agendas"] = query(
        """SELECT ma.seq_no, ma.status ma_status, ma.leader_comment,
                  a.id, a.serial_no, a.title, a.dept, a.category, a.content, a.summary, a.amount
           FROM meeting_agendas ma JOIN agendas a ON a.id=ma.agenda_id
           WHERE ma.meeting_id=? ORDER BY ma.seq_no""", (mid,)
    )
    return ok(m)

@app.post("/api/meetings/<int:mid>/agendas")
@require_login
def add_agenda_to_meeting(mid):
    if g.current_user["role"] not in ("admin","office_staff","office_leader"):
        return err("无权限", 403)
    d = request.json or {}
    aid = d.get("agenda_id")
    seq = d.get("seq_no", 0)
    try:
        execute("INSERT INTO meeting_agendas (meeting_id, agenda_id, seq_no) VALUES (?,?,?)",
                (mid, aid, seq))
        execute("UPDATE agendas SET status='scheduled',updated_at=CURRENT_TIMESTAMP WHERE id=?", (aid,))
    except Exception as e:
        return err(str(e))
    return ok()

@app.post("/api/meetings/<int:mid>/action")
@require_login
def meeting_action(mid):
    d = request.json or {}
    action = d.get("action")
    if action == "start":
        execute("UPDATE meetings SET status='ongoing',updated_at=CURRENT_TIMESTAMP WHERE id=?", (mid,))
    elif action == "finish":
        execute("UPDATE meetings SET status='finished',updated_at=CURRENT_TIMESTAMP WHERE id=?", (mid,))
    return ok()

# ══════════════════════════════════════════════
# 会议纪要
# ══════════════════════════════════════════════
@app.get("/api/minutes")
@require_login
def list_minutes():
    mid = request.args.get("meeting_id")
    if mid:
        rows = query("SELECT m.*, u.real_name creator FROM minutes m LEFT JOIN users u ON u.id=m.created_by WHERE m.meeting_id=?", (mid,))
    else:
        rows = query("SELECT m.*, u.real_name creator FROM minutes m LEFT JOIN users u ON u.id=m.created_by ORDER BY m.created_at DESC LIMIT 50")
    return ok(rows)

@app.post("/api/minutes")
@require_login
def create_minutes():
    u = g.current_user
    d = request.json or {}
    nid = execute(
        "INSERT INTO minutes (meeting_id, agenda_id, content, decision, status, created_by) VALUES (?,?,?,?,?,?)",
        (d["meeting_id"], d.get("agenda_id"), d["content"], d.get("decision",""), "draft", u["id"])
    )
    return ok({"id": nid})

@app.put("/api/minutes/<int:nid>")
@require_login
def update_minutes(nid):
    d = request.json or {}
    fields, vals = [], []
    for k in ("content","decision","status"):
        if k in d:
            fields.append(f"{k}=?"); vals.append(d[k])
    if fields:
        vals.append(nid)
        execute(f"UPDATE minutes SET {','.join(fields)},updated_at=CURRENT_TIMESTAMP WHERE id=?", vals)
    if d.get("action") == "sign":
        execute("UPDATE minutes SET status='signed',signed_by=?,signed_at=CURRENT_TIMESTAMP WHERE id=?",
                (g.current_user["id"], nid))
    return ok()

# ══════════════════════════════════════════════
# 议题转换（场馆中心）
# ══════════════════════════════════════════════
@app.get("/api/conversions")
@require_login
def list_conversions():
    return ok(query("""
        SELECT c.*, a.title src_title, a.category src_category
        FROM conversions c JOIN agendas a ON a.id=c.source_agenda_id
        ORDER BY c.created_at DESC LIMIT 50
    """))

@app.post("/api/conversions")
@require_login
def create_conversion():
    u = g.current_user
    d = request.json or {}
    src_id = d.get("source_agenda_id")
    src = query("SELECT * FROM agendas WHERE id=?", (src_id,), one=True)
    if not src: return err("源议题不存在", 404)
    # 自动生成转换建议
    suggestion = {"rule": "三重一大规则", "action": "转局长办公会",
                  "reason": f"金额 {src.get('amount',0)} 万元，类别 {src.get('category','')}", "confidence": 0.92}
    cid = execute(
        "INSERT INTO conversions (source_agenda_id, rule_suggestion, operator_id, status) VALUES (?,?,?,?)",
        (src_id, json.dumps(suggestion, ensure_ascii=False), u["id"], "pending")
    )
    execute("UPDATE agendas SET status='converted',updated_at=CURRENT_TIMESTAMP WHERE id=?", (src_id,))
    return ok({"id": cid, "suggestion": suggestion})

@app.put("/api/conversions/<int:cid>/confirm")
@require_login
def confirm_conversion(cid):
    u = g.current_user
    d = request.json or {}
    conv = query("SELECT * FROM conversions WHERE id=?", (cid,), one=True)
    if not conv: return err("转换记录不存在", 404)
    # 生成新的局长办公会议题
    src = query("SELECT * FROM agendas WHERE id=?", (conv["source_agenda_id"],), one=True)
    new_title   = d.get("title",   f"[转换] {src['title']}")
    new_content = d.get("content", src["content"])
    serial = auto_serial("局办公室")
    new_id = execute(
        "INSERT INTO agendas (serial_no,title,dept,category,content,summary,amount,status,source,created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (serial, new_title, "局办公室", src["category"], new_content,
         src.get("summary",""), src.get("amount",0), "dept_review", "converted", u["id"])
    )
    execute("UPDATE conversions SET target_agenda_id=?,status='confirmed' WHERE id=?", (new_id, cid))
    return ok({"new_agenda_id": new_id, "serial_no": serial})

# ══════════════════════════════════════════════
# 通知
# ══════════════════════════════════════════════
@app.get("/api/notifications")
@require_login
def list_notifications():
    uid  = g.current_user["id"]
    rows = query("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 30", (uid,))
    unread = query("SELECT COUNT(*) cnt FROM notifications WHERE user_id=? AND is_read=0", (uid,))[0]["cnt"]
    return ok({"list": rows, "unread": unread})

@app.post("/api/notifications/read-all")
@require_login
def read_all():
    execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (g.current_user["id"],))
    return ok()

# ══════════════════════════════════════════════
# 数据统计
# ══════════════════════════════════════════════
@app.get("/api/stats/overview")
@require_login
def stats_overview():
    year = request.args.get("year", datetime.now().year)
    u    = g.current_user

    total   = query("SELECT COUNT(*) cnt FROM agendas")[0]["cnt"]
    pending = query("SELECT COUNT(*) cnt FROM agendas WHERE status NOT IN ('archived','abandoned')")[0]["cnt"]
    mtg     = query("SELECT COUNT(*) cnt FROM meetings WHERE status='finished'")[0]["cnt"]

    dept_stats = query("""
        SELECT dept, COUNT(*) cnt, SUM(amount) total_amount
        FROM agendas WHERE status NOT IN ('abandoned') GROUP BY dept ORDER BY cnt DESC
    """)

    income = query("SELECT SUM(amount) v FROM budgets WHERE year=? AND category='收入'", (year,))[0]["v"] or 0
    spend  = query("SELECT SUM(amount) v FROM budgets WHERE year=? AND category='支出'",  (year,))[0]["v"] or 0

    dept_spend = query("SELECT dept, amount FROM budgets WHERE year=? AND category='支出' ORDER BY amount DESC", (year,))

    major3 = query("""SELECT id, serial_no, title, dept, created_at, status
                      FROM agendas WHERE category='三重一大' AND status NOT IN ('abandoned')
                      ORDER BY created_at DESC LIMIT 10""")

    return ok({
        "total_agendas":     total,
        "pending_agendas":   pending,
        "meetings_finished": mtg,
        "income":            income,
        "spend":             spend,
        "dept_stats":        dept_stats,
        "dept_spend":        dept_spend,
        "major3":            major3,
    })

# ══════════════════════════════════════════════
# 静态文件（直接访问前端 HTML）
# ══════════════════════════════════════════════
@app.get("/")
def index():
    return send_from_directory(os.path.join(BASE_DIR, STATIC_DIR), "miniapp_preview.html")

# ══════════════════════════════════════════════
if __name__ == "__main__":
    # 确保数据库存在
    if not os.path.exists(DB_PATH):
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("db_init", os.path.join(BASE_DIR, "db_init.py"))
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.init_db()
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=not IS_CLOUD)
