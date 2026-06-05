# PRD-2: HTTP Security Hardening Middleware

## 1. Problem & threat
`backend/main.py:139` is the ONLY middleware on the app:

```python
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
```

`allow_origins=["*"]` + `allow_credentials=True` is invalid per the Fetch spec and
dangerous (it tells browsers to permit credentialed cross-origin reads from any
site). There are NO security headers (HSTS, CSP, X-Frame-Options, X-Content-Type-
Options, Referrer-Policy, Permissions-Policy), no HTTP→HTTPS redirect, no host
allowlist, and no global rate limiting. Automated scanners (Mozilla Observatory,
OWASP ZAP) — which hospital reviews routinely run — fail this instantly, and it
breaks §164.312(e) (transmission security) + HECVAT/SIG application-security items.

## 2. Goal / definition of done
- CORS restricted to an explicit origin allowlist.
- Security headers on every response.
- HTTPS enforced + host allowlist in production (no-op in local dev).
- Global + per-endpoint rate limiting on auth/code/OTP surfaces.
- A new test asserting headers + CORS behavior.
- Mozilla Observatory grade B+ or better on the deployed app.

## 3. CORS lockdown (replace main.py:139)
```python
_origins_env = os.getenv("ALLOWED_ORIGINS", "").strip()
if _origins_env:
    ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(",") if o.strip()]
else:
    ALLOWED_ORIGINS = list(filter(None, {
        os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"),
        os.getenv("LANDING_URL", "http://localhost:5173").rstrip("/"),
    }))

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Patient-Session"],
    max_age=600,
)
```
Because the landing app (cross-origin) calls the API with credentials (PRD-1 cookies
/ staff bearer), the landing origin MUST be in `ALLOWED_ORIGINS` in prod.

## 4. Security headers middleware
Add a `BaseHTTPMiddleware` AFTER CORS so headers land on all responses (including
errors). Keep it dependency-free.

```python
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=(self)",
    # microphone=self because the voice companion records the patient
}

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        if os.getenv("ENV") == "production":
            resp.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=63072000; includeSubDomains; preload")
        csp = _build_csp()
        header = ("Content-Security-Policy-Report-Only"
                  if os.getenv("CSP_REPORT_ONLY", "1") == "1"
                  else "Content-Security-Policy")
        resp.headers.setdefault(header, csp)
        return resp

app.add_middleware(SecurityHeadersMiddleware)
```

## 5. Content-Security-Policy (start in report-only)
The frontend uses heavy INLINE scripts/styles (the injected `window.__PATIENT__`
script in the page handlers, the inline survey-page script, inline `style=` attrs
throughout) and loads external media (ElevenLabs audio, Tavus avatar iframe). A
strict nonce CSP would break the UI on day one. Plan:

**Phase 1 (this PRD): report-only, permissive-but-bounded:**
```python
def _build_csp() -> str:
    return "; ".join([
        "default-src 'self'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "img-src 'self' data: blob:",
        "style-src 'self' 'unsafe-inline'",
        # 'unsafe-inline' for script is a TEMPORARY allowance removed in phase 2
        # via nonces; keep report-only until then.
        "script-src 'self' 'unsafe-inline'",
        "font-src 'self' data:",
        "media-src 'self' blob: https:",
        "connect-src 'self' https:",
        "frame-src 'self' https:",
    ])
```

**Phase 2 (follow-up issue, NOT this PRD):** replace `'unsafe-inline'` for scripts
with per-response nonces injected into the inline `<script>` tags in the page
handlers; narrow `media-src`/`connect-src`/`frame-src` to exact vendor hosts (verify
Tavus host, e.g. `*.tavus.io`, and the ElevenLabs media host); flip
`CSP_REPORT_ONLY=0`. Document the vendor host list in `docs/security/CSP.md`.

## 6. HTTPS + host allowlist (production only)
```python
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

if os.getenv("ENV") == "production":
    hosts = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()]
    if hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    app.add_middleware(HTTPSRedirectMiddleware)
```
**Caveat:** the app runs behind Railway/Render TLS termination, so it sees HTTP with
`X-Forwarded-Proto: https`. Update the Dockerfile CMD to run uvicorn with
`--proxy-headers --forwarded-allow-ips='*'` so `HTTPSRedirectMiddleware` doesn't
loop. Verify platform health checks still pass after enabling.

## 7. Rate limiting
Generalize the sliding-window pattern in `eligibility/store.py:rate_limit_check`
into `backend/ratelimit.py: allow(bucket_key, max, window_sec)`, or add `slowapi`.
Apply a `rate_limit(scope, max, window)` dependency keyed on client IP (respect the
first `X-Forwarded-For` hop behind the proxy).

| Scope | Limit |
|---|---|
| Global default | 120 req / 60s / IP |
| `POST /api/patient/by-codes` (PRD-1) | 10 / 60s / IP |
| `POST /api/auth/login` (~1664) | 10 / 60s / IP |
| `POST /admin/auth/login` (routers/admin.py) | 10 / 60s / IP |
| `POST /api/onboarding/request-otp` (routers/onboarding.py) | 5 / 60s / IP + 20/day/email |
| `GET /survey`, `POST /api/survey/submit` | 30 / 60s / IP |

On limit exceeded: return 429 with `Retry-After`. Do not leak which field failed.

## 8. Startup secret guard (optional here; can live in PRD-3)
In the existing `@app.on_event("startup")` (main.py ~5291), before scheduler setup:
```python
if os.getenv("ENV") == "production":
    weak = []
    for name, default in [("AUTH_SECRET", "change-me-in-production-elysium"),
                          ("INTERNAL_TOOL_SECRET", "change-me-internal-secret"),
                          ("ADMIN_PASSWORD", "change-me-strong-password")]:
        v = os.getenv(name, "")
        if not v or v == default or len(v) < 32:
            weak.append(name)
    if weak:
        raise RuntimeError(f"Refusing to start: weak/default secrets: {weak}")
```

## 9. Env vars (document in `.env.example`)
```
ENV=production|development        # gates HSTS/HTTPS/TrustedHost/secret-guard
ALLOWED_ORIGINS=https://app.archangelhealth.ai,https://archangelhealth.ai
ALLOWED_HOSTS=app.archangelhealth.ai,archangelhealth.ai
CSP_REPORT_ONLY=1                 # flip to 0 in phase 2
```

## 10. Test plan (`backend/tests/test_security_headers.py`)
1. `GET /` and `GET /api/patients`: assert `X-Frame-Options=DENY`,
   `X-Content-Type-Options=nosniff`, `Referrer-Policy`, `Permissions-Policy` present.
2. With `ENV=production` (monkeypatch): HSTS present; without it: absent.
3. CSP header present (report-only key when `CSP_REPORT_ONLY=1`).
4. CORS: preflight OPTIONS with Origin not in allowlist → no
   `access-control-allow-origin` echoed; allowlisted origin → echoed.
5. Rate limit: 11 rapid calls to `/api/patient/by-codes` from one IP → 11th is 429
   with `Retry-After`.
6. Secret guard: startup with `ENV=production` + default `AUTH_SECRET` raises.
7. Local dev (`ENV` unset): no HTTPS redirect, app serves over http (regression).

Full suite green: `cd backend && python3 -m pytest tests/ -q`.

## 11. Rollout / safety
- Ship CSP in REPORT-ONLY first; watch reports/console for a week before enforcing
  (phase 2).
- Enable HTTPSRedirect + TrustedHost only after confirming `--proxy-headers` is set
  and the platform health check uses an allowed host.
- Keep local dev fully functional with `ENV` unset (every prod-only middleware is
  guarded by the `ENV` check).

## 12. Out of scope
- Nonce-based strict CSP (phase 2 follow-up).
- WAF / DDoS protection (platform/Cloudflare layer).
- MFA, session lifecycle (PRD-3).
