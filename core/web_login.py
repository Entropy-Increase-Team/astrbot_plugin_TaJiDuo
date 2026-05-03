import json
import mimetypes
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

import httpx


PHONE_RE = re.compile(r"^1\d{10}$")
CAPTCHA_RE = re.compile(r"^\d{4,8}$")
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
STATIC_ASSETS = {
    "/assets/bg/main.png": PLUGIN_ROOT / "img" / "bg" / "UI_YH_Bond_MainUI_Bkg.png",
    "/assets/bg/figure.png": PLUGIN_ROOT / "img" / "bg" / "YH_lihui_fashionshop_nanali1.png",
}


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


LOGIN_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TaJiDuo 网页登录</title>
  <style>
    :root {
      color-scheme: dark;
      --panel: rgba(13, 27, 36, 0.74);
      --panel-strong: rgba(8, 20, 28, 0.88);
      --line: rgba(146, 214, 221, 0.24);
      --text: #f4fbfd;
      --muted: rgba(212, 238, 239, 0.72);
      --accent: #96f1ff;
      --accent-2: #62d9ec;
      --danger: #ffd0d0;
      --shadow: rgba(2, 13, 20, 0.42);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      overflow: hidden;
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background: #081017;
      color: var(--text);
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      background:
        linear-gradient(90deg, rgba(6, 17, 25, 0.92) 0%, rgba(7, 23, 31, 0.74) 36%, rgba(8, 23, 31, 0.26) 60%, rgba(6, 16, 23, 0.78) 100%),
        url("/assets/bg/main.png") center center / cover no-repeat;
      transform: scale(1.02);
    }
    body::after {
      content: "";
      position: fixed;
      inset: 0;
      background:
        radial-gradient(circle at 24% 18%, rgba(150, 241, 255, 0.18), transparent 24%),
        radial-gradient(circle at 76% 78%, rgba(98, 217, 236, 0.12), transparent 22%);
      pointer-events: none;
    }
    .stage {
      position: relative;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      padding: 36px 24px;
      isolation: isolate;
    }
    .hero {
      position: fixed;
      right: max(12px, 1.6vw);
      bottom: 0;
      display: flex;
      align-items: flex-end;
      justify-content: flex-end;
      width: min(34vw, 620px);
      min-width: 260px;
      pointer-events: none;
      user-select: none;
      z-index: 0;
    }
    .hero::after {
      content: "";
      position: absolute;
      inset: auto 10% 7% 18%;
      height: 16%;
      background: radial-gradient(circle, rgba(86, 222, 238, 0.24), transparent 72%);
      filter: blur(34px);
      z-index: -1;
    }
    .hero img {
      display: block;
      width: 100%;
      height: auto;
      object-fit: contain;
      object-position: right bottom;
      filter: drop-shadow(0 28px 48px rgba(4, 12, 18, 0.52));
    }
    .panel {
      position: relative;
      padding: 28px 28px 24px;
      border-radius: 26px;
      background:
        linear-gradient(180deg, rgba(18, 45, 56, 0.72) 0%, rgba(7, 22, 29, 0.86) 100%);
      border: 1px solid var(--line);
      box-shadow: 0 24px 60px var(--shadow);
      backdrop-filter: blur(16px);
    }
    .panel::before {
      content: "";
      position: absolute;
      inset: 10px;
      border: 1px solid rgba(150, 241, 255, 0.18);
      border-radius: 18px;
      pointer-events: none;
    }
    .eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 14px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(14, 41, 49, 0.72);
      border: 1px solid rgba(150, 241, 255, 0.18);
      color: var(--accent);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    h1 {
      margin: 0 0 10px;
      font-size: 30px;
      font-weight: 700;
      letter-spacing: 0.02em;
    }
    p {
      margin: 0 0 20px;
      color: var(--muted);
      line-height: 1.6;
    }
    .row { display: grid; gap: 14px; margin-bottom: 14px; }
    label {
      display: block;
      margin-bottom: 8px;
      color: rgba(227, 247, 248, 0.88);
      font-size: 14px;
    }
    input {
      width: 100%;
      padding: 14px 16px;
      border-radius: 14px;
      border: 1px solid rgba(150, 241, 255, 0.14);
      background: rgba(7, 24, 31, 0.84);
      color: var(--text);
      font-size: 16px;
      outline: none;
    }
    input:focus {
      border-color: rgba(150, 241, 255, 0.52);
      box-shadow: 0 0 0 4px rgba(98, 217, 236, 0.10);
    }
    .actions {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-top: 10px;
    }
    button {
      border: 0;
      border-radius: 14px;
      padding: 14px 16px;
      cursor: pointer;
      font-size: 15px;
      font-weight: 700;
      color: #04222c;
      background: linear-gradient(135deg, var(--accent) 0%, var(--accent-2) 100%);
      box-shadow: 0 12px 24px rgba(98, 217, 236, 0.20);
    }
    button.secondary {
      color: var(--text);
      background: rgba(11, 31, 40, 0.92);
      border: 1px solid rgba(150, 241, 255, 0.14);
      box-shadow: none;
    }
    button:disabled {
      opacity: 0.65;
      cursor: wait;
    }
    .status {
      min-height: 58px;
      margin-top: 18px;
      padding: 14px 16px;
      border-radius: 16px;
      background: rgba(8, 24, 31, 0.72);
      border: 1px solid rgba(150, 241, 255, 0.10);
      font-size: 14px;
      line-height: 1.6;
      color: var(--muted);
      white-space: pre-wrap;
    }
    .status.error {
      color: var(--danger);
      border-color: rgba(255, 190, 190, 0.20);
      background: rgba(42, 12, 16, 0.34);
    }
    .status.success {
      color: #b8f7ff;
      border-color: rgba(150, 241, 255, 0.22);
    }
    .tips {
      margin-top: 14px;
      padding-top: 12px;
      border-top: 1px solid rgba(150, 241, 255, 0.12);
      color: rgba(212, 238, 239, 0.60);
      font-size: 12px;
      line-height: 1.7;
    }
    @media (max-width: 920px) {
      .stage {
        padding: 28px 18px;
      }
      .panel {
        width: min(480px, 100%);
      }
      .hero {
        width: min(44vw, 420px);
        min-width: 220px;
        right: -10px;
      }
      .hero img {
        opacity: 0.68;
      }
    }
    @media (max-width: 640px) {
      body::before {
        background:
          linear-gradient(180deg, rgba(6, 17, 25, 0.90) 0%, rgba(7, 23, 31, 0.82) 42%, rgba(6, 16, 23, 0.92) 100%),
          url("/assets/bg/main.png") 66% center / cover no-repeat;
      }
      .stage {
        align-items: center;
        justify-content: center;
        padding: 18px 14px 20px;
      }
      .hero {
        width: min(56vw, 280px);
        min-width: 180px;
        right: -18px;
      }
      .hero img {
        opacity: 0.42;
      }
      .panel {
        width: 100%;
        padding: 22px 18px 18px;
        border-radius: 22px;
      }
      .actions {
        grid-template-columns: 1fr;
      }
      h1 {
        font-size: 26px;
      }
    }
  </style>
</head>
<body>
  <main class="stage">
    <div class="hero">
      <img src="/assets/bg/figure.png" alt="角色立绘">
    </div>
    <section class="panel">
      <div class="eyebrow">塔吉多-异环幻塔插件</div>
      <h1>TaJiDuo 网页登录</h1>
      <p>当前页面仍然走短信验证码登录。先发送验证码，再输入验证码完成登录。</p>
      <div class="row">
        <div>
          <label for="phone">手机号</label>
          <input id="phone" inputmode="numeric" placeholder="请输入 11 位手机号">
        </div>
        <div>
          <label for="captcha">验证码</label>
          <input id="captcha" inputmode="numeric" placeholder="请输入短信验证码">
        </div>
      </div>
      <div class="actions">
        <button id="sendBtn" type="button">发送验证码</button>
        <button id="loginBtn" type="button" class="secondary">完成登录</button>
      </div>
      <div id="status" class="status">等待操作</div>
      <div class="tips">若当前页面无法访问，请检查 `login_server_public_link` 是否填写为当前设备可访问的地址。</div>
    </section>
  </main>
  <script>
    const statusEl = document.getElementById('status');
    const phoneEl = document.getElementById('phone');
    const captchaEl = document.getElementById('captcha');
    const sendBtn = document.getElementById('sendBtn');
    const loginBtn = document.getElementById('loginBtn');

    function setStatus(message, type) {
      statusEl.textContent = message || '';
      statusEl.className = 'status' + (type ? ' ' + type : '');
    }

    async function postJson(path, payload) {
      const response = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload || {})
      });
      return response.json();
    }

    sendBtn.addEventListener('click', async () => {
      const phone = phoneEl.value.trim();
      sendBtn.disabled = true;
      setStatus('验证码发送中，请稍候...');
      try {
        const result = await postJson(window.location.pathname.replace('/login/', '/captcha/'), { phone });
        if (result.code !== 0) {
          setStatus(result.message || '验证码发送失败', 'error');
          return;
        }
        setStatus('验证码已发送，请查看手机短信。', 'success');
      } catch (error) {
        setStatus('验证码发送失败，请检查当前服务地址是否可访问。', 'error');
      } finally {
        sendBtn.disabled = false;
      }
    });

    loginBtn.addEventListener('click', async () => {
      const phone = phoneEl.value.trim();
      const captcha = captchaEl.value.trim();
      loginBtn.disabled = true;
      setStatus('登录中，请稍候...');
      try {
        const result = await postJson(window.location.pathname.replace('/login/', '/session/'), { phone, captcha });
        if (result.code !== 0) {
          setStatus(result.message || '登录失败', 'error');
          return;
        }
        const data = result.data || {};
        setStatus(`登录成功\\n账号：${data.username || '未返回'}\\nUID：${data.tjdUid || '未返回'}\\n你现在可以返回聊天窗口查看结果。`, 'success');
      } catch (error) {
        setStatus('登录失败，请稍后重试。', 'error');
      } finally {
        loginBtn.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


class TaJiDuoWebLoginServer:
    def __init__(
        self,
        *,
        on_login: Callable[[Dict[str, Any]], None],
        logger: Any,
    ) -> None:
        self.on_login = on_login
        self.logger = logger
        self.enabled = False
        self.port = 25188
        self.public_link = ""
        self.base_url = ""
        self.api_key = ""
        self.timeout_ms = 15000
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def configure(
        self,
        *,
        enabled: bool,
        port: int,
        public_link: str,
        base_url: str,
        api_key: str,
        timeout_ms: int,
    ) -> None:
        should_restart = (
            self._server is not None
            and (self.port != int(port) or self.public_link != _safe_str(public_link).rstrip("/"))
        )
        self.enabled = bool(enabled)
        self.port = max(int(port or 25188), 1)
        self.public_link = _safe_str(public_link).rstrip("/")
        self.base_url = _safe_str(base_url).rstrip("/")
        self.api_key = _safe_str(api_key)
        self.timeout_ms = max(int(timeout_ms or 15000), 1000)

        if should_restart:
            self.close()
        if self.enabled:
            self.ensure_started()
        else:
            self.close()

    def is_enabled(self) -> bool:
        return self.enabled

    def ensure_started(self) -> None:
        if not self.enabled or self._server is not None:
            return

        owner = self

        class RequestHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                owner.logger.debug("[TaJiDuo] web login " + format % args)

            def _send_html(self, html: str, status_code: int = 200) -> None:
                body = html.encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_file(self, path: Path) -> None:
                if not path.exists() or not path.is_file():
                    self._send_json({"code": 404, "message": "资源不存在"}, 404)
                    return
                body = path.read_bytes()
                mime, _ = mimetypes.guess_type(path.name)
                self.send_response(200)
                self.send_header("Content-Type", mime or "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_json(self, payload: Dict[str, Any], status_code: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_json(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    return {}

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                asset = STATIC_ASSETS.get(path)
                if asset:
                    self._send_file(asset)
                    return
                if path.startswith("/login/"):
                    session_id = path.split("/")[-1]
                    if not owner.get_session(session_id):
                        self._send_html("<h1>登录链接不存在或已过期</h1>", 404)
                        return
                    self._send_html(LOGIN_HTML)
                    return
                self.send_response(302)
                self.send_header("Location", owner.base_url or "https://tajiduo.shallow.ink")
                self.end_headers()

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if path.startswith("/captcha/"):
                    self._handle_captcha(path.split("/")[-1])
                    return
                if path.startswith("/session/"):
                    self._handle_session(path.split("/")[-1])
                    return
                self._send_json({"code": 404, "message": "未找到接口"}, 404)

            def _handle_captcha(self, session_id: str) -> None:
                session = owner.get_session(session_id)
                if not session:
                    self._send_json({"code": 404, "message": "登录链接不存在或已过期"})
                    return
                payload = self._read_json()
                phone = _safe_str(payload.get("phone"))
                if not PHONE_RE.fullmatch(phone):
                    self._send_json({"code": 400, "message": "请输入正确的 11 位手机号"})
                    return
                try:
                    data = owner.send_captcha(phone)
                except Exception as exc:
                    self._send_json({"code": 500, "message": str(exc)})
                    return
                owner.update_session(
                    session_id,
                    {
                        "phone": phone,
                        "device_id": _safe_str(data.get("deviceId")),
                        "updated_at": time.time(),
                    },
                )
                self._send_json({"code": 0, "message": "验证码已发送", "data": data})

            def _handle_session(self, session_id: str) -> None:
                session = owner.get_session(session_id)
                if not session:
                    self._send_json({"code": 404, "message": "登录链接不存在或已过期"})
                    return
                payload = self._read_json()
                phone = _safe_str(payload.get("phone") or session.get("phone"))
                captcha = _safe_str(payload.get("captcha"))
                if not PHONE_RE.fullmatch(phone):
                    self._send_json({"code": 400, "message": "请输入正确的 11 位手机号"})
                    return
                if not CAPTCHA_RE.fullmatch(captcha):
                    self._send_json({"code": 400, "message": "请输入正确的验证码"})
                    return
                try:
                    data = owner.create_session(
                        phone=phone,
                        captcha=captcha,
                        device_id=_safe_str(session.get("device_id")),
                        platform_id=_safe_str(session.get("platform_id")),
                        platform_user_id=_safe_str(session.get("platform_user_id")),
                    )
                    profile = owner.get_profile(_safe_str(data.get("fwt")))
                except Exception as exc:
                    self._send_json({"code": 500, "message": str(exc)})
                    return

                account = {
                    "framework_token": _safe_str(data.get("fwt")),
                    "fwt": _safe_str(data.get("fwt")),
                    "username": _safe_str(data.get("username") or profile.get("nickname")),
                    "nickname": _safe_str(profile.get("nickname") or data.get("username")),
                    "tjd_uid": _safe_str(data.get("tjdUid") or profile.get("uid")),
                    "tgd_uid": _safe_str(data.get("tgdUid") or data.get("tjdUid") or profile.get("uid")),
                    "avatar": _safe_str(profile.get("avatar")),
                    "introduce": _safe_str(profile.get("introduce")),
                    "device_id": _safe_str(data.get("deviceId") or session.get("device_id")),
                    "platform_id": _safe_str(data.get("platformId") or session.get("platform_id")),
                    "platform_user_id": _safe_str(
                        data.get("platformUserId") or session.get("platform_user_id")
                    ),
                    "is_primary": True,
                }
                owner.update_session(
                    session_id,
                    {
                        "phone": phone,
                        "device_id": account["device_id"],
                        "account": account,
                        "completed": True,
                        "updated_at": time.time(),
                    },
                )
                owner.finish_session(session_id)
                self._send_json(
                    {
                        "code": 0,
                        "message": "登录成功",
                        "data": {
                            "username": account["username"],
                            "tjdUid": account["tjd_uid"],
                        },
                    }
                )

        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), RequestHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="TaJiDuoWebLoginServer",
            daemon=True,
        )
        self._thread.start()
        self.logger.info(f"[TaJiDuo] web login server started on :{self.port}")

    def close(self) -> None:
        if self._server is None:
            return
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass
        self._server = None
        self._thread = None

    def get_session(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._sessions.get(session_id) or {})

    def update_session(self, session_id: str, fields: Dict[str, Any]) -> None:
        with self._lock:
            if session_id not in self._sessions:
                return
            self._sessions[session_id].update(fields)

    def create_login_session(self, payload: Dict[str, Any]) -> Dict[str, str]:
        session_id = secrets.token_urlsafe(8)
        session = dict(payload)
        session["id"] = session_id
        session["created_at"] = time.time()
        session["updated_at"] = time.time()
        with self._lock:
            self._sessions[session_id] = session
        base = self.public_link or f"http://127.0.0.1:{self.port}"
        return {"id": session_id, "url": f"{base.rstrip('/')}/login/{session_id}"}

    def purge_expired(self, max_age_seconds: int = 600) -> None:
        now = time.time()
        with self._lock:
            expired = [
                session_id
                for session_id, session in self._sessions.items()
                if now - float(session.get("updated_at") or session.get("created_at") or now)
                >= max_age_seconds
            ]
            for session_id in expired:
                self._sessions.pop(session_id, None)

    def finish_session(self, session_id: str) -> None:
        with self._lock:
            session = dict(self._sessions.get(session_id) or {})
        if not session:
            return
        try:
            self.on_login(session)
        except Exception as exc:
            self.logger.error(f"[TaJiDuo] web login callback failed: {exc}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_data: Optional[Dict[str, Any]] = None,
        fwt: str = "",
        platform_id: str = "",
        platform_user_id: str = "",
    ) -> Dict[str, Any]:
        headers: Dict[str, str] = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if fwt:
            headers["X-Framework-Token"] = fwt
        if platform_id:
            headers["X-Platform-Id"] = platform_id
        if platform_user_id:
            headers["X-Platform-User-Id"] = platform_user_id

        with httpx.Client(timeout=self.timeout_ms / 1000, follow_redirects=True) as client:
            response = client.request(
                method=method,
                url=f"{self.base_url}{path}",
                headers=headers,
                json={k: v for k, v in (json_data or {}).items() if v not in ("", None)},
            )
        try:
            body = response.json()
        except ValueError:
            body = {"message": response.text}

        if response.status_code >= 400:
            raise RuntimeError(str(body.get("message") or f"HTTP {response.status_code}"))
        if isinstance(body, dict) and "code" in body:
            if body.get("code") != 0:
                raise RuntimeError(str(body.get("message") or f"业务错误 {body.get('code')}"))
            return dict(body.get("data") or {})
        return dict(body or {})

    def send_captcha(self, phone: str) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/login/tajiduo/captcha/send",
            json_data={"phone": phone, "deviceId": secrets.token_hex(16)},
        )

    def create_session(
        self,
        *,
        phone: str,
        captcha: str,
        device_id: str,
        platform_id: str,
        platform_user_id: str,
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/login/tajiduo/session",
            json_data={"phone": phone, "captcha": captcha, "deviceId": device_id},
            platform_id=platform_id,
            platform_user_id=platform_user_id,
        )

    def get_profile(self, fwt: str) -> Dict[str, Any]:
        if not fwt:
            return {}
        try:
            return self._request(
                "GET",
                "/api/v1/login/tajiduo/profile",
                fwt=fwt,
            )
        except Exception:
            return {}
