FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py importio.py ./
COPY *.html manifest.webmanifest icon.svg ./
COPY dados_alunos.json ./

# uploads_tmp é criado em tempo de execução
ENV PORT=8080
EXPOSE 8080

CMD ["python", "app.py"]
