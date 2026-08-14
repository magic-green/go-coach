"""简思围棋教室 - Flask 主应用入口。"""
import os

from flask import Flask, render_template, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

import config
from core_logger import setup_logging, LOG_FILE
from routes import bp as api_bp
from game_db import init_db

logger = setup_logging("app")

app = Flask(__name__)
# 信任 3 层反向代理：cloudflared → jsai-server(8000) → go-coach(5001)
# 这样 request.remote_addr / request.host 会按 X-Forwarded-* 自动修正
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=3, x_proto=2, x_host=2, x_port=1, x_prefix=1)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.register_blueprint(api_bp)

# 初始化棋局数据库
init_db()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/help")
def help_page():
    return render_template("help.html", version=config.APP_VERSION)


@app.route("/go-admin")
def admin_page():
    """管理后台页面。"""
    return send_from_directory(
        os.path.join(config.PROJECT_ROOT, "static"),
        "admin.html",
    )


if __name__ == "__main__":
    status = config.engine_status()
    print("=" * 60)
    print(f"  简思围棋教室  v{config.APP_VERSION}")
    print(f"  引擎模式: {status['mode'].upper()}  -  {status['message']}")
    print(f"  日志文件: {LOG_FILE}")
    print(f"  访问地址: http://localhost:{config.PORT}")
    print("=" * 60)
    logger.info(
        "启动服务 PID=%s HOST=%s PORT=%s engine_mode=%s debug=%s",
        os.getpid(), config.HOST, config.PORT, status["mode"], config.DEBUG,
    )
    logger.info("引擎信息: %s", status["message"])
    logger.info("KataGo 检测: exe=%s model=%s",
                config.katago_available(),
                "ok" if os.path.isfile(config.KATAGO_MODEL) else "missing")
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
