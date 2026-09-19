"""本机 ffmpeg 的总数闸门。

合成、切段、抽帧、压缩，四条路都在起 ffmpeg，此前各跑各的、谁也不知道谁。
2026-09-18 实测同时有 29 个 ffmpeg 在跑、合计占 842% CPU（10 核机器），
负载 120，登录要十几秒，界面处处卡顿——而那里面只有 2 个是 Part 合成，
其余 27 个全是 AI 分析在切段和转码。

所以闸门开在「本机同时能跑几个 ffmpeg」这一层，而不是各自限各自的。
没别的任务时满速跑满名额，忙的时候自动排队。

默认留两个核给界面、后端和系统。FFMPEG_PAR 可覆盖。
"""
import os
import threading

def _默认并发() -> int:
    核 = os.cpu_count() or 4
    return max(2, 核 - 2)

并发上限 = max(1, int(os.environ.get("FFMPEG_PAR", str(_默认并发()))))
_闸 = threading.BoundedSemaphore(并发上限)


class 排队:
    """with 排队(): ... 里面起 ffmpeg。

    用线程信号量而不是 asyncio 的：起 ffmpeg 的地方有一半在线程里跑
    （合成走 run_in_executor，拆解管线走自己的线程池），协程锁在那边拦不住。
    """

    def __enter__(self):
        _闸.acquire()
        return self

    def __exit__(self, *exc):
        _闸.release()
        return False
