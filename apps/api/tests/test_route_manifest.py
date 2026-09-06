"""Frozen route manifest for the API surface.

Every route is pinned as ``(path, methods, endpoint name)`` so any accidental
addition, removal, rename or method change fails loudly.  This test exists to
make the main.py split a reviewable, verifiable move: the physical layout of
routers may change, the public surface may not.

Regenerate the literal below only when a route change is *intentional*.
"""

from app.main import app

ROUTE_MANIFEST: list[tuple[str, tuple[str, ...], str]] = [
    ('/api/v1/ai/cluster-feedback', ('POST',), 'ai_cluster_feedback'),
    ('/api/v1/ai/distill-interview', ('POST',), 'ai_distill_interview'),
    ('/api/v1/ai/draft-document', ('POST',), 'ai_draft_document'),
    ('/api/v1/ai/frame-problem', ('POST',), 'ai_frame_problem'),
    ('/api/v1/ai/interpret', ('POST',), 'ai_interpret'),
    ('/api/v1/ai/propose-solutions', ('POST',), 'ai_propose_solutions'),
    ('/api/v1/ai/usage', ('GET',), 'ai_usage'),
    ('/api/v1/analysis-artifacts/{artifact_id}', ('GET',), 'get_artifact'),
    ('/api/v1/analysis-runs', ('GET',), 'list_analysis_runs'),
    ('/api/v1/analysis-runs', ('POST',), 'create_analysis'),
    ('/api/v1/analysis-runs/validate-config', ('POST',), 'validate_analysis_config'),
    ('/api/v1/analysis-runs/{run_id}', ('GET',), 'get_analysis'),
    ('/api/v1/analysis-runs/{run_id}/artifacts', ('GET',), 'list_artifacts'),
    ('/api/v1/analysis-runs/{run_id}/rerun', ('POST',), 'rerun_analysis'),
    ('/api/v1/approval-requests', ('GET',), 'list_approvals'),
    ('/api/v1/approval-requests/{approval_id}/approve', ('POST',), 'approve'),
    ('/api/v1/approval-requests/{approval_id}/reject', ('POST',), 'reject'),
    ('/api/v1/audit-logs', ('GET',), 'list_audit_logs'),
    ('/api/v1/auth/login', ('POST',), 'login'),
    ('/api/v1/auth/refresh', ('POST',), 'refresh'),
    ('/api/v1/auth/register', ('POST',), 'register'),
    ('/api/v1/auto-reports/{report_id}', ('GET',), 'get_auto_report'),
    ('/api/v1/auto-reports/{report_id}/confirm', ('POST',), 'confirm_auto_report'),
    ('/api/v1/auto-reports/{report_id}/narrate', ('POST',), 'narrate_auto_report'),
    ('/api/v1/copilot/runs/{run_id}', ('GET',), 'get_copilot_run'),
    ('/api/v1/copilot/runs/{run_id}/events', ('GET',), 'copilot_events'),
    ('/api/v1/copilot/sessions', ('POST',), 'create_copilot_session'),
    ('/api/v1/copilot/sessions/{session_id}', ('GET',), 'get_copilot_session'),
    ('/api/v1/copilot/sessions/{session_id}/messages', ('POST',), 'copilot_message'),
    ('/api/v1/dataset-versions/{version_id}', ('GET',), 'get_dataset_version'),
    ('/api/v1/dataset-versions/{version_id}/preview', ('GET',), 'preview_dataset'),
    ('/api/v1/dataset-versions/{version_id}/quality-report', ('GET',), 'quality_report'),
    ('/api/v1/dataset-versions/{version_id}/report-narration', ('POST',), 'report_narration'),
    ('/api/v1/dataset-versions/{version_id}/schema', ('GET',), 'get_dataset_version_schema'),
    ('/api/v1/dataset-versions/{version_id}/schema', ('PATCH',), 'patch_schema'),
    ('/api/v1/dataset-versions/{version_id}/schema-review', ('POST',), 'mark_schema_reviewed'),
    ('/api/v1/datasets', ('GET',), 'list_datasets'),
    ('/api/v1/datasets/upload', ('POST',), 'upload_dataset'),
    ('/api/v1/datasets/upload-batch', ('POST',), 'upload_dataset_batch'),
    ('/api/v1/datasets/{dataset_id}', ('DELETE',), 'delete_dataset'),
    ('/api/v1/datasets/{dataset_id}', ('GET',), 'get_dataset'),
    ('/api/v1/datasets/{dataset_id}/versions', ('GET',), 'list_dataset_versions'),
    ('/api/v1/datasets/{dataset_id}/versions/{version_id}/schema', ('GET',), 'get_dataset_schema'),
    ('/api/v1/decision-proposals', ('GET',), 'list_decisions'),
    ('/api/v1/decision-proposals', ('POST',), 'create_decision'),
    ('/api/v1/decision-proposals/{proposal_id}', ('GET',), 'get_decision'),
    ('/api/v1/decision-proposals/{proposal_id}', ('PATCH',), 'patch_decision'),
    ('/api/v1/decision-proposals/{proposal_id}/submit', ('POST',), 'submit_decision'),
    ('/api/v1/discussions', ('GET',), 'list_discussions'),
    ('/api/v1/documents', ('GET',), 'list_documents'),
    ('/api/v1/documents', ('POST',), 'create_document'),
    ('/api/v1/documents/generate', ('POST',), 'generate_document'),
    ('/api/v1/documents/{document_id}', ('GET',), 'get_document'),
    ('/api/v1/documents/{document_id}/export', ('GET',), 'export_document'),
    ('/api/v1/documents/{document_id}/submit', ('POST',), 'submit_document'),
    ('/api/v1/documents/{document_id}/versions', ('GET',), 'list_document_versions'),
    ('/api/v1/documents/{document_id}/versions', ('POST',), 'create_document_version'),
    ('/api/v1/feedback-clusters', ('GET',), 'list_feedback_clusters'),
    ('/api/v1/feedback-clusters/generate', ('POST',), 'generate_feedback_clusters'),
    ('/api/v1/feedback-clusters/{cluster_id}', ('PATCH',), 'patch_feedback_cluster'),
    ('/api/v1/feedback-clusters/{cluster_id}/link-task', ('POST',), 'link_cluster_task'),
    ('/api/v1/feedback-imports', ('GET',), 'list_feedback_imports'),
    ('/api/v1/feedback-items', ('GET',), 'list_feedback'),
    ('/api/v1/feedback-items', ('POST',), 'create_feedback'),
    ('/api/v1/feedback-items/import', ('POST',), 'import_feedback'),
    ('/api/v1/feedback-items/{feedback_id}', ('PATCH',), 'patch_feedback'),
    ('/api/v1/feedback-notes', ('GET',), 'list_feedback_notes'),
    ('/api/v1/feedback-notes', ('POST',), 'create_feedback_note'),
    ('/api/v1/feedback-notes/{note_id}', ('PATCH',), 'patch_feedback_note'),
    ('/api/v1/insights', ('GET',), 'list_insights'),
    ('/api/v1/insights', ('POST',), 'create_insight'),
    ('/api/v1/insights/{insight_id}', ('GET',), 'get_insight'),
    ('/api/v1/insights/{insight_id}', ('PATCH',), 'patch_insight'),
    ('/api/v1/interview-questions', ('GET',), 'list_interview_questions'),
    ('/api/v1/interview-questions', ('POST',), 'add_manual_question'),
    ('/api/v1/interview-questions/{question_id}', ('PATCH',), 'patch_interview_question'),
    ('/api/v1/jobs/{job_id}', ('GET',), 'get_job'),
    ('/api/v1/jobs/{job_id}/cancel', ('POST',), 'cancel_job'),
    ('/api/v1/jobs/{job_id}/retry', ('POST',), 'retry_job'),
    ('/api/v1/me', ('GET',), 'me'),
    ('/api/v1/metrics', ('GET',), 'list_metrics_alias'),
    ('/api/v1/metrics', ('POST',), 'create_metric_alias'),
    ('/api/v1/metrics/{metric_id}', ('DELETE',), 'delete_metric_alias'),
    ('/api/v1/metrics/{metric_id}', ('PATCH',), 'patch_metric_alias'),
    ('/api/v1/problems', ('GET',), 'list_problems'),
    ('/api/v1/problems', ('POST',), 'create_problem'),
    ('/api/v1/problems/{problem_id}', ('GET',), 'get_problem'),
    ('/api/v1/problems/{problem_id}', ('PATCH',), 'patch_problem'),
    ('/api/v1/problems/{problem_id}/solutions', ('GET',), 'list_solutions'),
    ('/api/v1/problems/{problem_id}/solutions', ('POST',), 'create_solution'),
    ('/api/v1/projects', ('GET',), 'list_projects'),
    ('/api/v1/projects', ('POST',), 'create_project'),
    ('/api/v1/projects/{project_id}', ('DELETE',), 'delete_project'),
    ('/api/v1/projects/{project_id}', ('GET',), 'get_project'),
    ('/api/v1/projects/{project_id}', ('PATCH',), 'patch_project'),
    ('/api/v1/projects/{project_id}/archive', ('POST',), 'archive_project'),
    ('/api/v1/projects/{project_id}/auto-report', ('POST',), 'generate_auto_report'),
    ('/api/v1/projects/{project_id}/auto-report/compute', ('POST',), 'compute_auto_report'),
    ('/api/v1/projects/{project_id}/auto-reports', ('GET',), 'list_auto_reports'),
    ('/api/v1/projects/{project_id}/interview/complete', ('POST',), 'interview_complete'),
    ('/api/v1/projects/{project_id}/interview/next-question', ('POST',), 'interview_next_question'),
    ('/api/v1/projects/{project_id}/overview', ('GET',), 'project_overview'),
    ('/api/v1/projects/{project_id}/tasks', ('GET',), 'list_tasks'),
    ('/api/v1/projects/{project_id}/tasks', ('POST',), 'create_task'),
    ('/api/v1/projects/{project_id}/unarchive', ('POST',), 'unarchive_project'),
    ('/api/v1/projects/{project_id}/workflow-status', ('GET',), 'project_workflow_status'),
    ('/api/v1/settings', ('GET',), 'get_settings_alias'),
    ('/api/v1/settings', ('PATCH',), 'patch_settings_alias'),
    ('/api/v1/solutions', ('GET',), 'list_all_solutions'),
    ('/api/v1/solutions/{solution_id}', ('PATCH',), 'patch_solution'),
    ('/api/v1/solutions/{solution_id}/select', ('POST',), 'select_solution'),
    ('/api/v1/tasks/{task_id}', ('DELETE',), 'delete_task'),
    ('/api/v1/tasks/{task_id}', ('GET',), 'get_task'),
    ('/api/v1/tasks/{task_id}', ('PATCH',), 'patch_task'),
    ('/api/v1/tasks/{task_id}/links', ('GET',), 'list_task_links'),
    ('/api/v1/tasks/{task_id}/links', ('POST',), 'link_task'),
    ('/api/v1/tasks/{task_id}/links/{link_id}', ('DELETE',), 'unlink_task'),
    ('/api/v1/workspaces', ('GET',), 'list_workspaces'),
    ('/api/v1/workspaces/{workspace_id}', ('PATCH',), 'patch_workspace'),
    ('/api/v1/workspaces/{workspace_id}/members', ('GET',), 'list_members'),
    ('/api/v1/workspaces/{workspace_id}/members', ('POST',), 'add_member'),
    ('/api/v1/workspaces/{workspace_id}/members/{member_id}', ('PATCH',), 'patch_member'),
    ('/api/v1/workspaces/{workspace_id}/metrics', ('GET',), 'list_metric_definitions'),
    ('/api/v1/workspaces/{workspace_id}/metrics', ('POST',), 'create_metric_definition'),
    ('/api/v1/workspaces/{workspace_id}/metrics/{metric_id}', ('DELETE',), 'delete_metric_definition'),
    ('/api/v1/workspaces/{workspace_id}/metrics/{metric_id}', ('PATCH',), 'patch_metric_definition'),
    ('/api/v1/workspaces/{workspace_id}/settings', ('GET',), 'get_workspace_settings'),
    ('/api/v1/workspaces/{workspace_id}/settings', ('PATCH',), 'patch_workspace_settings'),
    ('/docs', ('GET', 'HEAD'), 'swagger_ui_html'),
    ('/docs/oauth2-redirect', ('GET', 'HEAD'), 'swagger_ui_redirect'),
    ('/health', ('GET',), 'health'),
    ('/health/ai', ('GET',), 'health_ai'),
    ('/health/ready', ('GET',), 'health_ready'),
    ('/openapi.json', ('GET', 'HEAD'), 'openapi'),
    ('/redoc', ('GET', 'HEAD'), 'redoc_html'),
]




def _live_manifest() -> list[tuple[str, tuple[str, ...], str]]:
    rows = []
    for route in app.routes:
        methods = tuple(sorted(route.methods)) if getattr(route, "methods", None) else ()
        rows.append((route.path, methods, route.name))
    return sorted(rows)


def test_routes_match_the_frozen_manifest():
    live = _live_manifest()
    assert live == ROUTE_MANIFEST
    # Redundant by construction, but kept explicit: the total route count must
    # match the frozen list, so a route can never quietly appear or disappear.
    assert len(app.routes) == len(ROUTE_MANIFEST)
    paths = {path for path, _methods, _name in ROUTE_MANIFEST}
    for expected in ("/health", "/health/ready", "/health/ai"):
        assert expected in paths
    for path, _methods, _name in ROUTE_MANIFEST:
        assert (
            path.startswith("/api/")
            or path in {"/health", "/health/ready", "/health/ai"}
            or path in {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
        )
