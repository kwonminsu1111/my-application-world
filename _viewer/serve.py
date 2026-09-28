"""지원결과 뷰어 로컬 서버.

지원결과 폴더를 요청마다 새로 읽어 JSON으로 내려주고, 파일 열기/탐색기 열기를 처리한다.
127.0.0.1 에만 바인딩한다. 실행: python serve.py  (또는 상위 폴더의 보기.bat)
"""
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = 8765

# 단계 폴더 → (전형 단계, 결과)
STAGE_FOLDERS = {
    "서탈": ("서류", "탈"), "서합": ("서류", "합"),
    "필탈": ("필기", "탈"), "필합": ("필기", "합"),
    "1차면탈": ("1차면접", "탈"), "1차면합": ("1차면접", "합"),
    "2차면탈": ("2차면접", "탈"), "2차면합": ("2차면접", "합"),
    "3차면탈": ("3차면접", "탈"), "3차면합": ("3차면접", "합"),
    "최종합격": ("최종", "합"),
}
STEP_RANK = {"서류": 0, "필기": 1, "1차면접": 2, "2차면접": 3, "3차면접": 4, "최종": 9}
FAIL_LABEL = {"서류": "서탈", "필기": "필탈", "1차면접": "1차면탈", "2차면접": "2차면탈", "3차면접": "3차면탈"}
DEFAULT_STEPS = ["서류", "필기", "1차면접", "2차면접"]
FOLDER_FOR = {v: k for k, v in STAGE_FOLDERS.items()}
STEP_ALIASES = {
    "서류": "서류", "서류전형": "서류",
    "필기": "필기", "필기전형": "필기",
    "1차": "1차면접", "1차면접": "1차면접", "면접": "1차면접",
    "2차": "2차면접", "2차면접": "2차면접",
    "3차": "3차면접", "3차면접": "3차면접",
}
HALF_RE = re.compile(r"^\d{2}[상하]$")
APP_RE = re.compile(r"^(?P<company>.+?)_(?P<half>\d{2}[상하])(?:_(?P<suffix>.+))?$")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
INFO_NAME = "정보.txt"


def read_text(path, warnings):
    raw = open(path, "rb").read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        warnings.append(f"{rel(path)}: UTF-8이 아닙니다. 메모장에서 'UTF-8'로 다시 저장해 주세요.")
        return raw.decode("cp949", errors="replace")


def rel(path):
    return os.path.relpath(path, ROOT).replace("\\", "/")


def parse_info(path, warnings):
    info = {"기업": "", "직무": "", "전형": "", "가점": "", "메모": ""}
    lines = read_text(path, warnings).splitlines()
    for i, line in enumerate(lines):
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or key not in info:
            continue
        if key == "메모":
            info["메모"] = "\n".join([value.strip()] + lines[i + 1:]).strip()
            break
        info[key] = value.strip()
    return info


def parse_steps(text, where, warnings):
    if not text.strip():
        return list(DEFAULT_STEPS)
    steps = []
    for token in re.split(r"[,，/]", text):
        token = token.strip().replace(" ", "")
        if not token:
            continue
        step = STEP_ALIASES.get(token)
        if step is None:
            warnings.append(f"{where}: 전형 '{token}'을(를) 알 수 없습니다. (쓸 수 있는 값: 서류, 필기, 1차면접, 2차면접, 3차면접)")
        elif step not in steps:
            steps.append(step)
    steps.sort(key=STEP_RANK.get)
    return steps or list(DEFAULT_STEPS)


def file_kind(name):
    low = name.lower()
    if "자소서" in name or "자기소개서" in name:
        return 0
    if "지원서" in name or "이력서" in name or "application" in low:
        return 1
    if "공고" in name:
        return 2
    if "직무" in name or "직기" in name:
        return 3
    return 4


def scan():
    warnings = []
    apps = {}
    for stage in sorted(os.listdir(ROOT)):
        stage_dir = os.path.join(ROOT, stage)
        if not os.path.isdir(stage_dir) or stage.startswith(("_", ".")):
            continue
        if stage not in STAGE_FOLDERS:
            warnings.append(f"{stage}/: 알 수 없는 단계 폴더입니다. (README.txt의 단계 폴더 목록 참고)")
            continue
        for half in sorted(os.listdir(stage_dir)):
            half_dir = os.path.join(stage_dir, half)
            if not os.path.isdir(half_dir):
                warnings.append(f"{stage}/{half}: 단계 폴더 바로 아래에는 반기 폴더(예: 26하)만 둘 수 있습니다.")
                continue
            if not HALF_RE.match(half):
                warnings.append(f"{stage}/{half}/: 반기 폴더 이름은 '26하'처럼 연도 2자리 + 상/하여야 합니다.")
                continue
            for name in sorted(os.listdir(half_dir)):
                app_dir = os.path.join(half_dir, name)
                if not os.path.isdir(app_dir):
                    warnings.append(f"{stage}/{half}/{name}: 반기 폴더 안에는 지원 폴더만 둘 수 있습니다.")
                    continue
                m = APP_RE.match(name)
                if not m:
                    warnings.append(f"{stage}/{half}/{name}/: 지원 폴더 이름은 '기업명_반기' 또는 '기업명_반기_구분'이어야 합니다.")
                    continue
                if m["half"] != half:
                    warnings.append(f"{stage}/{half}/{name}/: 폴더 이름의 반기({m['half']})와 상위 반기 폴더({half})가 다릅니다.")
                apps.setdefault(name, {"match": m, "copies": []})["copies"].append((stage, app_dir))

    result = []
    for name, entry in apps.items():
        m, copies = entry["match"], entry["copies"]
        copies.sort(key=lambda c: (STEP_RANK[STAGE_FOLDERS[c[0]][0]], STAGE_FOLDERS[c[0]][1] == "합"))
        final_stage, final_dir = copies[-1]
        steps_in_folders = {}
        for stage, _ in copies:
            step, res = STAGE_FOLDERS[stage]
            steps_in_folders.setdefault(step, set()).add(res)
        for step, results in steps_in_folders.items():
            if len(results) > 1:
                warnings.append(f"{name}: '{step}' 단계가 합격 폴더와 탈락 폴더에 모두 있습니다.")

        info_path = os.path.join(final_dir, INFO_NAME)
        if os.path.isfile(info_path):
            info = parse_info(info_path, warnings)
        else:
            info = {"기업": "", "직무": "", "전형": "", "가점": "", "메모": ""}
            warnings.append(f"{rel(final_dir)}/: {INFO_NAME}이 없습니다. (가장 뒤 단계 폴더에 두세요)")
        if info["기업"] and info["기업"] != m["company"]:
            warnings.append(f"{rel(info_path)}: 기업 '{info['기업']}'이(가) 폴더 이름의 '{m['company']}'와 다릅니다.")

        steps = parse_steps(info["전형"], rel(info_path), warnings)
        final_step, final_res = STAGE_FOLDERS[final_stage]
        step_states = []
        if final_step == "최종":
            step_states = [{"name": s, "state": "pass"} for s in steps]
            label, tone = "최종합격", "success"
        else:
            if final_step not in steps:
                warnings.append(f"{name}: '{final_stage}' 폴더에 있지만 정보.txt 전형에 '{final_step}'이 없습니다.")
                steps.append(final_step)
                steps.sort(key=STEP_RANK.get)
            frank = STEP_RANK[final_step]
            skipped = False
            for s in steps:
                r = STEP_RANK[s]
                if r < frank:
                    state = "pass"
                elif r == frank:
                    state = "pass" if final_res == "합" else "fail"
                elif final_res == "합" and not skipped:
                    state, skipped = "skip", True
                else:
                    state = "none"
                step_states.append({"name": s, "state": state})
            if final_res == "탈":
                label, tone = FAIL_LABEL[final_step], "danger"
            else:
                nxt = next((x["name"] for x in step_states if x["state"] == "skip"), None)
                if nxt:
                    label, tone = f"{nxt}미응시", "neutral"
                else:
                    label, tone = f"{final_step} 합격", "neutral"
                    warnings.append(f"{name}: 마지막 전형까지 합격했다면 '최종합격' 폴더로 옮겨 주세요.")

        files, seen = [], {}
        for stage, app_dir in copies:
            for dirpath, _, filenames in os.walk(app_dir):
                for fn in filenames:
                    full = os.path.join(dirpath, fn)
                    sub = os.path.relpath(full, app_dir).replace("\\", "/")
                    if sub == INFO_NAME:
                        continue
                    size = os.path.getsize(full)
                    key = (sub, size)
                    if key in seen:
                        seen[key]["stages"].append(stage)
                        continue
                    ext = os.path.splitext(fn)[1].lower()
                    item = {
                        "name": fn, "sub": sub, "path": rel(full), "size": size, "ext": ext,
                        "image": ext in IMAGE_EXT, "etc": sub.split("/")[0] == "기타",
                        "order": file_kind(fn), "stages": [stage],
                        "mtime": os.path.getmtime(full),
                    }
                    seen[key] = item
                    files.append(item)
        files.sort(key=lambda f: (f["order"], f["sub"]))

        result.append({
            "id": name,
            "company": info["기업"] or m["company"],
            "half": m["half"],
            "suffix": m["suffix"] or "",
            "job": info["직무"],
            "score": info["가점"],
            "memo": info["메모"],
            "infoPath": rel(info_path) if os.path.isfile(info_path) else "",
            "steps": step_states,
            "label": label,
            "tone": tone,
            "finalStage": final_stage,
            "next": next_move(final_step, final_res, step_states),
            "folders": [{"stage": s, "path": rel(d)} for s, d in copies],
            "files": files,
        })
    result.sort(key=lambda a: (a["half"], a["company"]), reverse=False)
    return {"root": ROOT, "apps": result, "warnings": warnings}


def next_move(final_step, final_res, step_states):
    """다음 전형 결과를 기록할 때 옮겨갈 단계 폴더. 이미 끝난 지원이면 None."""
    if final_step == "최종" or final_res == "탈":
        return None
    nxt = next((s["name"] for s in step_states if s["state"] == "skip"), None)
    if nxt is None:
        return {"step": "최종", "pass": "최종합격", "fail": None}
    is_last = nxt == step_states[-1]["name"]
    return {"step": nxt, "pass": "최종합격" if is_last else FOLDER_FOR[(nxt, "합")], "fail": FOLDER_FOR[(nxt, "탈")]}


def advance_app(payload):
    """지원 폴더(가장 뒤 단계 복사본)를 다음 결과 단계 폴더로 이동한다. 결과: {id, path, stage}"""
    app = next((a for a in scan()["apps"] if a["id"] == payload.get("id")), None)
    if not app:
        raise ValueError("지원을 찾을 수 없어요. 새로고침 후 다시 시도해 주세요.")
    move = app["next"]
    if not move:
        raise ValueError("이미 결과가 끝난 지원이에요.")
    result = payload.get("result")
    stage = move["pass"] if result == "합" else move["fail"] if result == "탈" else None
    if not stage:
        raise ValueError("결과를 골라 주세요.")
    src = os.path.join(ROOT, app["folders"][-1]["path"])
    half_dir = os.path.dirname(src)
    dst = os.path.join(ROOT, stage, app["half"], app["id"])
    if os.path.exists(dst):
        raise ValueError(f"옮길 위치에 같은 폴더가 이미 있어요: {rel(dst)}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        shutil.move(src, dst)
    except PermissionError:
        raise ValueError("폴더 안 파일이 다른 프로그램에서 열려 있어요. 한글·PDF·탐색기 창을 닫고 다시 시도해 주세요.")
    if not os.listdir(half_dir):
        os.rmdir(half_dir)  # 비게 된 반기 폴더 정리
    return {"id": app["id"], "path": rel(dst), "stage": stage}


CREATE_STAGES = ("서합", "서탈")
BAD_NAME_CHARS = set('\\/:*?"<>|_')


def check_name(value, label, required):
    value = (value or "").strip()
    if not value:
        if required:
            raise ValueError(f"{label} 칸을 입력해 주세요.")
        return ""
    if any(c in BAD_NAME_CHARS or c.isspace() for c in value):
        raise ValueError(f"{label}에는 공백과 \\ / : * ? \" < > | _ 를 쓸 수 없어요.")
    if len(value) > 60 or value.startswith(".") or value.endswith("."):
        raise ValueError(f"{label}이(가) 올바르지 않아요.")
    return value


def create_app(payload):
    """새 지원 폴더와 정보.txt를 만든다. 결과: {id, path}"""
    half = (payload.get("half") or "").strip()
    stage = payload.get("stage") or "서합"
    if not HALF_RE.match(half):
        raise ValueError("반기는 '26하'처럼 연도 2자리 + 상/하로 골라 주세요.")
    if stage not in CREATE_STAGES:
        raise ValueError("결과는 서합 또는 서탈만 고를 수 있어요.")
    company = check_name(payload.get("company"), "기업명", True)
    suffix = check_name(payload.get("suffix"), "구분", False)
    job = " ".join((payload.get("job") or "").split())
    name = f"{company}_{half}" + (f"_{suffix}" if suffix else "")
    existing = next((a for a in scan()["apps"] if a["id"] == name), None)
    if existing:
        raise ValueError(f"이미 있는 지원이에요: {existing['folders'][-1]['path']}  (같은 반기에 또 지원했다면 '구분'을 입력해 주세요)")
    target = os.path.join(ROOT, stage, half, name)
    os.makedirs(target)
    with open(os.path.join(target, INFO_NAME), "w", encoding="utf-8") as f:
        f.write(f"기업: {company}\n직무: {job}\n전형: \n가점: \n메모:\n")
    return {"id": name, "path": rel(target)}


def safe_path(p):
    full = os.path.realpath(os.path.join(ROOT, p))
    if os.path.commonpath([full, os.path.realpath(ROOT)]) != os.path.realpath(ROOT):
        return None
    return full if os.path.exists(full) else None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            body = open(os.path.join(HERE, "index.html"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/data":
            self.send_json(scan())
        elif url.path == "/file":
            full = safe_path(parse_qs(url.query).get("p", [""])[0])
            if not full or not os.path.isfile(full):
                return self.send_error(404)
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/"):
                ctype += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(os.path.getsize(full)))
            self.end_headers()
            with open(full, "rb") as f:
                while chunk := f.read(1 << 16):
                    self.wfile.write(chunk)
        else:
            self.send_error(404)

    def do_POST(self):
        # 커스텀 헤더를 요구해 다른 사이트에서 보낸 요청(CORS preflight 대상)을 막는다.
        if self.headers.get("X-Viewer") != "1":
            return self.send_error(403)
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        action = {"/api/create": create_app, "/api/advance": advance_app}.get(urlparse(self.path).path)
        if action:
            try:
                return self.send_json({"ok": True, **action(payload)})
            except ValueError as e:
                return self.send_json({"ok": False, "error": str(e)}, 400)
        full = safe_path(payload.get("p", ""))
        if not full:
            return self.send_json({"ok": False, "error": "파일을 찾을 수 없습니다."}, 404)
        url = urlparse(self.path)
        if url.path == "/api/open":
            os.startfile(full)
        elif url.path == "/api/reveal":
            if os.path.isdir(full):
                subprocess.Popen(["explorer", full])
            else:
                subprocess.Popen(f'explorer /select,"{full}"')
        else:
            return self.send_error(404)
        self.send_json({"ok": True})


class Server(ThreadingHTTPServer):
    # Windows에서 SO_REUSEADDR를 켜면 같은 포트에 서버가 여러 개 떠서 예전 코드가 요청을 받을 수 있다.
    allow_reuse_address = False
    daemon_threads = True


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"Port {PORT} is already in use. The viewer may already be running.")
        if "--no-browser" not in sys.argv:
            webbrowser.open(f"http://127.0.0.1:{PORT}/")
        return
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Viewer running at {url}  (close this window to stop)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
