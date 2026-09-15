FROM python:3.12-slim
RUN pip install --no-cache-dir pandas==3.0.5 numpy==2.5.2
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1
COPY sandbox_worker.py transport.py /app/
USER 65534:65534
WORKDIR /app
ENTRYPOINT ["python", "/app/sandbox_worker.py"]
