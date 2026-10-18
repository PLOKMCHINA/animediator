# AniMediator 运行镜像。
#
# 引擎只用标准库，所以构建过程不需要 pip、不需要联网拉依赖，
# 只是把源码放进镜像并造一个入口脚本。这决定了镜像构建在任何网络环境下都能成功。
#
# 用别的 Python 基座：
#   docker build --build-arg PYTHON_VERSION=3.13 .

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim-bookworm

LABEL org.opencontainers.image.title="AniMediator" \
      org.opencontainers.image.description="Anime library organizer" \
      org.opencontainers.image.source="https://github.com/PLOKMCHINA/animediator" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    TZ=Asia/Shanghai

WORKDIR /app
COPY src/ /app/src/

# 入口脚本自己造，不用 pip install —— 少一步依赖，镜像构建就不受网络影响
RUN printf '#!/bin/sh\nexec python3 -m animediator "$@"\n' > /usr/local/bin/animediator \
    && chmod 0755 /usr/local/bin/animediator

# 非 root 运行。1000 是常见宿主用户，挂载时权限不容易打架；
# 宿主的 UID 不是 1000 时用 --user $(id -u):$(id -g) 覆盖。
RUN groupadd -g 1000 ami \
    && useradd -m -u 1000 -g ami -s /bin/sh ami \
    && mkdir -p /config /data /vfs /media \
    && chown -R ami:ami /config /data /vfs

USER ami
WORKDIR /config

ENTRYPOINT ["/usr/local/bin/animediator"]
CMD ["--help"]
