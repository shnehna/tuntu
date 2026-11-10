#!/usr/bin/env python3
"""吞吐 (tuntu) — 本机 Claude Code token 用量看板。

只用 Python 标准库。读取本机的 Claude Code 会话日志，在 localhost 上
打开一个看板页面。数据不会离开这台电脑。

    tuntu            # 扫描并打开看板
    tuntu --demo     # 用虚构数据演示（适合截图）

从源码目录运行时用 python3 -m tuntu。
"""

import argparse
import collections
import datetime as dt
import glob
import json
import os
import random
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__ as VERSION

HERE = os.path.dirname(os.path.abspath(__file__))
CTX_BUCKETS = 96          # 会话详情图最多画多少根柱子
SCRATCH_KEY = "\x00scratch"

# 一条请求记录的字段下标
T, CWD, MODEL, SIDE, IN, OUT, CW, CR, H1, EFFORT = range(10)


# ---------------------------------------------------------------- 读取日志

def find_roots(custom=None):
    """返回存在的日志根目录列表。"""
    if custom:
        cands = [custom]
    else:
        cands = []
        env = os.environ.get("CLAUDE_CONFIG_DIR")
        if env:
            cands += [os.path.join(p.strip(), "projects") for p in env.split(",") if p.strip()]
        cands += ["~/.claude/projects", "~/.config/claude/projects"]
    roots = []
    for c in cands:
        p = os.path.abspath(os.path.expanduser(c))
        if os.path.isdir(p) and p not in roots:
            roots.append(p)
    return roots


def tidy_home(path):
    """把家目录换成 ~，页面上不显示用户名。"""
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def project_key(cwd):
    """把工作目录归并成项目：去掉 worktree 后缀，临时会话合并成一项。"""
    if not cwd:
        return "?"
    p = cwd.replace("\\", "/").rstrip("/")
    if "/scratch-workspaces/" in p:
        return SCRATCH_KEY
    for marker in ("/.claude/worktrees/", "/.claude-worktrees/"):
        i = p.find(marker)
        if i > 0:
            p = p[:i]
    return p or "/"


def encode_path(path):
    """Claude Code 给项目目录起名的方式：非字母数字的字符都换成 -。"""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def resolve_dirs(cwd_seen):
    """把日志目录名还原成项目路径。

    会话中途 cd 到子目录时 cwd 会变，所以项目按日志目录归，而不是按每条
    记录的 cwd 归；目录名里的 - 有歧义，用记录里出现过的 cwd 反查。
    """
    resolved = {}
    for dirname, cwds in cwd_seen.items():
        for c in cwds:
            if encode_path(c) == dirname:
                resolved[dirname] = c
                break
    by_enc = {encode_path(project_key(c)): project_key(c) for c in resolved.values()}
    home_enc = encode_path(os.path.expanduser("~"))
    out = {}
    for dirname in cwd_seen:
        if dirname in resolved:
            out[dirname] = project_key(resolved[dirname])
            continue
        parent = re.split(r"--claude-worktrees-|-claude-worktrees-", dirname)[0]
        if parent in by_enc:
            out[dirname] = by_enc[parent]
        elif "-scratch-workspaces-" in dirname:
            out[dirname] = SCRATCH_KEY
        else:
            name = parent[len(home_enc) + 1:] if parent.startswith(home_enc + "-") else parent
            out[dirname] = "/" + (name or parent)
    return out


def parse_file(path, force_side, recs, tools, proj, cwds):
    """解析一个 jsonl 文件，把请求写进 recs，把工具调用写进 tools。"""
    try:
        fh = open(path, "r", encoding="utf-8", errors="ignore")
    except OSError:
        return
    with fh:
        for line in fh:
            if '"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = d.get("message")
            if not isinstance(m, dict):
                continue
            u = m.get("usage")
            if not isinstance(u, dict):
                continue
            model = m.get("model") or "unknown"
            if model == "<synthetic>":
                continue
            ts = d.get("timestamp")
            if not isinstance(ts, str):
                continue
            try:
                t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
            except ValueError:
                continue
            cwd = d.get("cwd")
            if isinstance(cwd, str) and len(cwds) < 500:
                cwds.add(cwd)
            key = (m.get("id") or d.get("uuid"), d.get("requestId"))
            cc = u.get("cache_creation")
            h1 = cc.get("ephemeral_1h_input_tokens") if isinstance(cc, dict) else 0
            rec = [
                t,
                proj,
                model,
                bool(force_side or d.get("isSidechain")),
                u.get("input_tokens") or 0,
                u.get("output_tokens") or 0,
                u.get("cache_creation_input_tokens") or 0,
                u.get("cache_read_input_tokens") or 0,
                h1 or 0,
                d.get("perTurnEffort") or d.get("effort") or None,
            ]
            old = recs.get(key)
            if old is not None:
                # 同一条消息会按内容块写多行，用量取最大的那次
                for i in (IN, OUT, CW, CR, H1):
                    rec[i] = max(rec[i], old[i])
                rec[T] = min(rec[T], old[T])
            recs[key] = rec
            content = m.get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("id"):
                        name = str(c.get("name") or "?")
                        if name.startswith("mcp__"):
                            name = name.split("__")[-1] or name
                        tools[c["id"]] = (t.strftime("%Y-%m-%d"), name)


def collect(roots):
    """扫描所有日志，按会话归组。返回 (sessions, 文件数, 目录名到项目的映射)。"""
    sessions = {}
    cwd_seen = {}
    nfiles = 0
    for root in roots:
        for path in sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)):
            rel = os.path.relpath(path, root).split(os.sep)
            if len(rel) < 2:
                continue
            nfiles += 1
            is_main = len(rel) == 2
            sid = rel[1][:-len(".jsonl")] if is_main else rel[1]
            s = sessions.setdefault(sid, {"recs": {}, "tools": {}})
            parse_file(path, not is_main, s["recs"], s["tools"], rel[0], cwd_seen.setdefault(rel[0], set()))
    return sessions, nfiles, resolve_dirs(cwd_seen)


# ---------------------------------------------------------------- 聚合

def aggregate(sessions, meta, dirmap=None):
    """把会话记录聚合成页面要用的数据。返回 (data, details)。"""
    seen = set()
    tool_seen = set()
    buckets = collections.defaultdict(lambda: [0, 0, 0, 0, 0, 0])
    tools = collections.Counter()
    proj_ids, model_ids = {}, {}
    sess_out, details = [], {}

    dirmap = dirmap or {}

    def pid(key):
        key = dirmap.get(key, key)
        return proj_ids.setdefault(key, len(proj_ids))

    def mid(key):
        return model_ids.setdefault(key, len(model_ids))

    for sid in sorted(sessions):
        s = sessions[sid]
        recs = s["recs"]
        if not recs:
            continue
        tok = [0, 0, 0, 0]
        sub_req = sub_tok = 0
        by_proj, by_model = collections.Counter(), collections.Counter()
        main = []
        for key, r in recs.items():
            total = r[IN] + r[OUT] + r[CW] + r[CR]
            for i, f in enumerate((IN, OUT, CW, CR)):
                tok[i] += r[f]
            by_proj[r[CWD]] += total
            by_model[r[MODEL]] += total
            if r[SIDE]:
                sub_req += 1
                sub_tok += total
            else:
                main.append(r)
            # 续接或分叉的会话会带着前半段记录；总量只算一次
            if key in seen:
                continue
            seen.add(key)
            b = buckets[(r[T].strftime("%Y-%m-%d"), r[T].hour, pid(r[CWD]), mid(r[MODEL]),
                         1 if r[SIDE] else 0, r[EFFORT])]
            b[0] += r[IN]
            b[1] += r[OUT]
            b[2] += r[CW]
            b[3] += r[CR]
            b[4] += r[H1]
            b[5] += 1
        for tid, (date, name) in s["tools"].items():
            if tid in tool_seen:
                continue
            tool_seen.add(tid)
            tools[(date, name)] += 1

        main.sort(key=lambda r: r[T])
        ctx = [r[IN] + r[CW] + r[CR] for r in main]
        drops = [i for i in range(1, len(ctx)) if ctx[i - 1] > 20000 and ctx[i] < ctx[i - 1] * 0.5]
        times = [r[T] for r in recs.values()]
        sess_out.append({
            "id": sid,
            "p": pid(by_proj.most_common(1)[0][0]),
            "m": mid(by_model.most_common(1)[0][0]),
            "start": min(times).strftime("%Y-%m-%dT%H:%M"),
            "end": max(times).strftime("%Y-%m-%dT%H:%M"),
            "tok": tok,
            "req": len(recs),
            "subReq": sub_req,
            "subTok": sub_tok,
            "peak": max(ctx) if ctx else 0,
            "drops": len(drops),
        })
        details[sid] = downsample(ctx, drops)

    data = {
        "meta": meta,
        "projects": project_names(proj_ids),
        "models": [k for k, _ in sorted(model_ids.items(), key=lambda kv: kv[1])],
        "buckets": sorted([list(k) + v for k, v in buckets.items()], key=lambda b: (b[0], b[1])),
        "tools": sorted([[d, n, c] for (d, n), c in tools.items()]),
        "sessions": sess_out,
    }
    data["meta"]["sessions"] = len(sess_out)
    return data, details


def downsample(ctx, drops):
    """把逐次请求的上下文长度压成最多 CTX_BUCKETS 根柱子（取每段最大值）。"""
    n = len(ctx)
    if n == 0:
        return {"n": 0, "per": 1, "ctx": [], "drop": []}
    per = max(1, -(-n // CTX_BUCKETS))
    dropset = set(drops)
    out, flags = [], []
    for i in range(0, n, per):
        out.append(max(ctx[i:i + per]))
        flags.append(1 if any(j in dropset for j in range(i, min(n, i + per))) else 0)
    return {"n": n, "per": per, "ctx": out, "drop": flags}


def project_names(proj_ids):
    """给每个项目起显示名：只用目录名，重名时带上上一级目录。"""
    keys = [k for k, _ in sorted(proj_ids.items(), key=lambda kv: kv[1])]
    base = collections.Counter(os.path.basename(k) for k in keys if k != SCRATCH_KEY)
    home = os.path.expanduser("~").replace("\\", "/")
    names = []
    for k in keys:
        if k == SCRATCH_KEY:
            names.append("临时会话")
        elif k == home:
            names.append("~")
        else:
            b = os.path.basename(k) or k
            if base[b] > 1:
                b = os.path.basename(os.path.dirname(k)) + "/" + b
            names.append(b)
    return names


# ---------------------------------------------------------------- 演示数据

def demo_sessions(seed=7, days=45):
    """生成一份虚构的用量记录，结构和真实日志解析后一样。"""
    rnd = random.Random(seed)
    now = dt.datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
    projects = [("/demo/moon-base", 55), ("/demo/noodle-shop", 25), ("/demo/side-quest", 14), ("/demo/dotfiles", 6)]
    models = [("claude-opus-5-5", 80), ("claude-sonnet-5-5", 16), ("claude-haiku-5-5", 4)]
    efforts = [("high", 50), ("medium", 40), ("low", 10)]
    tool_names = [("Bash", 50), ("Read", 18), ("Edit", 14), ("Grep", 8), ("Write", 5), ("Glob", 3), ("WebFetch", 2)]
    hour_w = [3, 2, 1, 0, 0, 0, 0, 1, 3, 6, 8, 8, 5, 6, 9, 10, 9, 7, 5, 6, 9, 10, 8, 5]

    def pick(pairs):
        return rnd.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]

    sessions = {}
    n = 0
    for back in range(days - 1, -1, -1):
        if back and rnd.random() < 0.12:
            continue
        day = (now - dt.timedelta(days=back)).replace(hour=0)
        for _ in range(rnd.randint(1, 4)):
            start = day + dt.timedelta(hours=rnd.choices(range(24), weights=hour_w)[0], minutes=rnd.randint(0, 59))
            if start > now:
                continue
            sid = "demo-%04d" % len(sessions)
            cwd, model = pick(projects), pick(models)
            recs, tools = {}, {}
            t, ctx = start, rnd.randint(15000, 30000)
            for i in range(int(rnd.lognormvariate(4.3, 0.9)) + 5):
                t += dt.timedelta(seconds=rnd.randint(15, 140))
                if t > now:
                    break
                n += 1
                side = rnd.random() < 0.07
                grow = rnd.randint(300, 4000)
                if side:
                    cr, cw = rnd.randint(8000, 30000), rnd.randint(500, 6000)
                else:
                    if ctx > rnd.randint(150000, 190000):
                        ctx = rnd.randint(25000, 40000)      # 自动压缩
                        cr, cw = 0, ctx
                    else:
                        cr, cw = ctx, grow
                    ctx += grow
                recs[("demo", n)] = [t, cwd, model, side, rnd.randint(1, 40), rnd.randint(40, 1800), cw, cr,
                                     cw if rnd.random() < 0.85 else 0, pick(efforts)]
                for k in range(rnd.choice([0, 1, 1, 1, 2])):
                    tools[("demo", n, k)] = (t.strftime("%Y-%m-%d"), pick(tool_names))
            if recs:
                sessions[sid] = {"recs": recs, "tools": tools}
    return sessions


# ---------------------------------------------------------------- 服务

class App:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.data = None
        self.details = {}

    def scan(self):
        with self.lock:
            now = dt.datetime.now().astimezone()
            meta = {"version": VERSION, "generated": now.strftime("%Y-%m-%dT%H:%M"), "demo": bool(self.args.demo)}
            if self.args.demo:
                sessions, nfiles, dirmap = demo_sessions(), 0, None
                meta["root"] = "演示数据"
            else:
                roots = find_roots(self.args.dir)
                sessions, nfiles, dirmap = collect(roots)
                meta["root"] = "、".join(tidy_home(r) for r in roots) or "~/.claude/projects"
            meta["files"] = nfiles
            self.data, self.details = aggregate(sessions, meta, dirmap)
            return self.data


def make_handler(app, page):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tuntu/" + VERSION

        def log_message(self, *a):
            pass

        def send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy", page["csp"])
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, obj, code=200):
            self.send(code, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                      "application/json; charset=utf-8")

        def do_GET(self):
            # 只接受发往本机的请求，挡掉 DNS 重绑定
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in ("127.0.0.1", "localhost"):
                return self.send(403, b"forbidden", "text/plain")
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            if url.path in ("/", "/index.html"):
                return self.send(200, page["html"], "text/html; charset=utf-8")
            if url.path == "/api/data":
                if "rescan" in qs or app.data is None:
                    app.scan()
                return self.send_json(app.data)
            if url.path == "/api/session":
                detail = app.details.get((qs.get("id") or [""])[0])
                if detail is None:
                    return self.send_json({"error": "not found"}, 404)
                return self.send_json(detail)
            return self.send(404, b"not found", "text/plain")

    return Handler


def load_page(web_fonts):
    with open(os.path.join(HERE, "web", "index.html"), "r", encoding="utf-8") as fh:
        html = fh.read()
    csp = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:"
    fonts = ""
    if web_fonts:
        fonts = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
                 'family=Bricolage+Grotesque:opsz,wght@12..96,500..800&family=ZCOOL+QingKe+HuangYou&display=swap">')
        csp = csp.replace("style-src 'unsafe-inline'", "style-src 'unsafe-inline' https://fonts.googleapis.com")
        csp += "; font-src https://fonts.gstatic.com"
    return {"html": html.replace("<!--FONTS-->", fonts).encode("utf-8"), "csp": csp}


def main():
    ap = argparse.ArgumentParser(prog="tuntu", description="吞吐 (tuntu)：本机 Claude Code token 用量看板")
    ap.add_argument("--port", type=int, default=8787, help="端口，默认 8787（被占用时自动往后找）")
    ap.add_argument("--dir", help="日志目录，默认 ~/.claude/projects")
    ap.add_argument("--demo", action="store_true", help="用虚构数据演示，不读取任何日志")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--web-fonts", action="store_true", help="从 Google Fonts 加载展示字体（会产生外网请求）")
    ap.add_argument("--json", action="store_true", help="把聚合结果打印成 JSON 后退出")
    ap.add_argument("--version", action="version", version="tuntu " + VERSION)
    args = ap.parse_args()

    app = App(args)
    data = app.scan()
    if args.json:
        json.dump(data, sys.stdout, ensure_ascii=False)
        return
    if not args.demo and not data["buckets"]:
        print("没有找到用量记录（%s）。可以用 --dir 指定目录，或用 --demo 看演示。" % data["meta"]["root"])

    handler = make_handler(app, load_page(args.web_fonts))
    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit("端口 %d 到 %d 都被占用了，请用 --port 换一个。" % (args.port, args.port + 19))
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    print("吞吐已启动：%s  （Ctrl+C 退出）" % url)
    if not args.no_open:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n再见。")


if __name__ == "__main__":
    main()
