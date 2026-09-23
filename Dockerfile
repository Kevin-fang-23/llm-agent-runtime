FROM python:3.11-slim

# A10：PID 1 用 dumb-init —— 裸 uvicorn 当 1 号进程时对 SIGTERM 的转发/僵尸回收
# 都不可靠，compose `down` / k8s 滚动更新会等满 kill_timeout 再 SIGKILL（非优雅排空）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends dumb-init \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY app ./app
COPY web ./web
COPY sandbox ./sandbox

# A10：默认以非 root 运行。容器内被攻破时，root 意味着可直接摸 /var/run/docker.sock
# （worker 服务挂载了它）拿宿主权限；非 root 把这条链先掐断。
# data/ 承载 SQLite 业务库/checkpoint/凭证文件/工作区（见 config.py 的 PROJECT_ROOT 路径），
# 属主必须给到运行用户，否则升级镜像后第一次写库即崩。
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
ENTRYPOINT ["dumb-init", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
