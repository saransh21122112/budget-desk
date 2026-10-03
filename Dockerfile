FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 BUDGET_DB=/data/budget.db
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && mkdir -p /data
COPY main.py tursodb.py ./
COPY static static
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
