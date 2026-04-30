"""In-process MCP server layer.

This package owns tool *implementations* and elicitation requests. The
agent (the MCP client) calls into this layer through the curated tool
wrappers in `app/ai_chat_layer/tools/`; the tools themselves delegate
their data access here, and may decide to surface an elicitation form
to the user mid-execution by raising or returning an
`ElicitationRequired` payload.

Why a separate package even though we run in-process today:
  * Keeps the boundary explicit so we can swap to a subprocess MCP
    server (via the MCP Python SDK) without touching call-sites.
  * Concentrates schema introspection and elicitation specs in one
    place — clients never compose elicitation forms; only the server
    layer does.
"""
