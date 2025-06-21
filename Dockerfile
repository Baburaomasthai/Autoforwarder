FROM python:3.9-slim

# 🛠 Install compiler for tgcrypto extension
RUN apt-get update && apt-get install -y gcc build-essential && apt-get clean

WORKDIR /app
COPY . /app

# Install dependencies if requirements.txt exists
RUN if [ -f "requirements.txt" ]; then pip install --no-cache-dir -r requirements.txt; fi

CMD ["python", "bot.py"]
