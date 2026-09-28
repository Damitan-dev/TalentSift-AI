"""HTTP/WS access boundary and the small recruiter/invitation UI."""
import asyncio
import hmac
import json
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.requests import HTTPConnection

from access_control import AccessError, digest, verify_password


class AccessMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        connection = HTTPConnection(scope)
        store = scope["app"].state.access
        settings = store.settings
        path = scope["path"]
        is_ws = scope["type"] == "websocket"

        async def protected_send(message):
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", []))
                headers.update({b"cache-control": b"no-store", b"referrer-policy": b"same-origin",
                                b"x-content-type-options": b"nosniff", b"x-frame-options": b"DENY",
                                b"permissions-policy": b"microphone=(self), camera=()"})
                # Preserve repeated Set-Cookie headers.
                cookies = [(k, v) for k, v in message.get("headers", []) if k == b"set-cookie"]
                headers.pop(b"set-cookie", None)
                message["headers"] = list(headers.items()) + cookies
            await send(message)

        try:
            # Health is safe for a host's internal health probe, with no data access.
            if path != "/health":
                settings.origin(connection)
            if is_ws or scope["method"] not in ("GET", "HEAD", "OPTIONS"):
                settings.check_origin(connection)
            if is_ws:
                grant = await asyncio.to_thread(store.get_session,
                    connection.cookies.get(settings.cookie_name("candidate")), "candidate")
                if not grant:
                    raise AccessError("Open your invitation link before starting the interview.")
                scope.setdefault("state", {})["candidate_access"] = grant
                if path.startswith("/ws/recording/"):
                    await asyncio.to_thread(store.authorize_recording, grant["invitation_id"], path.rsplit("/", 1)[-1])
                return await self.app(scope, receive, protected_send)

            protected = path == "/recruiter" or path.startswith("/recruiter/") or path.startswith("/api/jobs/") or path == "/logout"
            kind = "recruiter" if protected else "login" if path == "/login" else "candidate" if path in ("/api/session", "/api/invitation") else None
            access = None
            if kind:
                access = await asyncio.to_thread(store.get_session,
                    connection.cookies.get(settings.cookie_name(kind)), kind)
                if not access and protected:
                    if scope["method"] == "GET" and not path.startswith("/api/"):
                        return await RedirectResponse("/login", status_code=303)(scope, receive, protected_send)
                    raise AccessError("Please sign in as the recruiter.", 401)
                if not access and kind == "candidate":
                    raise AccessError("Open your personal invitation link to continue.", 401)
                scope.setdefault("state", {})["access"] = access

            if scope["method"] not in ("GET", "HEAD", "OPTIONS"):
                request = Request(scope, receive)
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 16384:
                        raise AccessError("Request is too large.", 413)
                content_type = connection.headers.get("content-type", "").split(";", 1)[0]
                try:
                    fields = parse_qs(body.decode("utf-8")) if content_type == "application/x-www-form-urlencoded" else {}
                except UnicodeDecodeError:
                    raise AccessError("Invalid form encoding.", 400) from None
                if kind:
                    supplied = connection.headers.get("x-csrf-token") or fields.get("csrf_token", [""])[0]
                    if not access or not hmac.compare_digest(supplied.encode(), access["csrf"].encode()):
                        raise AccessError("This form has expired. Reload the page and try again.")
                delivered = False
                original_receive = receive
                async def replay():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await original_receive()
                receive = replay
            await self.app(scope, receive, protected_send)
        except AccessError as error:
            if is_ws:
                # Reject before opening OpenAI or exposing any session details.
                await send({"type": "websocket.close", "code": 1008, "reason": "Interview access denied"})
            else:
                await JSONResponse({"detail": str(error)}, status_code=error.status)(scope, receive, protected_send)


def install_access_routes(app, templates):
    def cookie(response, settings, kind, token, lifetime):
        response.set_cookie(settings.cookie_name(kind), token, max_age=lifetime,
                            secure=bool(settings.public_url), httponly=True, samesite="lax", path="/")

    def clear_cookie(response, settings, kind):
        response.delete_cookie(settings.cookie_name(kind), path="/", secure=bool(settings.public_url), httponly=True, samesite="lax")

    def client_key(request):
        # Do not trust caller-supplied X-Forwarded-For. Configure trusted proxies in Uvicorn.
        return digest(request.client.host if request.client else "unknown")

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        store = app.state.access
        settings = store.settings
        recruiter = await asyncio.to_thread(store.get_session, request.cookies.get(settings.cookie_name("recruiter")), "recruiter")
        if recruiter:
            return RedirectResponse("/recruiter/jobs", status_code=303)
        await asyncio.to_thread(store.rate_limit, "login-page:" + client_key(request), 60, 60)
        ready = bool(await asyncio.to_thread(store.account))
        token, csrf, lifetime = await asyncio.to_thread(store.issue_session, "login", None, request.cookies.get(settings.cookie_name("login")))
        response = templates.TemplateResponse(request=request, name="login.html", context={"csrf_token": csrf, "ready": ready, "error": None}, status_code=200 if ready else 503)
        cookie(response, settings, "login", token, lifetime)
        return response

    @app.post("/login")
    async def login(request: Request):
        store = app.state.access
        await asyncio.to_thread(store.rate_limit, "login:" + client_key(request), 10, 900)
        await asyncio.to_thread(store.rate_limit, "login-global", 30, 60)
        form = await request.form()
        account = await asyncio.to_thread(store.account)
        password = str(form.get("password", ""))
        username = str(form.get("username", "")).strip().casefold()
        verified = account and await asyncio.to_thread(verify_password, password, account["password_hash"])
        if not verified or not hmac.compare_digest(username.encode(), account["username"].encode()):
            return templates.TemplateResponse(request=request, name="login.html", context={
                "csrf_token": request.state.access["csrf"], "ready": bool(account),
                "error": "The username or password is incorrect."}, status_code=401)
        await asyncio.to_thread(store.logout, request.cookies.get(store.settings.cookie_name("login")))
        token, _, lifetime = await asyncio.to_thread(store.issue_session, "recruiter", None, request.cookies.get(store.settings.cookie_name("recruiter")))
        response = RedirectResponse("/recruiter/jobs", status_code=303)
        cookie(response, store.settings, "recruiter", token, lifetime)
        clear_cookie(response, store.settings, "login")
        return response

    @app.post("/logout")
    async def logout(request: Request):
        store = app.state.access
        await asyncio.to_thread(store.logout, request.cookies.get(store.settings.cookie_name("recruiter")))
        response = RedirectResponse("/login", status_code=303)
        clear_cookie(response, store.settings, "recruiter")
        return response

    @app.post("/recruiter/job/{job_id}/invitations")
    async def create_invitation(request: Request, job_id: str):
        store = app.state.access
        form = await request.form()
        try:
            hours = int(form.get("expires_hours", "72"))
        except (TypeError, ValueError):
            raise AccessError("Select a valid invitation expiry.", 422)
        await asyncio.to_thread(store.rate_limit, "invitation-create", 50, 60)
        _, token = await asyncio.to_thread(store.create_invitation, job_id, str(form.get("candidate_name", "")), hours)
        # Fragment is never sent in access logs or HTTP Referer; exchange it via POST.
        url = store.settings.origin(request) + "/#invite=" + token
        return templates.TemplateResponse(request=request, name="invitation_created.html", context={
            "invite_url": url, "candidate_name": " ".join(str(form.get("candidate_name", "")).split()), "job_id": job_id})

    @app.post("/recruiter/job/{job_id}/invitations/{invitation_id}/revoke")
    async def revoke_invitation(request: Request, job_id: str, invitation_id: str):
        await asyncio.to_thread(app.state.access.revoke, invitation_id, job_id)
        return RedirectResponse(f"/recruiter/job/{job_id}", status_code=303)

    @app.post("/api/invitations/exchange")
    async def exchange(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise AccessError("Send an invitation request as JSON.", 415)
        store = app.state.access
        await asyncio.to_thread(store.rate_limit, "exchange:" + client_key(request), 30, 60)
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AccessError("Invalid invitation request.", 400)
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            raise AccessError("This invitation is invalid.", 400)
        invitation = await asyncio.to_thread(store.invitation, token=token)
        secret, csrf, lifetime = await asyncio.to_thread(store.issue_session, "candidate", invitation["id"], request.cookies.get(store.settings.cookie_name("candidate")))
        response = JSONResponse({"job_id": invitation["job_id"], "title": invitation["title"],
                                 "candidate_name": invitation["candidate_name"], "csrf_token": csrf})
        cookie(response, store.settings, "candidate", secret, lifetime)
        return response

    @app.get("/api/invitation")
    async def invitation_details(request: Request):
        grant = request.state.access
        invite = await asyncio.to_thread(app.state.access.invitation, invitation_id=grant["invitation_id"])
        return {"job_id": invite["job_id"], "title": invite["title"],
                "candidate_name": invite["candidate_name"], "csrf_token": grant["csrf"]}

    @app.exception_handler(AccessError)
    async def access_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=error.status)

    app.add_middleware(AccessMiddleware)
