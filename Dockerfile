FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock.txt requirements.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt && useradd --uid 10001 --create-home portal
COPY server ./server
COPY dist ./dist
USER portal
EXPOSE 8000
CMD ["python", "-m", "server.start"]
