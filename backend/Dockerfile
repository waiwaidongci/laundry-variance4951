FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 可选的 CBC/MILP 加速后端：装不上不阻断镜像构建，排坊会自动回退到纯标准库的确定性分支定界。
RUN pip install --no-cache-dir PuLP==3.3.2 || true
COPY src src
COPY scripts scripts
COPY migrations migrations
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "src.main"]
