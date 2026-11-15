"""Demonstrate retries, idempotency, timeouts, and compensation for side effects."""


# RequestTimeoutError 模拟客户端等待超时，但服务端动作可能已经完成。
class RequestTimeoutError(Exception):
    """The client did not receive a response in time."""


# IdempotencyConflictError 表示同一个幂等键被用于不同内容。
class IdempotencyConflictError(Exception):
    """The same idempotency key was reused with different content."""


# ResilientExecutor 管理会发送消息或创建订单的外部副作用。
class ResilientExecutor:
    # 初始化用于模拟外部状态的存储。
    def __init__(self):
        # side_effects 保存已经发生的外部动作。
        self.side_effects = {}
        # send_attempts 记录发送动作被尝试的次数。
        self.send_attempts = 0

    # 发送行程；第一次执行会模拟“动作已发生，但响应超时”。
    def _perform_send(self, request, attempt):
        # idempotency_key 是识别同一业务请求的唯一键。
        idempotency_key = request["idempotency_key"]
        # content 是准备发送的行程内容。
        content = request["content"]

        if idempotency_key in self.side_effects:
            # existing 是第一次已经保存的外部动作。
            existing = self.side_effects[idempotency_key]
            if existing["content"] != content:
                raise IdempotencyConflictError("幂等键已被不同内容使用")
            return {
                "receipt_id": existing["receipt_id"],
                "status": existing["status"],
                "duplicate": True,
            }

        # receipt_id 是本次外部动作生成的唯一回执编号。
        receipt_id = f"receipt-{len(self.side_effects) + 1:03d}"
        # record 是外部系统真实保存的动作记录。
        record = {
            "receipt_id": receipt_id,
            "content": content,
            "status": "sent",
        }
        self.side_effects[idempotency_key] = record

        if attempt == 1:
            raise RequestTimeoutError("发送成功但响应超时")

        return {
            "receipt_id": receipt_id,
            "status": "sent",
            "duplicate": False,
        }

    # 使用同一个幂等键重试发送，避免重复创建外部动作。
    def send_itinerary(self, request, max_attempts=2):
        if max_attempts < 1:
            raise ValueError("最大尝试次数必须大于 0")

        # attempt 是当前发送尝试序号。
        for attempt in range(1, max_attempts + 1):
            self.send_attempts += 1
            try:
                return self._perform_send(request, attempt)
            except RequestTimeoutError as error:
                print(f"第 {attempt} 次发送：{error}")
                if attempt == max_attempts:
                    raise

    # 补偿已经发送的行程，重复补偿仍返回同一个取消结果。
    def compensate(self, receipt_id):
        # matching_keys 找到属于指定回执的幂等键。
        matching_keys = [
            key
            for key, record in self.side_effects.items()
            if record["receipt_id"] == receipt_id
        ]
        if not matching_keys:
            raise ValueError(f"没有找到回执：{receipt_id}")

        # key 是命中回执对应的幂等键。
        key = matching_keys[0]
        # record 是准备被补偿的外部动作。
        record = self.side_effects[key]
        # already_cancelled 记录补偿前动作是否已经取消。
        already_cancelled = record["status"] == "cancelled"
        record["status"] = "cancelled"
        return {
            "receipt_id": receipt_id,
            "status": "cancelled",
            "already_cancelled": already_cancelled,
        }


# 演示同一幂等键被不同内容复用时的拒绝路径。
def demo_conflict():
    # executor 保存冲突演示产生的外部动作。
    executor = ResilientExecutor()
    # original_request 是第一次发送的旅行行程。
    original_request = {
        "idempotency_key": "trip-conflict-demo",
        "content": "杭州三日拍照行程：西湖、九溪烟树、南宋御街。",
    }
    # conflicting_request 复用了幂等键，却修改了业务内容。
    conflicting_request = {
        "idempotency_key": "trip-conflict-demo",
        "content": "杭州两日博物馆行程。",
    }

    executor.send_itinerary(original_request)
    try:
        executor.send_itinerary(conflicting_request)
    except IdempotencyConflictError as error:
        print(f"幂等冲突：{error}")
    print("实际外部动作：", len(executor.side_effects), "条")


# 运行可靠执行器演示；scenario 用于选择正常流程或幂等冲突诊断。
def main(scenario=None):
    if scenario == "--conflict":
        demo_conflict()
        return

    # request 是待发送的三日旅行行程。
    request = {
        "idempotency_key": "trip-2026-11-15-send-v1",
        "content": "杭州三日拍照行程：西湖、九溪烟树、南宋御街。",
    }
    # executor 保存本课演示的外部动作状态。
    executor = ResilientExecutor()

    # receipt 是重试后拿到的发送回执。
    receipt = executor.send_itinerary(request)
    print("发送回执：", receipt)
    print("实际外部动作：", len(executor.side_effects), "条")
    print("发送尝试：", executor.send_attempts, "次")

    # compensated 是第一次补偿的结果。
    compensated = executor.compensate(receipt["receipt_id"])
    print("第一次补偿：", compensated)
    # compensated_again 是重复补偿的结果。
    compensated_again = executor.compensate(receipt["receipt_id"])
    print("重复补偿：", compensated_again)


if __name__ == "__main__":
    main()
