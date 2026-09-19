"""医美注射全程追溯的运行入口。

用法：
  python3 service.py --check      基础自检
  python3 service.py --scenario   跨门店复诊追查验收（49 项硬断言）
  python3 service.py --port 8000  健康检查服务
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE_ID = "injection-trace"
SERVICE_NAME = "医美注射全程追溯"


def health_payload():
    """返回基础服务信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """提供健康查询。"""

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps(health_payload(), ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--scenario", action="store_true",
                        help="运行跨门店复诊追查验收场景")
    args = parser.parse_args()
    if args.scenario:
        from traceability.scenario import run_scenario
        summary = run_scenario()
        print(f"\n汇总：{summary['checks']} 项断言 / "
              f"{summary['records']} 份归档病历 / "
              f"{summary['movements']} 条库存移动 / "
              f"{summary['reminders_due']} 条待处理复诊提醒")
        return
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 领域包可导入、规则册版本存在
        from traceability.rules import RULE_BOOK_VERSION
        assert RULE_BOOK_VERSION
        print(f"基础检查通过（规则册 {RULE_BOOK_VERSION}）")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
