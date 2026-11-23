"""例子: 看月相. 按 2000-01-06 的新月和 29.53 天的朔望月推算, 不联网. 她自己写的 MCP 照这个样子写 (见 CONVENTION.md)."""
import datetime

from selfmcp_kit import Server

srv = Server("moon", "0.1.0")
NAMES = ["新月", "蛾眉月", "上弦月", "盈凸月", "满月", "亏凸月", "下弦月", "残月"]


@srv.tool("phase", "某一天大概是什么月相 (不写日期就是今天)",
          {"date": {"type": "string", "description": "日期, 比如 2026-10-25; 不写就是今天"}})
def phase(date: str = "") -> str:
    day = datetime.date.fromisoformat(date) if date else datetime.date.today()
    age = (day - datetime.date(2000, 1, 6)).days % 29.53
    return f"{day.isoformat()} 是{NAMES[int(age / 29.53 * 8 + 0.5) % 8]} (月龄约 {age:.0f} 天)"


if __name__ == "__main__":
    srv.run()
