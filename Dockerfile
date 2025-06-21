FROM python:3.9-slim

# 🔧 Install required system packages (gcc + dependencies)
RUN apt-get update && apt-get install -y gcc build-essential python3-dev && apt-get clean

WORKDIR /app

COPY . /app

RUN if [ -f "requirements.txt" ]; then pip install --no-cache-dir -r requirements.txt; fi

CMD ["python", "bot.py"]
