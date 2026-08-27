FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy only requirements first for caching
COPY requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Now copy the rest of the code (changes frequently)
COPY . ./

# Runs as non-root - nothing here needs root (db/OptimizedDE.db is
# generated fresh in the container's own writable layer at startup, no
# host bind mount to worry about permissions on).
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Set the default command
CMD ["python", "./main.py"]
