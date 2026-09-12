"""
Gradio 全流程管理界面入口（计划 002）。

用法：
    python cli.py webui --port 7860
    # 或直接：
    python webui.py
    # 或用 gradio 命令热重载：
    gradio webui.py

核心逻辑（仪表盘/角色管理/章节管理/TTS 试听各 Tab 的数据与操作函数）都在
src/webui_app.py 里，方便单元测试；本文件只是把它们组装成 gr.Blocks 并启动，
顶层暴露 `demo` 变量以支持 `gradio webui.py` 这种启动方式。
"""
from src.webui_app import build_app

demo = build_app()

if __name__ == "__main__":
    demo.queue().launch()
