"""
北京市朝阳区体育局 议题一体化系统
数据库初始化脚本 - SQLite
创建所有表结构并插入初始数据
"""

import sqlite3
import os
import hashlib
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "agendas.db")


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ─────────────────────────────────────────
    # 1. 用户表
    # ─────────────────────────────────────────
    cur.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA foreign_keys=ON;

    CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        username    TEXT NOT NULL UNIQUE,
        password    TEXT NOT NULL,
        real_name   TEXT NOT NULL,
        role        TEXT NOT NULL CHECK(role IN
                    ('admin','leader','dept_head','staff','office_staff','office_leader','venue_staff','venue_office')),
        dept        TEXT NOT NULL,
        is_active   INTEGER DEFAULT 1,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    -- ─────────────────────────────────────────
    -- 2. 议题模板表
    -- ─────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS templates (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL,
        dept         TEXT NOT NULL,
        category     TEXT NOT NULL,
        fields_json  TEXT NOT NULL,        -- JSON: 模板字段定义
        description  TEXT,
        is_active    INTEGER DEFAULT 1,
        created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    -- ─────────────────────────────────────────
    -- 3. 议题主表
    -- ─────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS agendas (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        serial_no       TEXT UNIQUE,               -- 编号: YYMM-DEPT-SEQ
        title           TEXT NOT NULL,
        dept            TEXT NOT NULL,
        category        TEXT NOT NULL,             -- 三重一大 / 行政事项 / 其他
        template_id     INTEGER REFERENCES templates(id),
        content         TEXT NOT NULL,             -- 正文内容（富文本/Markdown）
        summary         TEXT,                      -- 摘要
        amount          REAL DEFAULT 0,            -- 涉及金额
        status          TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft','submitted','dept_review','office_review',
                                         'converted','scheduled','meeting','minutes_draft',
                                         'minutes_signed','archived','rejected','abandoned')),
        source          TEXT DEFAULT 'write'
                        CHECK(source IN ('write','upload','converted')),
        created_by      INTEGER NOT NULL REFERENCES users(id),
        created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
        submitted_at    DATETIME,
        archived_at     DATETIME,
        remarks         TEXT                       -- 备注/驳回原因
    );

    -- ─────────────────────────────────────────
    -- 4. 审批记录表（工作流）
    -- ─────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS approvals (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        agenda_id   INTEGER NOT NULL REFERENCES agendas(id),
        step        TEXT NOT NULL,                 -- dept_review / office_review / leader_review
        action      TEXT NOT NULL CHECK(action IN ('pass','reject','comment','sign')),
        operator_id INTEGER NOT NULL REFERENCES users(id),
        comment     TEXT,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    -- ─────────────────────────────────────────
    -- 5. 局长办公会批次表
    -- ─────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS meetings (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_no     TEXT NOT NULL UNIQUE,         -- 批次号: 2026-第01次
        title        TEXT NOT NULL,
        meeting_date DATE NOT NULL,
        location     TEXT,
        status       TEXT NOT NULL DEFAULT 'planning'
                     CHECK(status IN ('planning','ongoing','finished','archived')),
        created_by   INTEGER REFERENCES users(id),
        created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    -- ─────────────────────────────────────────
    -- 6. 上会议题关联表（排期）
    -- ─────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS meeting_agendas (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id  INTEGER NOT NULL REFERENCES meetings(id),
        agenda_id   INTEGER NOT NULL REFERENCES agendas(id),
        seq_no      INTEGER DEFAULT 0,             -- 议题顺序
        status      TEXT DEFAULT 'pending'
                    CHECK(status IN ('pending','passed','rejected')),
        leader_comment TEXT,
        UNIQUE(meeting_id, agenda_id)
    );

    -- ─────────────────────────────────────────
    -- 7. 会议纪要表
    -- ─────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS minutes (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id   INTEGER NOT NULL REFERENCES meetings(id),
        agenda_id    INTEGER REFERENCES agendas(id),  -- NULL则为整场纪要
        content      TEXT NOT NULL,
        decision     TEXT,                            -- 决议事项
        status       TEXT DEFAULT 'draft'
                     CHECK(status IN ('draft','leader_review','signed','archived')),
        signed_by    INTEGER REFERENCES users(id),
        signed_at    DATETIME,
        created_by   INTEGER REFERENCES users(id),
        created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    -- ─────────────────────────────────────────
    -- 8. 议题转换记录（场馆中心专用）
    -- ─────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS conversions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source_agenda_id INTEGER NOT NULL REFERENCES agendas(id),
        target_agenda_id INTEGER REFERENCES agendas(id),
        rule_suggestion  TEXT,               -- 系统给出的转换建议JSON
        operator_id      INTEGER REFERENCES users(id),
        status           TEXT DEFAULT 'pending'
                         CHECK(status IN ('pending','confirmed','adjusted','rejected')),
        notes            TEXT,
        created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    -- ─────────────────────────────────────────
    -- 9. 附件表
    -- ─────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS attachments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        agenda_id   INTEGER REFERENCES agendas(id),
        meeting_id  INTEGER REFERENCES meetings(id),
        filename    TEXT NOT NULL,
        filepath    TEXT NOT NULL,
        filetype    TEXT,
        filesize    INTEGER DEFAULT 0,
        uploaded_by INTEGER REFERENCES users(id),
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    -- ─────────────────────────────────────────
    -- 10. 通知消息表
    -- ─────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS notifications (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        type        TEXT NOT NULL,              -- approval / meeting / system
        title       TEXT NOT NULL,
        content     TEXT,
        agenda_id   INTEGER REFERENCES agendas(id),
        is_read     INTEGER DEFAULT 0,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    -- ─────────────────────────────────────────
    -- 11. 财务预算表（数据统计用）
    -- ─────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS budgets (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        year        INTEGER NOT NULL,
        dept        TEXT NOT NULL,
        category    TEXT NOT NULL,              -- 收入 / 支出 / 三重一大
        amount      REAL NOT NULL,
        description TEXT,
        agenda_id   INTEGER REFERENCES agendas(id),
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(year, dept, category)
    );

    -- ─────────────────────────────────────────
    -- 12. 会话表（持久化登录状态，重启不丢失）
    -- ─────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS sessions (
        token        TEXT PRIMARY KEY,
        user_id      INTEGER NOT NULL REFERENCES users(id),
        created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
        expires_at   DATETIME NOT NULL,
        ip_addr      TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

    -- ─────────────────────────────────────────
    -- 13. 登录日志表
    -- ─────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS login_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER REFERENCES users(id),
        username    TEXT,
        ip_addr     TEXT,
        action      TEXT DEFAULT 'login',
        result      TEXT DEFAULT 'success',
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    -- ─────────────────────────────────────────
    -- 索引
    -- ─────────────────────────────────────────
    CREATE INDEX IF NOT EXISTS idx_agendas_dept        ON agendas(dept);
    CREATE INDEX IF NOT EXISTS idx_agendas_status      ON agendas(status);
    CREATE INDEX IF NOT EXISTS idx_agendas_created_by  ON agendas(created_by);
    CREATE INDEX IF NOT EXISTS idx_approvals_agenda    ON approvals(agenda_id);
    CREATE INDEX IF NOT EXISTS idx_notif_user          ON notifications(user_id, is_read);
    CREATE INDEX IF NOT EXISTS idx_meeting_agendas_mtg ON meeting_agendas(meeting_id);
    """)

    # ─────────────────────────────────────────
    # 插入初始用户数据
    # ─────────────────────────────────────────
    users_seed = [
        ("admin",         hash_password("admin123"),   "系统管理员", "admin",         "信息中心"),
        ("leader",        hash_password("leader123"),  "局领导",     "leader",         "局领导班子"),
        ("office_leader", hash_password("office123"),  "局办主任",   "office_leader",  "局办公室"),
        ("miao_yifei",    hash_password("staff123"),   "缪逸飞",     "staff",          "场馆中心"),
        ("venue_head",    hash_password("head123"),    "场馆主任",   "dept_head",      "场馆中心"),
        ("venue_office",  hash_password("venue123"),   "场馆办公室", "venue_office",   "场馆中心"),
        ("hr_staff",      hash_password("hr123"),      "组织人事科员","staff",         "组织人事科"),
        ("office_staff",  hash_password("ostaff123"),  "局办科员",   "office_staff",   "局办公室"),
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO users (username, password, real_name, role, dept) VALUES (?,?,?,?,?)",
        users_seed
    )

    # ─────────────────────────────────────────
    # 插入议题模板
    # ─────────────────────────────────────────
    import json
    templates_seed = [
        ("三重一大事项议题", "场馆中心", "三重一大", json.dumps([
            {"field": "topic_type", "label": "事项类型", "type": "select",
             "options": ["重大决策", "重要干部任免", "重大项目", "大额资金"], "required": True},
            {"field": "background", "label": "事项背景", "type": "textarea", "required": True},
            {"field": "content",    "label": "事项内容", "type": "textarea", "required": True},
            {"field": "amount",     "label": "涉及金额(万元)", "type": "number", "required": False},
            {"field": "opinion",    "label": "部门意见", "type": "textarea", "required": True},
        ], ensure_ascii=False), "场馆中心三重一大事项议题模板"),

        ("工程项目议题", "场馆中心", "行政事项", json.dumps([
            {"field": "project_name", "label": "项目名称", "type": "text", "required": True},
            {"field": "project_type", "label": "项目类型", "type": "select",
             "options": ["新建", "改建", "维修", "采购"], "required": True},
            {"field": "background",   "label": "项目背景", "type": "textarea", "required": True},
            {"field": "scope",        "label": "建设内容与规模", "type": "textarea", "required": True},
            {"field": "amount",       "label": "预算金额(万元)", "type": "number", "required": True},
            {"field": "schedule",     "label": "建设周期", "type": "text", "required": True},
            {"field": "opinion",      "label": "部门意见", "type": "textarea", "required": True},
        ], ensure_ascii=False), "工程改造/新建项目议题"),

        ("活动赛事议题", "场馆中心", "行政事项", json.dumps([
            {"field": "event_name",   "label": "活动名称",   "type": "text",     "required": True},
            {"field": "event_date",   "label": "活动时间",   "type": "date",     "required": True},
            {"field": "event_venue",  "label": "活动地点",   "type": "text",     "required": True},
            {"field": "participants", "label": "参与人数",   "type": "number",   "required": True},
            {"field": "content",      "label": "活动方案",   "type": "textarea", "required": True},
            {"field": "amount",       "label": "经费预算(万元)", "type": "number", "required": False},
            {"field": "opinion",      "label": "部门意见",   "type": "textarea", "required": True},
        ], ensure_ascii=False), "体育活动/赛事举办议题"),

        ("人员任免议题", "组织人事科", "三重一大", json.dumps([
            {"field": "person_name",  "label": "人员姓名", "type": "text",     "required": True},
            {"field": "from_post",    "label": "原职务",   "type": "text",     "required": False},
            {"field": "to_post",      "label": "拟任职务", "type": "text",     "required": True},
            {"field": "reason",       "label": "任免理由", "type": "textarea", "required": True},
            {"field": "opinion",      "label": "人事意见", "type": "textarea", "required": True},
        ], ensure_ascii=False), "干部任免议题模板"),

        ("局办行政议题", "局办公室", "行政事项", json.dumps([
            {"field": "issue_title",  "label": "议题标题", "type": "text",     "required": True},
            {"field": "background",   "label": "事项背景", "type": "textarea", "required": True},
            {"field": "content",      "label": "议题内容", "type": "textarea", "required": True},
            {"field": "suggestion",   "label": "拟办意见", "type": "textarea", "required": True},
        ], ensure_ascii=False), "局办公室通用行政议题"),
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO templates (name, dept, category, fields_json, description) VALUES (?,?,?,?,?)",
        templates_seed
    )

    # ─────────────────────────────────────────
    # 插入示例会议批次
    # ─────────────────────────────────────────
    meetings_seed = [
        ("2026-第01次", "2026年第1次局长办公会", "2026-03-15", "局会议室101", "finished"),
        ("2026-第02次", "2026年第2次局长办公会", "2026-04-18", "局会议室101", "finished"),
        ("2026-第03次", "2026年第3次局长办公会", "2026-05-20", "局会议室101", "finished"),
        ("2026-第04次", "2026年第4次局长办公会", "2026-06-15", "局会议室101", "planning"),
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO meetings (batch_no, title, meeting_date, location, status) VALUES (?,?,?,?,?)",
        meetings_seed
    )

    # ─────────────────────────────────────────
    # 插入示例预算数据
    # ─────────────────────────────────────────
    budgets_seed = [
        (2026, "场馆中心",  "收入", 1250.0, "体育局年度收入预算"),
        (2026, "场馆中心",  "支出", 980.0,  "场馆运维及改造支出"),
        (2026, "组织人事科","支出", 120.0,  "人事培训支出"),
        (2026, "局办公室",  "支出", 200.0,  "行政运营支出"),
        (2026, "全局",      "收入", 3800.0, "全局年度总收入"),
        (2026, "全局",      "支出", 3200.0, "全局年度总支出"),
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO budgets (year, dept, category, amount, description) VALUES (?,?,?,?,?)",
        budgets_seed
    )

    conn.commit()
    conn.close()
    print(f"[OK] 数据库初始化完成: {DB_PATH}")
    print(f"[OK] 已创建13张表 + 初始用户/模板/会议/预算数据")


if __name__ == "__main__":
    init_db()
