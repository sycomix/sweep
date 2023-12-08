from sweepai.core.documentation import DOCS_ENDPOINTS


def extract_docs_links(content: str) -> list[str]:
    return [
        url
        for framework, url in DOCS_ENDPOINTS.items()
        if (
            framework.lower() in content.lower()
            or framework.lower().replace(" ", "") in content.lower()
        )
    ]


print(extract_docs_links("I want to check the modal labs docs"))
