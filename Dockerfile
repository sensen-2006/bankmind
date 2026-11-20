FROM python:3.12-slim
WORKDIR /app
COPY requirements-team.txt .
RUN pip install --no-cache-dir -r requirements-team.txt
COPY apps/api apps/api
COPY scripts/generate_demo.py scripts/generate_demo.py
COPY data/demo data/demo
COPY bankmind_data_pack/07_app_ready bankmind_data_pack/07_app_ready
RUN useradd --system bankmind && mkdir storage && chown -R bankmind storage data/demo
USER bankmind
RUN test -w data/demo && test -w data/demo/bank.csv && test -w data/demo/book.csv
ENV PYTHONDONTWRITEBYTECODE=1 BANKMIND_PUBLIC_TRIAL=true BANKMIND_DEMO=true \
    BANKMIND_STORAGE=/app/storage BANKMIND_WORK_MODE=embedded LLM_MODE=mock
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "bankmind.main:app", "--app-dir", "apps/api", "--host", "0.0.0.0", "--port", "8000"]
