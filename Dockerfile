# Day Trader Autopilot: Alpaca PAPER trading bot + dashboard (standard library only)
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8
WORKDIR /app
COPY . .
RUN mkdir -p /app/trader
EXPOSE 8765
CMD ["python", "deploy/run_all.py"]
