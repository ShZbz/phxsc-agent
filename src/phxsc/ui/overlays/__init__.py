"""PhySc TUI 交互层浮层（overlays，batch60）：command palette / session picker / help / model picker。

统一 modal 规范（UI_DESIGN §6.10）：≤70% 宽、背景 dim（ModalScreen 自带）、
Esc 关闭（各 screen 自绑 escape→dismiss）。命令语义全部复用 cli.py 既有
handler（/sessions /cache /skill /mcp /model /thinking /voice /help 的
cli.py 实现），TUI 只做 UI 壳，禁止重写命令逻辑。
"""
