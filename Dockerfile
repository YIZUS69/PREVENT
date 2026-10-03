# Imagen oficial de Playwright: ya trae Chromium y sus dependencias
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot_preventa.py .
CMD ["python", "-u", "bot_preventa.py"]
