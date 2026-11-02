#!/usr/bin/env python3
"""
slack_cleanup.py — Slack에서 내가 쓴 메시지를 채널/DM별로 찾아서 삭제합니다.

준비
  1) pip install requests
  2) api.slack.com/apps 에서 앱을 만들고 User Token Scopes 추가 후 워크스페이스에 설치
       channels:history groups:history im:history mpim:history
       channels:read groups:read im:read mpim:read users:read search:read
       chat:write  (+ 파일까지 지울 때: files:read files:write)
  3) 발급된 xoxp- 토큰을 환경변수로 지정
       macOS/Linux:  export SLACK_USER_TOKEN="xoxp-..."
       Windows:      set SLACK_USER_TOKEN=xoxp-...

사용법
  # 1단계: 스캔(드라이런) — 아무것도 지우지 않고 채널/DM별 개수만 보여주고 scan.json에 백업
  python slack_cleanup.py scan
  python slack_cleanup.py scan --search      # 이미 나간 채널의 메시지도 검색으로 찾기
  python slack_cleanup.py scan --files       # 내가 올린 파일 목록도 포함

  # 2단계: 삭제 — scan.json을 기준으로 지움 (중간에 끊겨도 다시 실행하면 이어서 진행)
  python slack_cleanup.py delete                          # 전부
  python slack_cleanup.py delete --only general,random    # 특정 채널만
  python slack_cleanup.py delete --only 홍길동             # 특정 사람과의 DM만
  python slack_cleanup.py delete --types dm,group         # DM/그룹DM만
  python slack_cleanup.py delete --exclude announcements  # 특정 채널 제외
  python slack_cleanup.py delete --files                  # 스캔된 파일도 삭제

  --types 값: public, private, dm, group (쉼표로 여러 개)
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

try:
    import requests
except ImportError:
    sys.exit("requests 패키지가 필요합니다: pip install requests")

API = "https://slack.com/api/"
# 일반 메시지, 스레드→채널 브로드캐스트, 파일 공유, /me 메시지만 대상 (입장/퇴장 알림 등은 제외)
DELETABLE_SUBTYPES = {None, "thread_broadcast", "file_share", "me_message"}


class SlackError(Exception):
    def __init__(self, method, error):
        super().__init__(f"{method}: {error}")
        self.method, self.error = method, error


class Slack:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def call(self, method, http="GET", **params):
        while True:
            if http == "GET":
                r = self.s.get(API + method, params=params, timeout=60)
            else:
                r = self.s.post(API + method, data=params, timeout=60)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "10"))
                print(f"    ⏳ 속도 제한 — {wait}초 대기 후 재시도")
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                if data.get("error") == "ratelimited":
                    time.sleep(10)
                    continue
                raise SlackError(method, data.get("error"))
            return data

    def paginate(self, method, key, **params):
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            data = self.call(method, **params)
            yield from data.get(key, [])
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break


# ---------------------------------------------------------------- helpers

def ts_to_str(ts):
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")


def is_mine(m, my_id):
    return m.get("user") == my_id and m.get("subtype") in DELETABLE_SUBTYPES


def slim(m):
    return {
        "ts": m["ts"],
        "date": ts_to_str(m["ts"]),
        "thread_ts": m.get("thread_ts"),
        "text": m.get("text", ""),
        "files": [f.get("name") for f in m.get("files", []) if isinstance(f, dict)],
    }


def conv_type(c):
    if c.get("is_im"):
        return "dm"
    if c.get("is_mpim"):
        return "group"
    if c.get("is_private"):
        return "private"
    return "public"


def conv_label(c, users):
    t = conv_type(c)
    if t == "dm":
        name = users.get(c.get("user"), c.get("user", "?"))
        return name, f"DM @{name}"
    if t == "group":
        return c.get("name", c["id"]), f"그룹DM {c.get('name', c['id'])}"
    name = c.get("name", c["id"])
    prefix = "🔒#" if t == "private" else "#"
    suffix = " (보관됨)" if c.get("is_archived") else ""
    return name, f"{prefix}{name}{suffix}"


def collect_my_messages(sl, cid, my_id, threads=True):
    found = {}
    for m in sl.paginate("conversations.history", "messages", channel=cid, limit=200):
        if is_mine(m, my_id):
            found[m["ts"]] = slim(m)
        if threads and m.get("reply_count") and m.get("thread_ts") == m.get("ts"):
            for r in sl.paginate("conversations.replies", "messages",
                                 channel=cid, ts=m["ts"], limit=200):
                if r["ts"] != m["ts"] and is_mine(r, my_id):
                    found[r["ts"]] = slim(r)
    return sorted(found.values(), key=lambda x: float(x["ts"]))


def search_extra(sl, my_id, known_ids, users):
    """검색 API로 내가 멤버가 아닌 대화(나간 채널 등)의 메시지를 찾는다."""
    extra = {}
    page = 1
    while True:
        d = sl.call("search.messages", query=f"from:<@{my_id}>",
                    count=100, page=page, sort="timestamp")
        msgs = d.get("messages", {})
        for m in msgs.get("matches", []):
            ch = m.get("channel") or {}
            cid = ch.get("id")
            if not cid or cid in known_ids:
                continue
            if cid not in extra:
                if ch.get("is_im"):
                    other = ch.get("user") or ch.get("name")
                    name = users.get(other, other)
                    t, label = "dm", f"DM @{name}"
                elif ch.get("is_mpim"):
                    name, t = ch.get("name", cid), "group"
                    label = f"그룹DM {name}"
                else:
                    name = ch.get("name", cid)
                    t = "private" if ch.get("is_private") else "public"
                    label = ("🔒#" if t == "private" else "#") + name + " (멤버 아님)"
                extra[cid] = {"id": cid, "name": name, "label": label,
                              "type": t, "messages": {}}
            extra[cid]["messages"][m["ts"]] = slim(m)
        pages = msgs.get("paging", {}).get("pages", 1)
        if page >= pages or page >= 100:
            break
        page += 1
    out = []
    for c in extra.values():
        c["messages"] = sorted(c["messages"].values(), key=lambda x: float(x["ts"]))
        out.append(c)
    return out


def list_my_files(sl, my_id):
    files, page = [], 1
    while True:
        d = sl.call("files.list", user=my_id, count=200, page=page)
        for f in d.get("files", []):
            files.append({"id": f["id"], "name": f.get("name"),
                          "date": ts_to_str(f.get("created", 0)),
                          "size": f.get("size", 0)})
        pages = d.get("paging", {}).get("pages", 1)
        if page >= pages:
            break
        page += 1
    return files


# ---------------------------------------------------------------- scan

def cmd_scan(sl, args):
    me = sl.call("auth.test")
    my_id = me["user_id"]
    print(f"👤 {me['user']} ({my_id}) @ {me['team']}\n")

    print("사용자 목록 불러오는 중...")
    users = {}
    for u in sl.paginate("users.list", "members", limit=200):
        p = u.get("profile", {})
        users[u["id"]] = p.get("display_name") or u.get("real_name") or u.get("name")

    print("내가 속한 채널/DM 불러오는 중...")
    convs = list(sl.paginate("users.conversations", "channels", limit=200,
                             types="public_channel,private_channel,mpim,im",
                             exclude_archived="false"))
    print(f"  → {len(convs)}개 대화\n")

    results = []
    for i, c in enumerate(convs, 1):
        name, label = conv_label(c, users)
        print(f"[{i}/{len(convs)}] {label} 스캔 중...", end="", flush=True)
        try:
            msgs = collect_my_messages(sl, c["id"], my_id, threads=not args.no_threads)
        except SlackError as e:
            print(f" 건너뜀 ({e.error})")
            continue
        print(f" {len(msgs)}개")
        if msgs:
            results.append({"id": c["id"], "name": name, "label": label,
                            "type": conv_type(c), "messages": msgs})

    if args.search:
        print("\n검색으로 나간 채널 등 추가 확인 중...")
        known = {c["id"] for c in convs}
        extra = search_extra(sl, my_id, known, users)
        print(f"  → 추가 대화 {len(extra)}개")
        results.extend(extra)

    files = []
    if args.files:
        print("\n내가 올린 파일 불러오는 중...")
        files = list_my_files(sl, my_id)
        print(f"  → {len(files)}개")

    plan = {"user_id": my_id, "user": me["user"], "team": me["team"],
            "scanned_at": datetime.now().isoformat(timespec="seconds"),
            "conversations": results, "files": files}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    print_summary(results, files)
    print(f"\n💾 백업/계획 파일 저장: {args.out}  (메시지 원문 포함)")
    print("   다음 단계: python slack_cleanup.py delete [--only ...] [--types ...]")


def print_summary(convs, files=()):
    total = sum(len(c["messages"]) for c in convs)
    order = {"public": 0, "private": 1, "group": 2, "dm": 3}
    print("\n" + "=" * 60)
    print(f"{'대화':<40}{'메시지 수':>10}  기간")
    print("-" * 60)
    for c in sorted(convs, key=lambda c: (order.get(c["type"], 9), -len(c["messages"]))):
        m = c["messages"]
        span = f"{m[0]['date'][:10]} ~ {m[-1]['date'][:10]}" if m else ""
        print(f"{c['label'][:38]:<40}{len(m):>10}  {span}")
    print("-" * 60)
    print(f"{'합계':<40}{total:>10}")
    if files:
        print(f"{'업로드 파일':<40}{len(files):>10}")
    print("=" * 60)
    est = total * 1.25 / 60
    print(f"예상 삭제 시간: 약 {est:.0f}분 (분당 ~50건 기준)")


# ---------------------------------------------------------------- delete

def select(convs, only, exclude, types):
    def norm(s):
        return s.lower().lstrip("#@")

    def match(c, keys):
        names = {norm(c["id"]), norm(c["name"]), norm(c["label"])}
        return any(k in names for k in keys)

    out = convs
    if types:
        out = [c for c in out if c["type"] in types]
    if only:
        keys = {norm(k) for k in only}
        out = [c for c in out if match(c, keys)]
    if exclude:
        keys = {norm(k) for k in exclude}
        out = [c for c in out if not match(c, keys)]
    return out


def load_done(path):
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def cmd_delete(sl, args):
    if not os.path.exists(args.plan):
        sys.exit(f"{args.plan} 이 없습니다. 먼저 scan 을 실행하세요.")
    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)

    me = sl.call("auth.test")
    if me["user_id"] != plan["user_id"]:
        sys.exit("토큰 사용자와 scan.json 사용자가 다릅니다. 중단합니다.")

    split = lambda s: [x.strip() for x in s.split(",") if x.strip()] if s else []
    convs = select(plan["conversations"], split(args.only), split(args.exclude),
                   set(split(args.types)))
    done = load_done(args.log)

    todo = []
    for c in convs:
        # 최신 메시지부터 지워서 스레드 답글이 부모보다 먼저 지워지게 함
        for m in reversed(c["messages"]):
            key = f"{c['id']}:{m['ts']}"
            if key not in done:
                todo.append((c, m, key))
    files = [f for f in plan.get("files", []) if f"file:{f['id']}" not in done] if args.files else []

    if not todo and not files:
        print("삭제할 항목이 없습니다. (--only/--types 조건 또는 이미 삭제 완료)")
        return

    remaining = {}
    for c, m, _ in todo:
        remaining.setdefault(c["id"], {**c, "messages": []})["messages"].append(m)
    for c in remaining.values():
        c["messages"].reverse()
    print_summary(list(remaining.values()), files)

    if not args.yes:
        print("\n⚠️  삭제는 되돌릴 수 없습니다.")
        if input("계속하려면 DELETE 를 입력하세요: ").strip() != "DELETE":
            print("취소했습니다.")
            return

    ok = fail = 0
    errors = {}
    with open(args.log, "a", encoding="utf-8") as log:
        for i, (c, m, key) in enumerate(todo, 1):
            try:
                sl.call("chat.delete", http="POST", channel=c["id"], ts=m["ts"])
                ok += 1
                log.write(key + "\n")
            except SlackError as e:
                if e.error == "message_not_found":
                    log.write(key + "\n")  # 이미 지워짐
                else:
                    fail += 1
                    errors[e.error] = errors.get(e.error, 0) + 1
            log.flush()
            if i % 25 == 0 or i == len(todo):
                print(f"  [{i}/{len(todo)}] {c['label']} — 삭제 {ok}, 실패 {fail}")
            time.sleep(args.delay)

        for j, f in enumerate(files, 1):
            try:
                sl.call("files.delete", http="POST", file=f["id"])
                ok += 1
                log.write(f"file:{f['id']}\n")
            except SlackError as e:
                if e.error == "file_deleted" or e.error == "file_not_found":
                    log.write(f"file:{f['id']}\n")
                else:
                    fail += 1
                    errors[e.error] = errors.get(e.error, 0) + 1
            log.flush()
            if j % 25 == 0 or j == len(files):
                print(f"  [파일 {j}/{len(files)}] 삭제 누적 {ok}, 실패 {fail}")
            time.sleep(args.delay)

    print(f"\n✅ 완료: 삭제 {ok}건, 실패 {fail}건")
    if errors:
        print("실패 사유:", ", ".join(f"{k} ×{v}" for k, v in errors.items()))
        print("  - cant_delete_message: 워크스페이스 설정상 삭제 불가 메시지")
        print("  - is_archived / channel_not_found: 보관된 채널이거나 접근 불가 → 채널 복원/재참여 후 재실행")
    print(f"진행 기록: {args.log} (다시 실행하면 남은 것만 이어서 처리)")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Slack에서 내 메시지를 채널/DM별로 삭제")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="내 메시지를 찾아 요약하고 scan.json에 저장 (삭제 안 함)")
    s.add_argument("--out", default="scan.json")
    s.add_argument("--search", action="store_true", help="검색 API로 나간 채널 메시지도 찾기")
    s.add_argument("--files", action="store_true", help="내가 올린 파일 목록 포함")
    s.add_argument("--no-threads", action="store_true", help="스레드 답글은 건너뛰기 (빠름)")

    d = sub.add_parser("delete", help="scan.json 기준으로 삭제")
    d.add_argument("--plan", default="scan.json")
    d.add_argument("--log", default="deleted.log")
    d.add_argument("--only", help="이 채널/DM만 (이름·ID, 쉼표 구분)")
    d.add_argument("--exclude", help="이 채널/DM 제외 (이름·ID, 쉼표 구분)")
    d.add_argument("--types", help="public,private,dm,group 중 선택")
    d.add_argument("--files", action="store_true", help="스캔된 파일도 삭제")
    d.add_argument("--delay", type=float, default=1.2, help="요청 간 대기(초), 기본 1.2")
    d.add_argument("--yes", action="store_true", help="확인 질문 생략")

    args = ap.parse_args()
    token = os.environ.get("SLACK_USER_TOKEN")
    if not token or not token.startswith("xoxp-"):
        sys.exit("SLACK_USER_TOKEN 환경변수에 xoxp- 로 시작하는 User OAuth Token을 넣어주세요.")
    sl = Slack(token)
    try:
        cmd_scan(sl, args) if args.cmd == "scan" else cmd_delete(sl, args)
    except SlackError as e:
        if e.error == "missing_scope":
            sys.exit(f"권한 부족 ({e.method}) — 앱의 User Token Scopes를 확인하고 재설치하세요.")
        sys.exit(f"Slack API 오류: {e}")
    except KeyboardInterrupt:
        print("\n중단됨. 다시 실행하면 이어서 진행합니다.")


if __name__ == "__main__":
    main()
