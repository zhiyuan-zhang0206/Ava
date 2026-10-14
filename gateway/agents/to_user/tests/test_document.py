"""HTML content is isolated even when opened outside the Ava frame."""

import pytest
from fastapi import HTTPException

from base.agents.to_user import ToUserDocument
from base.host.env.agent_slices import ToUserFormat
from gateway.agents.to_user.document import html_document_response


def test_document_is_isolated_even_when_opened_directly() -> None:
    response = html_document_response(
        ToUserDocument(
            format="html",
            content="<script>window.test = true</script>",
            version="a",
            modified_at=None,
        )
    )
    csp = response.headers["content-security-policy"]
    assert "sandbox allow-scripts allow-forms allow-popups;" in csp
    assert "allow-same-origin" not in csp
    assert "connect-src 'none'" in csp
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("format,content", [("html", None), ("markdown", "# Document")])
def test_missing_and_markdown_documents_are_not_served_as_html(
    format: ToUserFormat, content: str | None
) -> None:
    with pytest.raises(HTTPException) as error:
        html_document_response(
            ToUserDocument(format=format, content=content, version=None, modified_at=None)
        )
    assert error.value.status_code == 404
