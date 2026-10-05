FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY dealhunter ./dealhunter
RUN pip install --no-cache-dir .
COPY config ./config
ENV DEALHUNTER_CONFIG=/app/config/config.yaml PYTHONUNBUFFERED=1
VOLUME ["/app/data"]
CMD ["dealhunter", "daemon"]
