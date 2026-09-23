FROM python:3.11-slim

# 沙箱镜像刻意保持最小：无 pip 包、无非 root 外用户、无网络工具。
# 运行期加固在 app/executor/sandbox.py：--user 65534（nobody）+ read_only +
# tmpfs /tmp（noexec/nosuid/1777）+ network_disabled；工作目录由运行期
# working_dir="/tmp" 指定。
# A11：此处原先有 `mkdir /tmp/sandbox + chmod 777 + WORKDIR /tmp/sandbox` ——
# 全部是死配置：运行期 tmpfs 挂载直接把镜像里的 /tmp 整个盖掉，mkdir/chmod
# 的效果永远不可见，WORKDIR 同理被 working_dir 覆盖。删掉，避免"看起来有加固
# 实际不生效"的假象。
# 代码由执行器经 `python -I -c <code>` 传入，本镜像不依赖任何挂载文件。
CMD ["python", "-I"]
