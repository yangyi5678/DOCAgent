"""Redis 基础设施封装。

本文件只负责 Redis 相关的底层能力，
包括 Redis client 创建、
Redis 分布式锁、
Redis 队列。
业务层的 scheduler 不直接关心 Redis key 如何组织，也不直接创建Redis 客户端。

redis 是scheduler和worker之间的节点任务的封装与管理； 

scheduler
  -> RedisQueue.push()   # 把 ready node 放进队列
  -> RedisLock.acquire() # 执行前加锁

worker
  -> RedisQueue.pop()    # 拿 ready node
  -> RedisLock.acquire() # 抢执行权
  -> redis.hset()        # 保存结果

edis_infra.py = Redis client + Redis lock + Redis queue 的统一封装。


edis 在你这个 agent 架构里有三个作用：
1. 任务队列
2. 分布式锁
3. 状态/结果存储
1. Redis 作为任务队列
scheduler 找到可以执行的节点后，会把 node 推进 Redis queue：
queue.push(task_id, node)
worker 从队列里取任务：
queue.pop(task_id)
这样 scheduler 和 worker 就不需要在同一个进程里。
可以变成：
scheduler 进程
  -> Redis queue
      -> worker-1
      -> worker-2
      -> worker-3
所以 Redis 让你可以横向扩展多个 worker。
2. Redis 作为分布式锁
如果多个 worker 同时抢同一个节点，需要防止重复执行。
Redis lock 用的是：
SET lock:n1 worker-1 NX EX 30
意思是：
只有锁不存在时才能写入
30 秒后自动过期
这样 worker-1 抢到 n1 后，worker-2 就不能重复执行 n1。
3. Redis 存执行结果
worker 执行完节点后，可以把结果写到 Redis：
result:{task_id}
例如：
{
    "n1": {"status": "success", "result": ...},
    "n2": {"status": "failed", "error": ...}
}
这样 scheduler 或其他进程可以读取结果并继续调度。
如果不是分布式，还需要 Redis 吗？
不一定。
如果你只是单机单进程 demo，可以不用 Redis，直接用 Python 内存结构：
queue = []
locks = set()
results = {}
比如：
planner -> scheduler -> dispatcher
全在一个进程里跑，Redis 不是必须的。
什么时候 Redis 有价值
当你想要这些能力时，Redis 就有用：
多个 worker 并行执行
scheduler 和 worker 分开部署
任务中断后可以恢复
长任务异步执行
多台机器共享任务队列
防止重复执行节点
保存中间执行结果
所以答案是：
是的，Redis 主要是为了分布式/多 worker/异步调度。
单机 demo 可以不用，但生产型 agent 通常需要类似 Redis 的队列和锁。
"""

from __future__ import annotations

import json
import os
from typing import Any

from sandbox.redaction import redact

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None


class RedisClientFactory:
    """Redis 客户端工厂。

    功能：
        统一创建 Redis client，避免业务代码到处散落 host、port、
        decode_responses 等连接配置。

    输入：
        初始化时传入 Redis 连接配置。

    输出：
        ``create`` 方法返回一个可用的 Redis client。
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        db: int | None = None,
        decode_responses: bool = True,
    ):
        """保存 Redis 连接配置。

        参数：
            host: Redis 服务地址。
            port: Redis 服务端口。
            db: Redis 数据库编号。
            decode_responses: 是否把 Redis 返回值自动解码为字符串。

        输出：
            无显式返回值。配置会保存在实例属性中。
        """
        self.host = host or os.getenv("REDIS_HOST", "localhost")
        self.port = port if port is not None else int(os.getenv("REDIS_PORT", "6379"))
        self.db = db if db is not None else int(os.getenv("REDIS_DB", "0"))
        self.decode_responses = decode_responses

    def create(self):
        """创建 Redis client。

        输入：
            使用实例初始化时保存的连接配置。

        输出：
            返回 ``redis.Redis`` client。

        功能：
            延迟导入并创建 Redis client。如果环境没有安装 ``redis`` 包，
            会抛出清晰的运行时错误。
        """
        if redis is None:
            raise RuntimeError("缺少 redis 依赖，请先安装: pip install redis")

        return redis.Redis(
            host=self.host,
            port=self.port,
            db=self.db,
            decode_responses=self.decode_responses,
        )


class RedisLock:
    """Redis 分布式锁 封装。

    功能：
        使用 ``SET key value NX EX`` 语义为任务节点加锁，避免多个 worker
        重复执行同一个节点。
    """

    def __init__(self, redis_client, prefix: str = "lock", ttl_seconds: int = 30):
        """初始化 Redis lock 封装。

        参数：
            redis_client: 已创建好的 Redis client。
            prefix: 锁 key 前缀。
            ttl_seconds: 锁过期时间，单位秒。

        输出：
            无显式返回值。Redis client 和锁配置会保存到实例中。
        """
        self.redis = redis_client
        self.prefix = prefix
        self.ttl_seconds = ttl_seconds

    def acquire(self, resource_id: str, owner_id: str):
        """申请资源锁。

        参数：
            resource_id: 需要加锁的资源 ID，例如 DAG 节点 ID。
            owner_id: 锁持有者 ID，例如 worker ID。

        输入：
            资源 ID 和持有者 ID。

        输出：
            Redis ``set`` 的返回值。成功加锁返回真值；锁已存在返回假值。

        功能：
            写入 ``{prefix}:{resource_id}``，并设置 ``nx=True`` 和过期时间。
        """
        return self.redis.set(
            f"{self.prefix}:{resource_id}",
            owner_id,
            nx=True,
            ex=self.ttl_seconds,
        )

    def release(self, resource_id: str, owner_id: str | None = None) -> int:
        """释放资源锁。

        参数：
            resource_id: 需要释放锁的资源 ID。
            owner_id: 可选锁持有者 ID。传入时只有当前 owner 匹配才删除。

        输入：
            节点 ID，以及可选 worker ID。

        输出：
            Redis ``delete`` 的结果。删除成功通常返回 1，未删除返回 0。

        功能：
            worker 执行完节点后释放锁，避免 retry 节点重新入队后被旧锁挡住。
            如果传入 owner_id，会先检查锁值，防止误删其他 worker 刚抢到的新锁。
        """
        key = f"{self.prefix}:{resource_id}"
        if owner_id is not None and self.redis.get(key) != owner_id:
            return 0
        return self.redis.delete(key)


class RedisQueue:
    """Redis 队列 封装。

    功能：
        基于 Redis list 提供简单的 ``push`` / ``pop`` 操作。队列按
        ``task_id`` 隔离，适合 scheduler 存放每个任务的就绪节点。
    """

    def __init__(self, redis_client, prefix: str = "queue"):
        """初始化 Redis queue 封装。

        参数：
            redis_client: 已创建好的 Redis client。
            prefix: 队列 key 前缀。

        输出：
            无显式返回值。Redis client 和 key 前缀会保存到实例中。
        """
        self.redis = redis_client
        self.prefix = prefix

    def key(self, task_id: str) -> str:
        """生成某个任务对应的 Redis 队列 key。

        参数：
            task_id: 当前任务或运行实例 ID。

        输出：
            返回形如 ``queue:{task_id}`` 的 Redis key。
        """
        return f"{self.prefix}:{task_id}"

    def push(self, task_id: str, payload: dict[str, Any]) -> int:
        """将一个节点 payload 推入任务队列。

        参数：
            task_id: 当前任务或运行实例 ID。
            payload: 要入队的节点字典。

        输入：
            任务 ID 和可 JSON 序列化的节点数据。

        输出：
            返回 Redis ``rpush`` 后队列长度。

        功能：
            将节点字典序列化为 JSON 字符串，并追加到队列尾部。
        """
        return self.redis.rpush(
            self.key(task_id),
            json.dumps(payload, ensure_ascii=False),
        )

    def pop(self, task_id: str) -> dict[str, Any] | None:
        """从任务队列弹出一个节点 payload。

        参数：
            task_id: 当前任务或运行实例 ID。

        输入：
            任务 ID。

        输出：
            如果队列中有数据，返回反序列化后的节点字典；如果队列为空，
            返回 ``None``。

        功能：
            从队列头部弹出一条 JSON 字符串，并还原为 Python 字典。
        """
        raw = self.redis.lpop(self.key(task_id))
        if raw is None:
            return None
        return json.loads(raw)

    def clear(self, task_id: str) -> int:
        """清空某个任务的 ready queue。"""
        return self.redis.delete(self.key(task_id))


class RedisReadyQueue:
    """Global ready-node queue shared by all tasks and pool workers."""

    def __init__(self, redis_client, key: str = "queue:ready"):
        self.redis = redis_client
        self._key = key

    def key(self) -> str:
        """Return the global ready queue key."""
        return self._key

    def push(self, task_id: str, node: dict[str, Any]) -> int:
        """Push a task-scoped node envelope into the global ready queue."""
        payload = {
            "task_id": task_id,
            "node": node,
        }
        return self.redis.rpush(
            self.key(),
            json.dumps(payload, ensure_ascii=False),
        )

    def pop(self) -> dict[str, Any] | None:
        """Pop one task/node envelope from the global ready queue."""
        raw = self.redis.lpop(self.key())
        if raw is None:
            return None
        return json.loads(raw)

    def remove_task(self, task_id: str) -> int:
        """Remove queued envelopes that belong to one task.

        Redis lists do not support deleting by JSON field, so this rebuilds the
        list while preserving envelopes for other tasks.
        """
        key = self.key()
        raw_items = self.redis.lrange(key, 0, -1)
        if not raw_items:
            return 0

        kept: list[str] = []
        removed = 0
        for raw in raw_items:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                kept.append(raw)
                continue

            if payload.get("task_id") == task_id:
                removed += 1
            else:
                kept.append(raw)

        pipe = self.redis.pipeline()
        pipe.delete(key)
        if kept:
            pipe.rpush(key, *kept)
        pipe.execute()
        return removed


class RedisResultStore:
    """Redis 节点结果存储封装。

    功能：
        worker 把节点执行结果写入 ``result:{task_id}``；scheduler 轮询这个 hash，
        读取新结果并据此推进 DAG。
    """

    def __init__(self, redis_client, prefix: str = "result"):
        """初始化结果存储。

        参数：
            redis_client: 已创建好的 Redis client。
            prefix: 结果 hash 的 key 前缀。

        输出：
            无显式返回值。Redis client 和 key 前缀会保存到实例中。
        """
        self.redis = redis_client
        self.prefix = prefix

    def key(self, task_id: str) -> str:
        """生成某个任务对应的结果 hash key。"""
        return f"{self.prefix}:{task_id}"

    def save(self, task_id: str, node_id: str, result: dict[str, Any]) -> int:
        """保存一个节点执行结果。"""
        return self.redis.hset(
            self.key(task_id),
            node_id,
            json.dumps(redact(result), ensure_ascii=False),
        )

    def get(self, task_id: str, node_id: str) -> dict[str, Any] | None:
        """读取一个节点执行结果。"""
        raw = self.redis.hget(self.key(task_id), node_id)
        if raw is None:
            return None
        return json.loads(raw)

    def all(self, task_id: str) -> dict[str, dict[str, Any]]:
        """读取某个任务下所有节点执行结果。"""
        raw_results = self.redis.hgetall(self.key(task_id))
        return {
            node_id: json.loads(raw)
            for node_id, raw in raw_results.items()
        }

    def clear(self, task_id: str) -> int:
        """清空某个任务的结果 hash。"""
        return self.redis.delete(self.key(task_id), f"{self.prefix}_attempt:{task_id}")

    def next_attempt(self, task_id: str, node_id: str) -> int:
        """获取某个节点下一次执行 attempt 编号。

        参数：
            task_id: 当前任务 ID。
            node_id: 节点 ID。

        输出：
            从 1 开始递增的 attempt 编号。

        功能：
            同一个节点 retry 时会多次写回结果，attempt 让 scheduler 能区分
            ``n1`` 的第 1 次失败和第 2 次失败。
        """
        return int(self.redis.hincrby(f"{self.prefix}_attempt:{task_id}", node_id, 1))


class RedisTaskStore:
    """Redis 任务元数据存储封装。

    功能：
        保存 scheduler 的 DAG、入度表、重试计数、完成节点等运行时状态。
        这让 scheduler/worker 分进程后仍然可以通过 Redis 共享任务状态。
    """

    def __init__(self, redis_client, prefix: str = "task"):
        """初始化任务状态存储。"""
        self.redis = redis_client
        self.prefix = prefix

    def key(self, task_id: str) -> str:
        """生成某个任务对应的 metadata hash key。"""
        return f"{self.prefix}:{task_id}"

    def save_field(self, task_id: str, field: str, value: Any) -> int:
        """保存一个任务状态字段。"""
        return self.redis.hset(
            self.key(task_id),
            field,
            json.dumps(value, ensure_ascii=False),
        )

    def load_field(self, task_id: str, field: str, default: Any = None) -> Any:
        """读取一个任务状态字段。"""
        raw = self.redis.hget(self.key(task_id), field)
        if raw is None:
            return default
        return json.loads(raw)

    def clear(self, task_id: str) -> int:
        """清空某个任务的 metadata。"""
        return self.redis.delete(self.key(task_id))


class RedisContextStore:
    """Redis 多轮上下文存储封装。

    功能：
        按 ``session_id`` 保存一段多轮会话的上下文快照。它和
        ``RedisTaskStore`` 的区别是：task 是一次 DAG 执行，context 是跨多轮
        对话长期存在的会话记忆。
    """

    def __init__(self, redis_client, prefix: str = "context"):
        """初始化上下文存储。"""
        self.redis = redis_client
        self.prefix = prefix

    def key(self, session_id: str) -> str:
        """生成某个 session 对应的上下文 key。"""
        return f"{self.prefix}:{session_id}"

    def save(self, session_id: str, context: dict[str, Any]) -> int:
        """保存完整上下文快照。"""
        return self.redis.set(
            self.key(session_id),
            json.dumps(context, ensure_ascii=False),
        )

    def load(self, session_id: str) -> dict[str, Any] | None:
        """读取完整上下文快照。"""
        raw = self.redis.get(self.key(session_id))
        if raw is None:
            return None
        return json.loads(raw)

    def clear(self, session_id: str) -> int:
        """清空某个 session 的上下文。"""
        return self.redis.delete(self.key(session_id))
