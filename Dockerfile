# Usa una imagen oficial de Python
FROM python:3.11-slim

# Establece el directorio de trabajo dentro del contenedor
WORKDIR /app

# Copia el archivo de requerimientos primero para aprovechar la caché de Docker
COPY requirements.txt .

# Instala las dependencias
RUN pip install --no-cache-dir -r requirements.txt

# Copia el resto del código
COPY main.py .

# Expone el puerto que usará la aplicación (Render asigna el puerto real)
EXPOSE 10000

# Comando para ejecutar la aplicación
CMD ["python", "main.py"]
