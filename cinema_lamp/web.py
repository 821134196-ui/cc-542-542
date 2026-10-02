"""本地 Web 界面：影院黑底、白字、金色状态标记。仅依赖标准库。"""
import html
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .service import (
    Service, DomainError, MaintenanceConflict, SpareMismatch,
    OverThresholdNeedsApproval, TimerResetFailed, BrightnessInsufficient,
    ReleaseNotAllowed, SUPERVISOR_ROLE,
)

GOLD = "#d4af37"
BG = "#0b0b0d"
PANEL = "#17171c"
LINE = "#2c2c33"
WHITE = "#f2f2f2"
MUTED = "#9a9aa2"
RED = "#e06a6a"
GREEN = "#7fd08a"

svc: Service = None


def esc(v):
    return html.escape("" if v is None else str(v), quote=True)


def layout(title, body):
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · 影城灯泡管理</title>
<style>
 body {{ background:{BG}; color:{WHITE}; font:15px/1.6 "PingFang SC","Microsoft YaHei",sans-serif; margin:0; }}
 header {{ background:#000; border-bottom:1px solid {LINE}; padding:14px 26px; display:flex;
          align-items:baseline; gap:24px; flex-wrap:wrap; }}
 header h1 {{ color:{GOLD}; font-size:18px; margin:0; letter-spacing:2px; }}
 nav a {{ color:{WHITE}; text-decoration:none; margin-right:16px; font-size:14px; }}
 nav a:hover {{ color:{GOLD}; }}
 main {{ padding:22px 26px; max-width:1180px; margin:0 auto; }}
 .card {{ background:{PANEL}; border:1px solid {LINE}; border-radius:10px; padding:16px 18px; margin-bottom:18px; }}
 h2 {{ font-size:16px; color:{GOLD}; margin:0 0 12px; }}
 table {{ border-collapse:collapse; width:100%; font-size:14px; }}
 th,td {{ text-align:left; padding:9px 10px; border-bottom:1px solid {LINE}; vertical-align:top; }}
 th {{ color:{MUTED}; font-weight:600; }}
 tr:last-child td {{ border-bottom:none; }}
 .pill {{ display:inline-block; padding:1px 10px; border-radius:99px; font-size:12px;
         border:1px solid {LINE}; color:{WHITE}; }}
 .p-ok {{ color:{GREEN}; border-color:{GREEN}; }}
 .p-warn {{ color:{GOLD}; border-color:{GOLD}; }}
 .p-bad {{ color:{RED}; border-color:{RED}; }}
 .muted {{ color:{MUTED}; }}
 .gold {{ color:{GOLD}; }}
 form.inline {{ display:inline; }}
 input,select,textarea,button {{ background:#0e0e12; color:{WHITE}; border:1px solid {LINE};
   border-radius:6px; padding:6px 9px; font-size:13px; font-family:inherit; }}
 button {{ cursor:pointer; }}
 button.gold {{ border-color:{GOLD}; color:{GOLD}; }}
 button.gold:hover {{ background:{GOLD}; color:#000; }}
 button.danger {{ border-color:{RED}; color:{RED}; }}
 button.danger:hover {{ background:{RED}; color:#000; }}
 .msg {{ padding:10px 14px; border-radius:8px; margin-bottom:14px; font-size:14px; }}
 .msg.err {{ border:1px solid {RED}; color:{RED}; }}
 .msg.ok {{ border:1px solid {GREEN}; color:{GREEN}; }}
 .pair {{ display:flex; gap:18px; align-items:center; flex-wrap:wrap; }}
 .pair figure {{ margin:0; text-align:center; }}
 .pair img {{ border:1px solid {LINE}; border-radius:6px; width:240px; }}
 .step {{ display:flex; gap:10px; align-items:flex-start; padding:8px 0; }}
 .step .no {{ color:{GOLD}; font-weight:700; min-width:130px; }}
 .step.done .nm {{ color:{GREEN}; }}
 .step.pending .nm {{ color:{MUTED}; }}
 .bar {{ height:6px; background:#26262d; border-radius:3px; width:120px; display:inline-block; vertical-align:middle; }}
 .bar > i {{ display:block; height:100%; border-radius:3px; background:{GREEN}; }}
 a {{ color:{GOLD}; }}
 .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
 @media(max-width:820px){{ .grid2{{grid-template-columns:1fr}} }}
</style></head><body>
<header><h1>🎞 影城放映机灯泡管理</h1>
<nav><a href="/">影厅概览</a><a href="/ledger">设备台账</a><a href="/shows">场次影响</a>
<a href="/tickets">换灯工单</a><a href="/incidents">异常处置</a><a href="/approvals">放行审批</a></nav>
</header><main>{body}</main></body></html>"""


def state_pill(state):
    cls = "pill"
    if state in ("正常",):
        cls += " p-ok"
    elif state in ("临界",) or state.startswith("超阈值"):
        cls += " p-warn"
    elif state in ("熄灯", "无设备"):
        cls += " p-bad"
    return f'<span class="{cls}">{esc(state)}</span>'


def life_bar(fc):
    ratio = min(1.0, max(0.0, fc["used_ratio"]))
    color = GREEN if ratio < config.CRITICAL_RATIO else GOLD if not fc["over_threshold"] else RED
    return (f'<span class="bar"><i style="width:{ratio*100:.0f}%;background:{color}"></i></span>'
            f' <b style="color:{color}">{fc["remaining_hours"]:.1f}h</b> 剩余')


def page_index(q, msg):
    rows = svc.hall_status()
    trs = []
    for it in rows:
        p = it["projector"]
        l, fc = it["lamp"], it["forecast"]
        ns = it["next_show"]
        if ns:
            cover = ('<span class="pill p-ok">可覆盖</span>' if it["can_cover_next"]
                     else '<span class="pill p-bad">无法覆盖整场</span>')
            next_s = f'{esc(ns["title"])}<br><span class="muted">{esc(ns["start_ts"])} 开 · {ns["duration_min"]:g} 分钟</span>'
        else:
            cover = '<span class="muted">—</span>'
            next_s = '<span class="muted">暂无排片</span>'
        if l and fc:
            life = f'{life_bar(fc)}<br><span class="muted">{l["lit_hours"]:.0f}h / 阈值 {l["threshold_hours"]:.0f}h</span>'
            lamp_txt = f'{esc(l["serial_no"])} <span class="muted">({esc(l["model"])})</span>'
        else:
            life = '<span class="muted">—</span>'
            lamp_txt = '<span class="muted">无在用灯泡</span>'
        pid = p["id"] if p else ""
        action = (f'<form class="inline" method="post" action="/open-ticket">'
                  f'<input type="hidden" name="projector_id" value="{pid}">'
                  f'备件序列号 <input name="spare" size="13" placeholder="SP-…">'
                  f' 技术员 <input name="tech" size="8" placeholder="姓名">'
                  f' <button class="gold" type="submit">开换灯工单</button></form>') if p else ""
        trs.append(f"""<tr>
 <td><b>{esc(it['hall']['name'])}</b><br>{state_pill(it['state'])}
 {f'<br><span class="muted">主管批准至 {esc(it["approval"]["usable_until"])}</span>' if it.get("approval") else ""}</td>
 <td>{esc(p['model']) if p else '—'}<br><span class="muted">要求灯泡 {esc(p['required_lamp_model']) if p else ''} · ≥{p['required_lumens']:g}lm</span></td>
 <td>{lamp_txt}</td><td>{life}</td><td>{next_s}</td><td>{cover}</td><td>{action}</td></tr>""")
    body = (msg
            + '<div class="card"><h2>影厅列表 · 下一场与剩余寿命并列</h2>'
            + '<table><tr><th>影厅/状态</th><th>放映机</th><th>在用灯泡</th><th>剩余寿命</th>'
            '<th>下一场开始时间</th><th>覆盖判断</th><th>维修操作</th></tr>'
            + "".join(trs) + '</table></div>')
    return layout("影厅概览", body)


def page_ledger(msg):
    rows = svc.ledger()
    trs = []
    for r in rows:
        if r["serial_no"]:
            trs.append(f"<tr><td>{esc(r['hall_name'])}</td><td>{esc(r['projector_model'])}</td>"
                       f"<td>{esc(r['required_lamp_model'])} / ≥{r['required_lumens']:g}lm</td>"
                       f"<td>{esc(r['serial_no'])}</td><td>{esc(r['lamp_model'])}</td>"
                       f"<td>{r['lit_hours']:.1f}</td><td>{r['threshold_hours']:.0f}</td>"
                       f'<td><span class="pill p-ok">在用</span></td></tr>')
    body = msg + ('<div class="card"><h2>设备台账</h2><table>'
                  '<tr><th>影厅</th><th>放映机型号</th><th>要求灯泡/亮度</th><th>灯泡序列号</th>'
                  '<th>灯泡型号</th><th>累计点亮(h)</th><th>更换阈值(h)</th><th>状态</th></tr>'
                  + "".join(trs) + "</table></div>")
    return layout("设备台账", body)


def page_shows(msg):
    trs = []
    for s in svc.list_shows():
        st = {"scheduled": '<span class="pill p-ok">已排</span>',
              "delayed": '<span class="pill p-bad">延误</span>',
              "cancelled": '<span class="pill">取消</span>',
              "screened": '<span class="pill">已放映</span>'}[s["status"]]
        cover = ('<span class="pill p-ok">寿命可覆盖</span>' if s["can_cover"]
                 else f'<span class="pill p-bad">{esc(s["cover_reason"])}</span>')
        delay_form = (f'<form class="inline" method="post" action="/delay-show">'
                      f'<input type="hidden" name="show_id" value="{s["id"]}">'
                      f'延误 <input name="delay_min" size="4" value="30"> 分 '
                      f'<button type="submit">登记延误</button></form>'
                      if s["status"] == "scheduled" else f'已延误 {s["delay_min"]:g} 分钟')
        trs.append(f"<tr><td>{esc(s['hall_name'])}</td><td>{esc(s['title'])}</td>"
                   f"<td>{esc(s['start_ts'])}</td><td>{s['duration_min']:g}</td>"
                   f"<td>{st}</td><td>{cover}</td><td>{delay_form}</td></tr>")
    body = msg + ('<div class="card"><h2>场次影响</h2><table>'
                  '<tr><th>影厅</th><th>片名</th><th>开始时间</th><th>时长(分)</th>'
                  '<th>状态</th><th>灯泡覆盖</th><th>延误处置</th></tr>'
                  + "".join(trs) + "</table></div>")
    return layout("场次影响", body)


def page_tickets(msg):
    trs = []
    for t in svc.list_tickets():
        st = {"open": "pill", "in_progress": "pill p-warn", "completed": "pill p-ok",
              "cancelled": "pill p-bad"}[t["status"]]
        label = {"open": "待处理", "in_progress": "维修中", "completed": "已完工",
                 "cancelled": "已取消"}[t["status"]]
        trs.append(f'<tr><td><a href="/ticket?id={t["id"]}">#{t["id"]}</a></td>'
                   f"<td>{esc(t['technician'])}</td><td>{esc(t['created_at'])}</td>"
                   f'<td><span class="{st}">{label}</span></td></tr>')
    body = msg + ('<div class="card"><h2>换灯工单</h2><table>'
                  '<tr><th>工单号</th><th>技术员</th><th>创建时间</th><th>状态</th></tr>'
                  + "".join(trs) + "</table></div>")
    return layout("换灯工单", body)


def page_ticket(tid, msg):
    t = svc.get_ticket(tid)
    if not t:
        return page_tickets('<div class="msg err">工单不存在</div>')
    steps = ""
    for st in t["steps"]:
        done = st["status"] == "done"
        meta = next((x for x in config.REPLACE_STEPS if x[0] == st["step_no"]), None)
        _, name, desc = meta
        cls = "step done" if done else "step pending"
        mark = "✓ " + esc(st["done_at"] or "") if done else "○ 待完成"
        btn = ""
        if t["status"] == "in_progress" and not done:
            btn = (f'<form class="inline" method="post" action="/step">'
                   f'<input type="hidden" name="ticket_id" value="{tid}">'
                   f'<input type="hidden" name="step_no" value="{st["step_no"]}">'
                   f'技术员 <input name="tech" size="8" value="{esc(t["technician"])}"> '
                   f'<button class="gold" type="submit">完成步骤 {st["step_no"]}</button></form>')
        steps += (f'<div class="{cls}"><span class="no">步骤 {st["step_no"]} · {esc(name)}</span>'
                  f'<span class="nm">{esc(desc)}<br><span class="muted">{mark} '
                  f'{esc(st["note"] or "")}</span></span><span>{btn}</span></div>')

    # 试映前后亮度成对展示
    pairs = ""
    for tr in t["trials"]:
        res = ('<span class="pill p-ok">合格</span>' if tr["passed"]
               else '<span class="pill p-bad">亮度不足</span>')
        pairs += f"""<div class="pair">
 <figure><figcaption>更换前</figcaption><img src="/photo?name={esc(os.path.basename(tr['before_photo']))}">
  <div class="muted">{tr['before_lumens']:g} lm</div></figure>
 <div class="gold">→</div>
 <figure><figcaption>试映后</figcaption><img src="/photo?name={esc(os.path.basename(tr['after_photo']))}">
  <div class="muted">{tr['after_lumens']:g} lm</div></figure>
 <div>要求 ≥{tr['required_lumens']:g} lm<br>{res}<br><span class="muted">{esc(tr['tested_by'])} · {esc(tr['tested_at'])}</span></div>
</div><hr style="border-color:{LINE}">"""

    actions = ""
    if t["status"] == "in_progress":
        actions = f"""<form class="inline" method="post" action="/complete-ticket">
 <input type="hidden" name="ticket_id" value="{tid}">
 完工技术员 <input name="tech" size="8" value="{esc(t['technician'])}">
 <button class="gold" type="submit">五步完成 · 完工并释放窗口</button></form>
 <form class="inline" method="post" action="/cancel-ticket">
 <input type="hidden" name="ticket_id" value="{tid}">
 技术员 <input name="tech" size="8" value="{esc(t['technician'])}">
 原因 <input name="note" size="18" placeholder="取消原因">
 <button class="danger" type="submit">中途取消</button></form>"""

    release_block = ""
    if t["status"] == "completed":
        if t["release"]:
            release_block = (f'<p>✅ 已最终放行：<b>{esc(t["release"]["signer"])}</b>'
                             f'（{esc(t["release"]["signer_role"])}）于 {esc(t["release"]["signed_at"])} 签署</p>')
        else:
            release_block = f"""<div class="msg">换灯人 <b>{esc(t['technician'])}</b> 不得自签；
 最终放行须由 <b class="gold">{SUPERVISOR_ROLE}</b> 且非换灯人签署。</div>
 <form method="post" action="/release">
 <input type="hidden" name="ticket_id" value="{tid}">
 放行人 <input name="signer" size="8" placeholder="主管姓名">
 角色 <input name="role" size="10" value="{SUPERVISOR_ROLE}">
 <button class="gold" type="submit">签署最终放行</button></form>"""
    elif t["status"] == "cancelled":
        release_block = f'<p class="muted">工单已中途取消：{esc(t["cancel_note"] or "")}</p>'

    body = msg + f"""<div class="card"><h2>换灯工单 #{tid} · {esc(t['technician'])} ·
     <span class="muted" style="font-size:13px">{esc(t['created_at'])}</span></h2>
     {steps}</div>
     <div class="card"><h2>试映记录 · 前后亮度成对</h2>{pairs or '<p class="muted">尚无试映记录</p>'}</div>
     <div class="card"><h2>完工与放行</h2>{actions}{release_block}</div>
     <p><a href="/tickets">← 返回工单列表</a></p>"""
    return layout(f"工单 #{tid}", body)


def page_incidents(msg):
    trs = []
    for i in svc.list_incidents():
        kind = f'<span class="pill p-bad">{esc(config.INCIDENT_LABELS.get(i["kind"], i["kind"]))}</span>'
        status = ('<span class="pill p-ok">已处置</span>' if i["resolution"]
                  else '<span class="pill p-warn">待处置</span>')
        form = ""
        if not i["resolution"]:
            form = (f'<form method="post" action="/resolve-incident" class="inline">'
                    f'<input type="hidden" name="incident_id" value="{i["id"]}">'
                    f'处置 <input name="resolution" size="20" placeholder="处置说明"> '
                    f'<input name="who" size="8" placeholder="处置人">'
                    f' <button type="submit">提交</button></form>')
        trs.append(f"<tr><td>{i['id']}</td><td>{kind}</td><td>{esc(i['detail'])}</td>"
                   f"<td>{status}<br><span class='muted'>{esc(i['resolution'] or '')}</span></td>"
                   f"<td>{esc(i['handled_by'] or '')} {esc(i['handled_at'] or '')}</td>"
                   f"<td>{esc(i['created_at'])}</td><td>{form}</td></tr>")
    body = msg + ('<div class="card"><h2>异常处置（熄灯 / 重置失败 / 备件不符 / 取消 / 亮度不足 / 延误）</h2>'
                  '<table><tr><th>#</th><th>类型</th><th>情况</th><th>处置结果</th>'
                  '<th>处置人/时间</th><th>登记时间</th><th>处置</th></tr>'
                  + "".join(trs) + "</table></div>")
    return layout("异常处置", body)


def page_approvals(msg):
    over = []
    for it in svc.hall_status():
        if it["lamp"] and it["forecast"]["over_threshold"]:
            pid = it["projector"]["id"]
            if it.get("approval"):
                block = (f'<span class="pill p-ok">已批准</span> '
                         f'<span class="muted">至 {esc(it["approval"]["usable_until"])} · '
                         f'{esc(it["approval"]["approver"])}</span>')
            else:
                block = (f'<span class="pill p-bad">待放映主管批准</span><br>'
                         f'<form method="post" action="/approve" class="inline">'
                         f'<input type="hidden" name="projector_id" value="{pid}">'
                         f'主管 <input name="approver" size="8" placeholder="姓名"> '
                         f'角色 <input name="role" size="10" value="{SUPERVISOR_ROLE}"> '
                         f'理由 <input name="reason" size="14" placeholder="临时使用理由"> '
                         f'<button class="gold" type="submit">批准临时使用</button></form>')
            over.append(f'<tr><td>{esc(it["hall"]["name"])}</td>'
                        f'<td>{esc(it["lamp"]["serial_no"])}</td>'
                        f'<td>{it["lamp"]["lit_hours"]:.0f} / {it["lamp"]["threshold_hours"]:.0f}h</td>'
                        f'<td>{block}</td></tr>')
    over_tbl = ("".join(over) if over
                else '<tr><td colspan="4" class="muted">当前无超阈值设备</td></tr>')
    body = msg + f"""<div class="card"><h2>超阈值设备临时使用审批（仅 {SUPERVISOR_ROLE}）</h2>
     <table><tr><th>影厅</th><th>灯泡</th><th>累计/阈值</th><th>审批</th></tr>{over_tbl}</table></div>
     <div class="card muted">最终放行在各
     <a href="/tickets">换灯工单</a> 详情页由放映主管（非换灯人）签署。</div>"""
    return layout("放行审批", body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, html_text, code=200):
        data = html_text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        msg = self._flash(qs)
        try:
            if path == "/":
                self._send(page_index(qs, msg))
            elif path == "/ledger":
                self._send(page_ledger(msg))
            elif path == "/shows":
                self._send(page_shows(msg))
            elif path == "/tickets":
                self._send(page_tickets(msg))
            elif path == "/ticket":
                self._send(page_ticket(int(qs.get("id", ["0"])[0]), msg))
            elif path == "/incidents":
                self._send(page_incidents(msg))
            elif path == "/approvals":
                self._send(page_approvals(msg))
            elif path == "/photo":
                self._photo(qs.get("name", [""])[0])
            else:
                self._send(layout("404", '<div class="msg err">页面不存在</div>'), 404)
        except DomainError as e:
            self._redirect("/", "err", str(e))

    def _flash(self, qs):
        if qs.get("ok"):
            return f'<div class="msg ok">{esc(qs["ok"][0])}</div>'
        if qs.get("err"):
            return f'<div class="msg err">{esc(qs["err"][0])}</div>'
        return ""

    def _redirect(self, path, kind, text):
        location = f"{path}?{kind}=" + urllib.parse.quote(str(text))
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _photo(self, name):
        # 只允许 PHOTO_DIR 内的普通文件名
        name = os.path.basename(name)
        full = os.path.realpath(os.path.join(config.PHOTO_DIR, name))
        if not full.startswith(os.path.realpath(config.PHOTO_DIR)) or not os.path.exists(full):
            self.send_error(404)
            return
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        f = lambda k: form.get(k, [""])[0].strip()
        path = urllib.parse.urlparse(self.path).path
        back = form.get("back", ["/"])[0]
        try:
            if path == "/open-ticket":
                tid = svc.open_ticket(int(f("projector_id")), f("tech"), f("spare"))
                self._redirect(f"/ticket?id={tid}", "ok", f"工单 #{tid} 已建立并占用维修窗口")
            elif path == "/step":
                tid, no = int(f("ticket_id")), int(f("step_no"))
                svc.complete_step(tid, no, f("tech"), f("note"))
                self._redirect(f"/ticket?id={tid}", "ok", f"步骤 {no} 已完成")
            elif path == "/cancel-ticket":
                tid = int(f("ticket_id"))
                svc.cancel_ticket(tid, f("tech"), f("note"))
                self._redirect(f"/ticket?id={tid}", "ok", "工单已中途取消，维修窗口已释放")
            elif path == "/complete-ticket":
                tid = int(f("ticket_id"))
                svc.complete_ticket(tid, f("tech"))
                self._redirect(f"/ticket?id={tid}", "ok", "已完工并释放维修窗口，等待主管放行")
            elif path == "/release":
                tid = int(f("ticket_id"))
                rid = svc.release(tid, f("signer"), f("role"), f("note"))
                self._redirect(f"/ticket?id={tid}", "ok", f"最终放行已签署（记录 #{rid}）")
            elif path == "/approve":
                aid = svc.approve_over_threshold(int(f("projector_id")), f("approver"),
                                                 f("role") or SUPERVISOR_ROLE, f("reason"))
                self._redirect("/approvals", "ok", f"临时使用已批准（记录 #{aid}），24 小时内有效")
            elif path == "/delay-show":
                sid = int(f("show_id"))
                svc.delay_show(sid, float(f("delay_min") or 0), "人工登记延误")
                self._redirect("/shows", "ok", f"场次 #{sid} 延误已登记")
            elif path == "/resolve-incident":
                svc.resolve_incident(int(f("incident_id")), f("resolution"), f("who"))
                self._redirect("/incidents", "ok", "异常已标记处置")
            else:
                self._redirect(back, "err", "未知操作")
        except (MaintenanceConflict, SpareMismatch, OverThresholdNeedsApproval,
                TimerResetFailed, BrightnessInsufficient, ReleaseNotAllowed,
                DomainError, ValueError, KeyError) as e:
            target = back
            if path in ("/step", "/cancel-ticket", "/complete-ticket", "/release"):
                target = f"/ticket?id={f('ticket_id') or '0'}"
            self._redirect(target, "err", str(e))


def serve(host="127.0.0.1", port=8000):
    global svc
    if svc is None:
        svc = Service()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"影城灯泡管理已启动：http://{host}:{port}")
    httpd.serve_forever()
