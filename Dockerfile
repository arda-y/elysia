FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy only requirements first for caching
COPY requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Now copy the rest of the code (changes frequently)
COPY . ./

# Set the default command
CMD ["python", "./main.py"]
