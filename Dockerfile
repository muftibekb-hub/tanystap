FROM python:3.11-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN test -f data/profiles.json || python data/generate_profiles.py --n 200
WORKDIR /srv/app
EXPOSE 8000
CMD gunicorn -b 0.0.0.0:${PORT:-8000} -w 1 --threads 8 --timeout 60 main:app
