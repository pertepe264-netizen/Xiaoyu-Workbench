"""班长全家桶 · 本地统一状态库（SQLite，方案 A）—— 20261012

一条片子 = 工程目录里一个 ``project.db``。新工程的全部状态（片级设置、段落
元数据、导演台面板、媒体架段槽位、文案台台词、操作流水、还原点、版本台账）
都收口到这个库里；大文件（latent / cont / mp4 / wav）仍留在文件系统，库里
只存路径与参数。

与旧 JSON 档案的关系（用户拍板：老工程不管）：

* 工程目录里**已有 chain.json** 的 = 老工程，永远继续走 JSON，本模块不碰；
* 两者都没有的工程第一次写时**自动建库**，此后只走 DB、不再产生 JSON；
* 工程目录里有 project.db 的 = DB 工程，权威在库。

并发模型：每库一个连接（WAL，多读单写）+ 每库一把 RLock。csglide_chain 的
公开写点本来就全包在全局 ``_chain_lock`` 里，库调用在其内天然串行；本模块
自带的 per-db 锁给自检脚本与未来直连兜底。所有写都是完整事务，崩溃不留
半截状态；操作流水 ``events.seq`` 由 SQLite AUTOINCREMENT 发号，全局单调，
前端按它对账，迟到旧写不可能盖掉新状态。

本模块**不 import csglide_chain**（单向依赖：chain → store），工程目录口径
与 ``csglide_chain.chain_dir`` 保持逐字一致（两处同实现，改一处要同步另一处）。
"""

import json
import os
import sqlite3
import threading
import time

try:
    import folder_paths
except Exception:      # pragma: no cover
    folder_paths = None

DB_FILENAME = "project.db"
SCHEMA_VERSION = 1

# 片级键值表（meta）里允许出现的键；chain 文档其余键也照存，这里只列默认值。
_CHAIN_KEYS = ("name", "fps", "trim_frames", "prompt_global", "current",
               "locked", "order", "transitions", "gen")
_CHAIN_DEFAULTS = {
    "name": "", "fps": 24, "trim_frames": 22, "prompt_global": "",
    "current": None, "locked": False, "order": [], "transitions": {},
    "gen": 0,
}
# clip 文档里被抽成 shots 标量列的键（其余一律进 meta_json，绝不丢字段）。
_SHOT_SCALARS = {
    "shot_uid": ("shot", ""),       # 标量列名, (clip dict 键, 默认值)
    "name": ("name", ""),
    "frames": ("frames", 0),
    "locked": ("locked", 0),
    "pending": ("pending", None),
}

# --------------------------------------------------------------------------
# 连接管理（每工程目录一个连接，按绝对路径缓存）
# --------------------------------------------------------------------------

_conns = {}                 # normpath(dir) -> sqlite3.Connection
_db_locks = {}              # normpath(dir) -> RLock
_conns_guard = threading.Lock()


def _out_dir():
    # 与 csglide_chain._out_dir 同口径。
    return folder_paths.get_output_directory() if folder_paths is not None else "."


def chain_dir(name):
    """与 csglide_chain.chain_dir 逐字同口径（两处同步维护）。"""
    safe = "".join(c for c in (name or "未命名") if c not in '\\/:*?"<>|').strip() or "未命名"
    return os.path.join(_out_dir(), "h3_chain", safe)


def db_path(d):
    return os.path.join(d, DB_FILENAME)


def has_db(d):
    """工程目录里是否已有状态库。"""
    return os.path.isfile(db_path(d))


def _lock_for(d):
    key = os.path.normcase(os.path.abspath(d))
    with _conns_guard:
        lk = _db_locks.get(key)
        if lk is None:
            lk = threading.RLock()
            _db_locks[key] = lk
        return lk


def _connect(d, create=True):
    """拿（必要时建）该工程的库连接。create=False 且库不存在时返回 None。"""
    key = os.path.normcase(os.path.abspath(d))
    with _conns_guard:
        conn = _conns.get(key)
        if conn is not None:
            return conn
        if not create and not os.path.isfile(db_path(d)):
            return None
        os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(db_path(d), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _create_schema(conn)
        _conns[key] = conn
        return conn


def close_db(d):
    """工程改名/删除后关掉旧路径连接（WAL 先 checkpoint，文件随目录搬走）。"""
    key = os.path.normcase(os.path.abspath(d))
    with _conns_guard:
        conn = _conns.pop(key, None)
    if conn is not None:
        try:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
            conn.close()
        except Exception:
            pass


def backend(d):
    """返回该工程当前的存储后端：'db' / 'json' / None（啥都还没有）。

    判定只读磁盘、不建库。db 与 json 并存时以 db 为权威（正常不会并存）。
    """
    if has_db(d):
        return "db"
    if os.path.isfile(os.path.join(d, "chain.json")):
        return "json"
    return None


def ensure_db(d):
    """首次写定型：db 在→db；chain.json 在（老工程）→不动，返回 'json'；
    都没有（全新工程）→建库，返回 'db'。"""
    be = backend(d)
    if be == "db":
        _connect(d)
        return "db"
    if be == "json":
        return "json"
    _connect(d, create=True)
    return "db"


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

def _create_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shots (
            cid        TEXT PRIMARY KEY,
            ord        INTEGER NOT NULL DEFAULT 0,
            shot_uid   TEXT NOT NULL DEFAULT '',
            name       TEXT NOT NULL DEFAULT '',
            frames     INTEGER NOT NULL DEFAULT 0,
            locked     INTEGER NOT NULL DEFAULT 0,
            pending    TEXT,
            created_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            meta_json  TEXT NOT NULL DEFAULT '{}'   -- clip 文档全量（state 除外）
        );
        CREATE INDEX IF NOT EXISTS idx_shots_ord ON shots(ord);

        CREATE TABLE IF NOT EXISTS shot_state (
            cid TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rack_slots (
            cid      TEXT NOT NULL,
            kind     TEXT NOT NULL,                 -- images / videos / audios
            slot_idx INTEGER NOT NULL,
            file     TEXT NOT NULL DEFAULT '',
            params   TEXT NOT NULL DEFAULT '{}',
            updated_at REAL NOT NULL,
            PRIMARY KEY (cid, kind, slot_idx)
        );
        CREATE INDEX IF NOT EXISTS idx_rack_file ON rack_slots(file);

        CREATE TABLE IF NOT EXISTS events (
            seq     INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      REAL NOT NULL,
            actor   TEXT NOT NULL DEFAULT '',
            action  TEXT NOT NULL,
            cid     TEXT,
            payload TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'auto',   -- auto / manual / before_action
            shots_json TEXT NOT NULL,
            desk_json TEXT NOT NULL DEFAULT '{}',
            cur_cid TEXT, idx INTEGER,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shot_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cid TEXT NOT NULL,
            take INTEGER NOT NULL,
            frames INTEGER NOT NULL DEFAULT 0,
            width INTEGER, height INTEGER,
            take_params TEXT NOT NULL DEFAULT '{}',
            film_path TEXT, latent_path TEXT, cont_path TEXT, audio_path TEXT,
            film_frames INTEGER NOT NULL DEFAULT 0,
            is_current INTEGER NOT NULL DEFAULT 1,
            note TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            UNIQUE(cid, take)
        );

        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cid TEXT NOT NULL, take INTEGER NOT NULL DEFAULT 1,
            kind TEXT NOT NULL,
            path TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0,
            mtime REAL NOT NULL DEFAULT 0,
            UNIQUE(cid, take, kind, path)
        );
        """
    )
    conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
    conn.commit()


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _jdumps(v):
    return json.dumps(v, ensure_ascii=False)


def _jloads(s, default):
    if s is None:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def _meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return _jloads(row[0], default)


def _meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, _jdumps(value)))


def _meta_set_many(conn, items):
    conn.executemany(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [(k, _jdumps(v)) for k, v in items])


# --------------------------------------------------------------------------
# 片级文档（chain dict）
# --------------------------------------------------------------------------

def load_chain_doc(d, name):
    """从库拼出与旧 chain.json 同构的 dict。库不存在返回 None。"""
    conn = _connect(d, create=False)
    if conn is None:
        return None
    with _lock_for(d):
        doc = dict(_CHAIN_DEFAULTS)
        doc["name"] = name
        for k in _CHAIN_KEYS:
            if k == "name":
                continue
            row = conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
            if row is not None:
                doc[k] = _jloads(row[0], _CHAIN_DEFAULTS.get(k))
        return doc


def save_chain_doc(d, name, data):
    """片级文档落库（调用方须已持写锁/在 chainlocked 内）。返回 'db'。"""
    conn = _connect(d, create=True)
    now = time.time()
    with _lock_for(d):
        try:
            conn.execute("BEGIN IMMEDIATE")
            items = [("name", name)]
            for k in _CHAIN_KEYS:
                if k == "name":
                    continue
                if k in data:
                    items.append((k, data.get(k)))
                else:
                    items.append((k, _CHAIN_DEFAULTS.get(k)))
            _meta_set_many(conn, items)
            if conn.execute("SELECT value FROM meta WHERE key='created_at'").fetchone() is None:
                _meta_set(conn, "created_at", now)
            _meta_set(conn, "updated_at", now)
            # 顺序同步到 shots.ord（标量查询列；order 本身以 meta 为权威）
            order = [str(c) for c in (data.get("order") or []) if c]
            for i, cid in enumerate(order):
                conn.execute("UPDATE shots SET ord=? WHERE cid=?", (i, cid))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return "db"


# --------------------------------------------------------------------------
# 段文档（clip dict）
# --------------------------------------------------------------------------

def load_clip_doc(d, cid):
    """从库拼出与旧 clip_XXXX.json 同构的 dict（含 state）。没有返回 None。"""
    conn = _connect(d, create=False)
    if conn is None:
        return None
    with _lock_for(d):
        row = conn.execute("SELECT * FROM shots WHERE cid=?", (str(cid),)).fetchone()
        if row is None:
            return None
        meta = _jloads(row["meta_json"], {})
        if not isinstance(meta, dict):
            meta = {}
        # 标量列回填（以列为地面真相，meta_json 只作兜底）
        meta["id"] = str(cid)
        meta["shot"] = row["shot_uid"] or meta.get("shot", "")
        meta["name"] = row["name"] or meta.get("name", "")
        meta["frames"] = int(row["frames"] or meta.get("frames", 0) or 0)
        meta["locked"] = bool(row["locked"])
        meta["pending"] = row["pending"]
        meta["updated"] = row["updated_at"] or meta.get("updated")
        srow = conn.execute("SELECT state_json FROM shot_state WHERE cid=?",
                            (str(cid),)).fetchone()
        if srow is not None:
            st = _jloads(srow["state_json"], None)
            if st is not None:
                meta["state"] = st
        return meta


def save_clip_doc(d, cid, meta):
    """段文档落库。state 抽进 shot_state（高频小表，防抖整包写不污染全量），
    其余字段进 shots.meta_json + 标量列。"""
    conn = _connect(d, create=True)
    cid = str(cid)
    now = time.time()
    doc = dict(meta or {})
    state = doc.pop("state", None)
    with _lock_for(d):
        try:
            conn.execute("BEGIN IMMEDIATE")
            existed = conn.execute("SELECT 1 FROM shots WHERE cid=?", (cid,)).fetchone()
            shot_uid = str(doc.get("shot") or "")
            name = str(doc.get("name") or "")
            frames = int(doc.get("frames") or 0)
            locked = 1 if doc.get("locked") else 0
            pending = doc.get("pending")
            ord_val = int(doc.get("index") or 0) - 1
            if ord_val < 0:
                # 没带 index 就按当前 order 位置
                order = _meta_get(conn, "order", []) or []
                ord_val = order.index(cid) if cid in order else 0
            if existed:
                conn.execute(
                    "UPDATE shots SET ord=?,shot_uid=?,name=?,frames=?,locked=?,"
                    "pending=?,updated_at=?,meta_json=? WHERE cid=?",
                    (ord_val, shot_uid, name, frames, locked, pending,
                     now, _jdumps(doc), cid))
            else:
                conn.execute(
                    "INSERT INTO shots(cid,ord,shot_uid,name,frames,locked,pending,"
                    "created_at,updated_at,meta_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (cid, ord_val, shot_uid, name, frames, locked, pending,
                     now, now, _jdumps(doc)))
            if state is not None:
                conn.execute(
                    "INSERT INTO shot_state(cid,state_json,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(cid) DO UPDATE SET state_json=excluded.state_json,"
                    "updated_at=excluded.updated_at",
                    (cid, _jdumps(state), now))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return "db"


def delete_clip_doc(d, cid):
    """删段：shots 级联清掉 state / rack_slots / versions / files。"""
    conn = _connect(d, create=False)
    if conn is None:
        return False
    with _lock_for(d):
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("DELETE FROM shots WHERE cid=?", (str(cid),))
            conn.execute("DELETE FROM shot_state WHERE cid=?", (str(cid),))
            conn.execute("DELETE FROM rack_slots WHERE cid=?", (str(cid),))
            conn.execute("DELETE FROM shot_versions WHERE cid=?", (str(cid),))
            conn.execute("DELETE FROM files WHERE cid=?", (str(cid),))
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            conn.rollback()
            raise


def all_clip_cids(d):
    conn = _connect(d, create=False)
    if conn is None:
        return []
    with _lock_for(d):
        # 以 meta.order 为权威，没有则按 ord 列
        order = _meta_get(conn, "order", []) or []
        if order:
            return [str(c) for c in order]
        return [r[0] for r in conn.execute(
            "SELECT cid FROM shots ORDER BY ord, cid").fetchall()]


# --------------------------------------------------------------------------
# 媒体架段槽位（rack）
# --------------------------------------------------------------------------

def load_rack(d, cid):
    """读某段媒体架：{slots:{images,videos,audios}, fps,audio_out}。
    没写过返回 None（前端据此不覆盖内存里的现状）。"""
    conn = _connect(d, create=False)
    if conn is None:
        return None
    cid = str(cid)
    with _lock_for(d):
        rows = conn.execute(
            "SELECT kind,slot_idx,file,params FROM rack_slots WHERE cid=? "
            "ORDER BY kind,slot_idx", (cid,)).fetchall()
        if not rows:
            return None
        slots = {"images": [], "videos": [], "audios": []}
        maxidx = {"images": -1, "videos": -1, "audios": -1}
        for r in rows:
            kind = r["kind"] if r["kind"] in slots else "images"
            item = _jloads(r["params"], {})
            if not isinstance(item, dict):
                item = {}
            if r["file"]:
                item["file"] = r["file"]
            slots[kind].append((r["slot_idx"], item))
            maxidx[kind] = max(maxidx[kind], r["slot_idx"])
        out = {"images": [None] * 0, "videos": [], "audios": []}
        for kind in ("images", "videos", "audios"):
            arr = [None] * (maxidx[kind] + 1)
            for idx, item in slots[kind]:
                arr[idx] = item
            out[kind] = arr
        return {"slots": out,
                "fps": _meta_get(conn, "rack.fps", 0) or 0,
                "audio_out": _meta_get(conn, "rack.audio_out", 0) or 0}


def save_rack(d, cid, rack):
    """整段替换某段槽位（事务内 delete+insert），并更新片级 fps/audio_out。"""
    conn = _connect(d, create=True)
    cid = str(cid)
    now = time.time()
    slots = (rack or {}).get("slots") or {}
    with _lock_for(d):
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM rack_slots WHERE cid=?", (cid,))
            rows = []
            for kind in ("images", "videos", "audios"):
                for idx, item in enumerate(slots.get(kind) or []):
                    if not item:
                        continue
                    if not isinstance(item, dict):
                        continue
                    it = dict(item)
                    f = str(it.pop("file", "") or "")
                    rows.append((cid, kind, idx, f, _jdumps(it), now))
            conn.executemany(
                "INSERT INTO rack_slots(cid,kind,slot_idx,file,params,updated_at) "
                "VALUES(?,?,?,?,?,?)", rows)
            if (rack or {}).get("fps") is not None:
                _meta_set(conn, "rack.fps", rack.get("fps"))
            if (rack or {}).get("audio_out") is not None:
                _meta_set(conn, "rack.audio_out", rack.get("audio_out"))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return True


def load_all_racks(d):
    """工程内所有段的架况（rehydrate 一次拉全）。"""
    conn = _connect(d, create=False)
    if conn is None:
        return {}
    with _lock_for(d):
        cids = [r[0] for r in conn.execute(
            "SELECT DISTINCT cid FROM rack_slots").fetchall()]
    return {cid: load_rack(d, cid) for cid in cids}


# --------------------------------------------------------------------------
# 文案台（desk）
# --------------------------------------------------------------------------

def load_desk(d):
    conn = _connect(d, create=False)
    if conn is None:
        return None
    with _lock_for(d):
        return _meta_get(conn, "desk", None)


def save_desk(d, desk):
    conn = _connect(d, create=True)
    with _lock_for(d):
        _meta_set(conn, "desk", desk or {})
        conn.commit()
    return True


# --------------------------------------------------------------------------
# 操作流水（events，只追加）
# --------------------------------------------------------------------------

def log_event(d, action, cid=None, payload=None, actor="backend"):
    """记一条操作流水，返回服务端单调 seq。库不存在（老工程）返回 None。"""
    conn = _connect(d, create=False)
    if conn is None:
        return None
    with _lock_for(d):
        cur = conn.execute(
            "INSERT INTO events(ts,actor,action,cid,payload) VALUES(?,?,?,?,?)",
            (time.time(), str(actor or ""), str(action or ""),
             str(cid) if cid is not None else None,
             _jdumps(payload if payload is not None else {})))
        conn.commit()
        return cur.lastrowid


def list_events(d, limit=100, cid=None, action=None):
    conn = _connect(d, create=False)
    if conn is None:
        return []
    sql = "SELECT seq,ts,actor,action,cid,payload FROM events"
    where, args = [], []
    if cid:
        where.append("cid=?")
        args.append(str(cid))
    if action:
        where.append("action=?")
        args.append(str(action))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY seq DESC LIMIT ?"
    args.append(int(limit or 100))
    with _lock_for(d):
        rows = [dict(x) for x in conn.execute(sql, args).fetchall()]
    rows.reverse()          # DESC 翻回时间正序（旧→新）
    for r in rows:
        r["payload"] = _jloads(r.get("payload"), {})
    return rows


# --------------------------------------------------------------------------
# 还原点（snapshots）
# --------------------------------------------------------------------------

def add_snapshot(d, label, shots_json, desk_json=None, cur_cid=None, idx=None,
                 source="auto"):
    conn = _connect(d, create=True)
    with _lock_for(d):
        cur = conn.execute(
            "INSERT INTO snapshots(label,source,shots_json,desk_json,cur_cid,idx,"
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (str(label or ""), str(source or "auto"),
             shots_json if isinstance(shots_json, str) else _jdumps(shots_json),
             desk_json if isinstance(desk_json, str) else _jdumps(desk_json or {}),
             str(cur_cid) if cur_cid is not None else None,
             int(idx) if idx is not None else None, time.time()))
        conn.commit()
        return cur.lastrowid


def list_snapshots(d, limit=200):
    conn = _connect(d, create=False)
    if conn is None:
        return []
    with _lock_for(d):
        rows = conn.execute(
            "SELECT id,label,source,cur_cid,idx,created_at FROM snapshots "
            "ORDER BY id DESC LIMIT ?", (int(limit or 200),)).fetchall()
    return [dict(r) for r in rows]


def get_snapshot(d, sid):
    conn = _connect(d, create=False)
    if conn is None:
        return None
    with _lock_for(d):
        r = conn.execute("SELECT * FROM snapshots WHERE id=?", (int(sid),)).fetchone()
    if r is None:
        return None
    out = dict(r)
    out["shots"] = _jloads(r["shots_json"], [])
    out["desk"] = _jloads(r["desk_json"], {})
    return out


def delete_snapshot(d, sid):
    conn = _connect(d, create=False)
    if conn is None:
        return False
    with _lock_for(d):
        cur = conn.execute("DELETE FROM snapshots WHERE id=?", (int(sid),))
        conn.commit()
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# 全量工程（rehydrate）与健康检查
# --------------------------------------------------------------------------

def project_blob(d, name, clips_loader=None):
    """一次拉全量：片级文档 + 各段 clip（含 state）+ 全部 rack + desk。

    clips_loader(name)->list[dict]：由 csglide_chain 传入 summary()，保证与
    时间轴/film_summary 完全同构（含 broken 等派生字段的原始材料由前端自算）；
    不传时直接从库拼。
    """
    be = backend(d)
    if be != "db":
        return {"name": name, "backend": be or "none"}
    chain_doc = load_chain_doc(d, name) or {}
    if clips_loader is not None:
        loaded = clips_loader(name)
        # loader 传的是 csglide_chain.summary()，返回整片 dict，取其 clips 列表
        clips = loaded.get("clips") if isinstance(loaded, dict) else loaded
    else:
        clips = []
        for cid in all_clip_cids(d):
            m = load_clip_doc(d, cid)
            if m is not None:
                clips.append(m)
    return {
        "name": name,
        "backend": "db",
        "schema": SCHEMA_VERSION,
        "fps": chain_doc.get("fps", 24),
        "trim_frames": chain_doc.get("trim_frames", 22),
        "prompt_global": chain_doc.get("prompt_global", ""),
        "current": chain_doc.get("current"),
        "locked": bool(chain_doc.get("locked")),
        "order": chain_doc.get("order", []),
        "transitions": chain_doc.get("transitions", {}),
        "gen": chain_doc.get("gen", 0),
        "clips": clips,
        "racks": load_all_racks(d),
        "desk": load_desk(d),
    }


def health(d):
    """库完整性自检。"""
    be = backend(d)
    out = {"backend": be, "db": None, "ok": be != "db", "schema": None,
           "shots": 0, "events": 0, "snapshots": 0, "errors": []}
    if be != "db":
        out["ok"] = True          # 老工程/空工程不算病
        return out
    conn = _connect(d)
    out["db"] = db_path(d)
    with _lock_for(d):
        try:
            ic = conn.execute("PRAGMA integrity_check").fetchone()
            ok = bool(ic and ic[0] == "ok")
            out["ok"] = ok
            if not ok:
                out["errors"].append(str(ic[0]) if ic else "integrity_check failed")
            out["schema"] = conn.execute("PRAGMA user_version").fetchone()[0]
            out["shots"] = conn.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
            out["events"] = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            out["snapshots"] = conn.execute(
                "SELECT COUNT(*) FROM snapshots").fetchone()[0]
        except Exception as e:
            out["ok"] = False
            out["errors"].append("%s: %s" % (type(e).__name__, e))
    return out


def repath_db(d, old_root, new_root):
    """工程改名后回写 meta_json / 各路径列里的旧绝对路径（与 chain 的
    _repoint_paths 同目的，作用对象换成库）。"""
    conn = _connect(d, create=False)
    if conn is None:
        return 0

    def _repoint(obj):
        if isinstance(obj, str):
            return new_root + obj[len(old_root):] if obj.startswith(old_root) else obj
        if isinstance(obj, dict):
            return {k: _repoint(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_repoint(v) for v in obj]
        return obj

    changed = 0
    with _lock_for(d):
        try:
            conn.execute("BEGIN IMMEDIATE")
            for r in conn.execute("SELECT cid,meta_json FROM shots").fetchall():
                doc = _jloads(r["meta_json"], {})
                ndoc = _repoint(doc)
                if _jdumps(ndoc) != r["meta_json"]:
                    conn.execute("UPDATE shots SET meta_json=? WHERE cid=?",
                                 (_jdumps(ndoc), r["cid"]))
                    changed += 1
            for col in ("film_path", "latent_path", "cont_path", "audio_path"):
                for r in conn.execute(
                        "SELECT cid,take,%s AS v FROM shot_versions WHERE %s LIKE ?"
                        % (col, col), (old_root.replace("%", "\\%") + "%",)).fetchall():
                    if r["v"]:
                        conn.execute(
                            "UPDATE shot_versions SET %s=? WHERE cid=? AND take=?"
                            % col, (new_root + r["v"][len(old_root):],
                                    r["cid"], r["take"]))
                        changed += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return changed


# --------------------------------------------------------------------------
# HTTP 路由（/cglide/store/*）—— 与 csglide_chain 同风格，模块加载即注册
# --------------------------------------------------------------------------

def _json_response(obj, status=200):
    from aiohttp import web
    return web.json_response(obj, status=status)


def _chain_module():
    """取已加载的 csglide_chain 模块（包名含连字符，直接 import 的路径不保证，
    从 sys.modules 按后缀找最稳）。只用于借它的 ws_broadcast 发广播。"""
    import sys
    for _k, _m in sys.modules.items():
        if _k.endswith("csglide_chain") and hasattr(_m, "ws_broadcast"):
            return _m
    return None


def _broadcast(kind, name, **extra):
    """借 csglide_chain.ws_broadcast 发 WS 变更通知。ws_broadcast 是**同步**
    函数（内部 PromptServer.send_sync 同步发 JSON，且自带异常静默），路由
    协程在 await run_in_executor 之后已回到事件循环线程，直接调即可。"""
    try:
        _cc = _chain_module()
        if _cc:
            _cc.ws_broadcast(kind, name, **extra)
    except Exception:
        pass


def register_routes():
    """在 PromptServer 就绪后由 csglide_chain 调用（不能在模块 import 顶部注册：
    本模块被 csglide_chain 顶部 import 时，PromptServer.instance 可能尚未创建）。"""
    import asyncio
    from server import PromptServer

    def _store_dir(request, body=None, qname=None):
        name = qname
        if name is None and body is not None:
            name = body.get("name")
        if not name:
            name = request.query.get("name") if qname is None else qname
        return name, chain_dir(name)

    @PromptServer.instance.routes.get("/cglide/store/health")
    async def _route_store_health(request):
        name = request.query.get("name") or "未命名"
        loop = asyncio.get_event_loop()
        h = await loop.run_in_executor(None, health, chain_dir(name))
        h["name"] = name
        return _json_response(h)

    @PromptServer.instance.routes.get("/cglide/store/project")
    async def _route_store_project(request):
        name = request.query.get("name") or "未命名"
        import asyncio
        loop = asyncio.get_event_loop()

        def _build():
            # clips 走 chain.summary 保持同构（含实时磁盘探测字段）；
            # 包名含连字符，相对导入失败时从 sys.modules 兜底。
            cc = _chain_module()
            loader = getattr(cc, "summary", None)
            d = chain_dir(name)
            if ensure_db(d) != "db":
                # 老工程：不建库，回一个 json 后端标记，前端走老路径
                return {"name": name, "backend": "json"}
            return project_blob(d, name, loader)

        return _json_response(await loop.run_in_executor(None, _build))

    @PromptServer.instance.routes.post("/cglide/store/rack")
    async def _route_store_rack(request):
        import asyncio
        try:
            body = await request.json()
        except Exception:
            return _json_response({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        cid = body.get("cid") or body.get("id")
        if not cid:
            return _json_response({"error": "缺少段落 id"}, 400)
        d = chain_dir(name)
        if ensure_db(d) != "db":
            return _json_response({"error": "老工程（JSON 档案）不支持状态库写入"}, 409)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, save_rack, d, cid,
            {"slots": body.get("slots"), "fps": body.get("fps"),
             "audio_out": body.get("audio_out")})
        await loop.run_in_executor(
            None, log_event, d, "rack_save", cid,
            {"images": len((body.get("slots") or {}).get("images") or []),
             "videos": len((body.get("slots") or {}).get("videos") or []),
             "audios": len((body.get("slots") or {}).get("audios") or [])},
            "rack")
        _broadcast("rack_saved", name, clip=cid)
        return _json_response({"ok": True})

    @PromptServer.instance.routes.post("/cglide/store/desk")
    async def _route_store_desk(request):
        import asyncio
        try:
            body = await request.json()
        except Exception:
            return _json_response({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        d = chain_dir(name)
        if ensure_db(d) != "db":
            return _json_response({"error": "老工程（JSON 档案）不支持状态库写入"}, 409)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, save_desk, d, body.get("desk") or {})
        await loop.run_in_executor(
            None, log_event, d, "desk_save", None, {}, "desk")
        return _json_response({"ok": True})

    @PromptServer.instance.routes.post("/cglide/store/event")
    async def _route_store_event(request):
        import asyncio
        try:
            body = await request.json()
        except Exception:
            return _json_response({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        d = chain_dir(name)
        if not has_db(d):
            return _json_response({"ok": True, "seq": None})   # 老工程静默跳过
        loop = asyncio.get_event_loop()
        seq = await loop.run_in_executor(
            None, log_event, d, body.get("action") or "event",
            body.get("cid"), body.get("payload") or {},
            body.get("actor") or "frontend")
        return _json_response({"ok": True, "seq": seq})

    @PromptServer.instance.routes.get("/cglide/store/events")
    async def _route_store_events(request):
        import asyncio
        name = request.query.get("name") or "未命名"
        d = chain_dir(name)
        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(
            None, list_events, d,
            int(request.query.get("limit") or 100),
            request.query.get("cid") or None,
            request.query.get("action") or None)
        return _json_response({"events": rows})

    @PromptServer.instance.routes.post("/cglide/store/snapshot")
    async def _route_store_snapshot_add(request):
        import asyncio
        try:
            body = await request.json()
        except Exception:
            return _json_response({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        d = chain_dir(name)
        if ensure_db(d) != "db":
            return _json_response({"error": "老工程（JSON 档案）不支持状态库写入"}, 409)
        loop = asyncio.get_event_loop()
        sid = await loop.run_in_executor(
            None, add_snapshot, d, body.get("label") or "",
            body.get("shots") or [], body.get("desk"),
            body.get("cur_cid"), body.get("idx"),
            body.get("source") or "manual")
        return _json_response({"ok": True, "id": sid})

    @PromptServer.instance.routes.get("/cglide/store/snapshots")
    async def _route_store_snapshot_list(request):
        import asyncio
        name = request.query.get("name") or "未命名"
        d = chain_dir(name)
        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(
            None, list_snapshots, d, int(request.query.get("limit") or 200))
        return _json_response({"snapshots": rows})

    @PromptServer.instance.routes.get("/cglide/store/snapshot")
    async def _route_store_snapshot_get(request):
        import asyncio
        name = request.query.get("name") or "未命名"
        sid = request.query.get("id")
        d = chain_dir(name)
        loop = asyncio.get_event_loop()
        out = await loop.run_in_executor(None, get_snapshot, d, sid)
        if out is None:
            return _json_response({"error": "没有这个还原点"}, 404)
        return _json_response(out)

    @PromptServer.instance.routes.delete("/cglide/store/snapshot")
    async def _route_store_snapshot_del(request):
        import asyncio
        name = request.query.get("name") or "未命名"
        sid = request.query.get("id")
        d = chain_dir(name)
        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(None, delete_snapshot, d, sid)
        return _json_response({"ok": bool(ok)})
