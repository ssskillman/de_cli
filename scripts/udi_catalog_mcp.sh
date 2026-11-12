#!/usr/bin/env bash
# ~/de/scripts/udi_catalog_mcp.sh
set -euo pipefail
URL="${CATALOG_MCP_URL:-https://udi-catalog-mcp-dy3eb6kvxa-uc.a.run.app}"
PY="${UDI_APPLICATIONS:-$HOME/github/udi-applications}/catalog-mcp/.venv/bin/python"
TOKEN="$(gcloud auth print-identity-token)"
curl -fsS -H "Authorization: Bearer ${TOKEN}" "${URL}/health"
echo
CATALOG_MCP_URL="$URL" "$PY" - "$URL" <<'PY'
import asyncio, os, sys
sys.path.insert(0, os.path.expanduser("~/github/udi-applications/catalog-mcp"))
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport
import cursor_proxy
origin = sys.argv[1]
async def main():
    transport = StreamableHttpTransport(url=f"{origin}/mcp", auth=cursor_proxy.IdentityTokenAuth(origin))
    async with Client(transport) as client:
        result = await client.call_tool("search_kpis", {"q": "ARR", "limit": 1})
        row = (result.data or [None])[0]
        print(row["kpi_id"], row["kpi_name"], row["status"])
asyncio.run(main())
PY
