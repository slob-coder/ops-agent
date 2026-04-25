"""
ChannelBackend — 交互通道后端的抽象接口

ops-agent 的 HumanChannel 通过多个 Backend 并行输出，
共享 inbox / approval_queue / interrupted 等交互原语。
"""

from __future__ import annotations

import queue
import threading
from abc import ABC, abstractmethod


class ChannelBackend(ABC):
    """交互通道后端接口。

    每个 Backend 负责一种人机交互方式（终端、飞书、Slack 等）。
    HumanChannel 在初始化时注入共享的队列和事件，Backend 往里塞消息即可。
    """

    @abstractmethod
    def start(self, inbox: queue.Queue, approval_queue: queue.Queue,
              interrupted: threading.Event) -> None:
        """启动后端。

        Args:
            inbox: 人类消息队列，Backend 收到输入后 put 进去
            approval_queue: 批准回复队列，Backend 收到 y/n 后 put 进去
            interrupted: 中断事件，收到人类输入时 set
        """

    @abstractmethod
    def send(self, message: str, urgency: str = "info") -> None:
        """输出消息给人类（对应 HumanChannel.say）"""

    @abstractmethod
    def send_log(self, message: str, urgency: str = "observe") -> None:
        """低优先级日志输出（对应 HumanChannel.log）"""

    @abstractmethod
    def send_cmd_log(self, cmd: str) -> None:
        """命令执行日志（对应 HumanChannel.cmd_log）"""

    @abstractmethod
    def request_approval(self, action_description: str) -> None:
        """展示需要批准的操作（非阻塞，只展示，等待人类通过 inbox/approval_queue 回复）"""

    def set_waiting_approval(self, value: bool) -> None:
        """通知 backend 当前是否在等待批准状态。Backend 据此路由输入。"""
        pass  # 默认空实现，需要的 backend 自行覆盖

    @abstractmethod
    def stop(self) -> None:
        """关闭后端"""
