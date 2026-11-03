FROM python:3.12-slim
WORKDIR /app
COPY services/backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY services/backend /app/services/backend
WORKDIR /app/services/backend
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
