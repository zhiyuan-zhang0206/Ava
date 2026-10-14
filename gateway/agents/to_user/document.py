"""Read the workspace document through its home machine; never copy it to the DB."""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from base.agents.to_user import ToUserDocument
from gateway.agents.eval_guard import deny_isolated_result_read
from ops.cluster.rpc import ClusterOpFailed, ClusterOpUnreachable, dispatch_to_machine

router = APIRouter()

# Let the authenticated shell initiate frame navigation. A navigation originating
# in an opaque sandbox otherwise loses SameSite cookies on private Ava links.
# This bridge carries only a URL; it never receives credentials or replies.
NAVIGATION_BRIDGE = """<script>
document.addEventListener('click', function(event) {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey ||
      event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = event.target.closest && event.target.closest('a[href]');
  if (!link || link.hasAttribute('download')) return;
  const href = link.getAttribute('href');
  if (href.startsWith('#')) return;
  const url = new URL(link.href);
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return;
  if (window.parent === window) return;
  event.preventDefault();
  window.parent.postMessage({type: 'ava:to-user:navigate', href: url.href}, '*');
});
</script>"""


def home_machine(request: Request, agent_id: int) -> str:
    deny_isolated_result_read(request)
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "Agent not found")
    return row[0]


@router.get("/api/agents/{agent_id}/to-user")
async def get_to_user(agent_id: int, request: Request) -> ToUserDocument:
    machine = await asyncio.to_thread(home_machine, request, agent_id)
    try:
        result = await dispatch_to_machine(
            request.app.state.db,
            target_machine=machine,
            kind="to_user_read",
            payload={"agent_id": agent_id},
            timeout_s=3,
            retries=0,
        )
    except ClusterOpUnreachable as exc:
        raise HTTPException(503, "The agent's machine is unavailable") from exc
    except ClusterOpFailed as exc:
        raise HTTPException(422, "The workspace document could not be read") from exc
    return ToUserDocument.model_validate(result)


@router.get("/api/agents/{agent_id}/to-user/document", response_class=HTMLResponse)
async def to_user_document(agent_id: int, request: Request) -> HTMLResponse:
    document = await get_to_user(agent_id, request)
    return html_document_response(document)


def html_document_response(document: ToUserDocument) -> HTMLResponse:
    """Apply the same trust boundary to embedded and directly-opened documents."""
    if document.content is None or document.format != "html":
        raise HTTPException(404, "No HTML document")
    # The response sandbox also applies when opened directly in a new tab.
    # Scripts have an opaque origin, no Ava cookies/storage or gateway tokens.
    return HTMLResponse(
        document.content + NAVIGATION_BRIDGE,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "sandbox allow-scripts allow-forms allow-popups; "
                "default-src 'none'; script-src 'unsafe-inline' https:; "
                "style-src 'unsafe-inline' https:; img-src https: http: data:; "
                "font-src https: data:; media-src https:; frame-src https: http:; "
                "connect-src 'none'; form-action 'none'; base-uri 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
