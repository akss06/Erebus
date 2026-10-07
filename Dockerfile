# Backend image for the Erebus FastAPI API (serves precomputed data + live analysis).
# rasterio / shapely / pyproj / scikit-image ship self-contained manylinux wheels,
# so no system GDAL/GEOS is needed on top of a slim Python base.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The host (Render/Railway/Fly) injects $PORT; default to 8000 for local `docker run`.
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
