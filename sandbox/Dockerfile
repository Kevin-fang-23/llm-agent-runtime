FROM python:3.11-slim

# 沙箱镜像刻意保持最小：无 pip 包、无非 root 外用户、无网络工具。
# 运行期由 --user 65534（nobody）+ read_only + network_disabled 加固。
# 代码由执行器经 `python -I -c <code>` 传入，本镜像不依赖任何挂载文件。
RUN mkdir -p /tmp/sandbox && chmod 777 /tmp/sandbox
WORKDIR /tmp/sandbox
CMD ["python", "-I"]
