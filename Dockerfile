FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RESCUE_DATA_DIR=/data

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码与模板
COPY rescue ./rescue
COPY templates ./templates
COPY wsgi.py .

# 任务数据（样盘、扇区、状态、快照）写入挂载卷
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 多线程 + 多进程；快照在进程内存中，故默认单进程多线程，
# 保证同一份进度的镜像与缺失清单在同一进程内一致。
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "8", "--timeout", "120", "wsgi:app"]
