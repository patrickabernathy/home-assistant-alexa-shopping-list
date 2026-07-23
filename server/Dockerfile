FROM alpine:3.20

# Stay on the pinned 3.20 repos (the old file repointed apk at *edge*, defeating
# the pin). Chromium/chromedriver are gone: the server drives Amazon's JSON API
# with requests only; browser login lives entirely in client/.
RUN apk add --no-cache python3 py3-pip

RUN mkdir /config
RUN mkdir /server

COPY alexa.py /server/alexa.py
COPY server.py /server/server.py
COPY requirements.txt /server/requirements.txt

ENV ASL_CONFIG_PATH="/config/"
# Unbuffered so print-based logging shows up in `docker logs` immediately.
ENV PYTHONUNBUFFERED=1

RUN pip3 install --no-cache-dir --break-system-packages -r /server/requirements.txt
RUN rm /server/requirements.txt

EXPOSE 4000

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s \
  CMD python3 -c "import socket; socket.create_connection(('127.0.0.1', 4000), timeout=5).close()"

ENTRYPOINT ["python3","/server/server.py"]
