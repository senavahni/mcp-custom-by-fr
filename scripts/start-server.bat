@echo off
rem MCP server (Streamable HTTP :4100/mcp) + agent runtime + outbox sender.
call "%~dp0env.bat"
"%PY%" scripts\run_server.py %*
