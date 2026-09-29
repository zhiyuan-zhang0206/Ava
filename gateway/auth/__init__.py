"""Who is calling: request principals, server-side browser sessions, the
browser-origin and cookie policy, scoped webhook authentication, and the
rejected-authentication log.

Package door — no imports, no re-exports; callers use the modules below:

  - `router.py`            — `/api/auth/*` login, logout, check, session listing + revocation
  - `request_principal.py` — the credential a request carries and what it may do
  - `session_store.py`     — opaque server-side `web_sessions` rows
  - `cors.py`              — exact-origin CORS policy + the Secure-cookie decision
  - `webhook.py`           — scoped webhook authentication (alert + work-failed ingest)
  - `rejection_log.py`     — severity routing + throttling for rejected authentication
"""
