
```
agentic-multimodal-tech-agent/
├── app.py                  ← tools + Starlette assembly + entrypoint
└── lib_mcp_auth/
    ├── __init__.py         ← re-exports middleware + all route handlers
    ├── config.py           ← all env vars, derived constants, UNPROTECTED_PATHS
    ├── token.py            ← get_jwks() + verify_token()  (imports from config)
    ├── middleware.py       ← BearerAuthMiddleware          (imports from config + token)
    └── routes.py           ← all 5 HTTP handlers           (imports from config)
```
