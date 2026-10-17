FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 chempro
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py entrypoint.py ./
RUN mkdir /app/data && chown -R chempro:chempro /app
USER chempro
EXPOSE 8080
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:8080", "--access-logfile", "-", "--error-logfile", "-", "entrypoint:app"]
