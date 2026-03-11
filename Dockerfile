# Usa una imagen oficial de Python ligera
FROM python:3.11-slim

# Evitar la creación de archivos .pyc y forzar el buffer de impresión
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Establecemos el directorio de trabajo
WORKDIR /app

# Instala dependencias del sistema necesarias
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements
COPY requirements.txt .

# Instalar dependencias de Python
RUN pip install --no-cache-dir -r requirements.txt

# Instalar navegador Chromium mediante Playwright (incluye dependencias de OS)
RUN playwright install chromium --with-deps

# Copiar el resto del código
COPY . .

# Exponer el puerto donde corre la aplicación FastAPI
EXPOSE 8000

# Comando para arrancar el servidor web
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
