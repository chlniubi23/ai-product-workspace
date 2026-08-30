# AI Product Workspace API

The API is a FastAPI application served from `app.main:app`. It uses SQLAlchemy models and creates the schema on startup for local development. Set `DATABASE_URL` to a MySQL URL for the documented deployment; when omitted it falls back to SQLite at `data/app.db`.

```powershell
cd apps/api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
uvicorn app.main:app --reload --port 8000
```

OpenAPI is available at <http://localhost:8000/docs>. Health endpoints are `/health`, `/health/ready`, and `/health/ai`; business routes are under `/api/v1`.

## Security-sensitive endpoints

- `DELETE /api/v1/datasets/{dataset_id}` requires a workspace `owner` and an
  explicit confirmation. Send either `?confirm={dataset_id}` (used by the web
  client), `?confirm=true`, or a JSON body such as `{"confirm": "{dataset_id}"}`.
- `GET /api/v1/audit-logs` is read-only and requires workspace membership.
  Supply `workspace_id`, with optional `target_type`, `target_id`, `page`, and
  `page_size` query parameters. Results are newest-first and scoped to that
  workspace.

Registration, successful login, and failed login for known accounts create
workspace-scoped audit events. Failed login responses always use the same
generic credentials error and do not disclose whether an email is registered.
