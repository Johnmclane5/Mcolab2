FROM anasty17/mltb:latest

WORKDIR /app
RUN chmod 777 /app

RUN apt-get update && apt-get install -y --no-install-recommends mediainfo && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv mltbenv

COPY requirements.txt .
RUN mltbenv/bin/pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["bash", "start.sh"]
