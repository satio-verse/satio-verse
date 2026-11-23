"""服务端单调时钟. 所有埋点都用它; 浏览器经 ping/pong 对时后换算到这条时间轴."""
import time

_T0 = time.perf_counter()


def now_ms() -> float:
    return (time.perf_counter() - _T0) * 1000.0
