@echo off
rem DeepSeek Harness (dsh), installed in desktop\third_party\dsh. Its home (config, keys, sessions) is desktop\third_party\dsh-home,
rem so %USERPROFILE%\.dsh is never touched. The pet is wired in as an MCP server there (cordis.patch.yml).
rem   start-dsh.cmd web                        browser UI at http://127.0.0.1:3080
rem   start-dsh.cmd headless "task"            answer one task and exit
rem DeepSeek API key: put DEEPSEEK_API_KEY=... into desktop\third_party\dsh-home\.env, or enter it on the web UI's models page.
rem dsh-run.mjs instead of the package's own bin: dsh wants Node >= 22.19 and its bin does nothing on older Node (see that file).
rem third_party\node22 is a portable Node 22.23.4 (2026-10-25): with the system's 22.13 dsh can't run any command (its job runner
rem also checks import.meta.main). Use it when it's there.
set "DSH_HOME=%~dp0third_party\dsh-home"
set "DSH_NODE=node"
if exist "%~dp0third_party\node22\node.exe" set "DSH_NODE=%~dp0third_party\node22\node.exe"
"%DSH_NODE%" "%~dp0third_party\dsh-run.mjs" %*
