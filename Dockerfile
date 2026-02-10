FROM alpine:3.14

RUN uv sync
RUN playwright install --with-deps
RUN . .venv/bin/activate && \
    playwright install && \
    apt-get update && apt-get install -y sudo && \
    sudo apt-get install -y libevent-2.1-7t64 libgstreamer-plugins-bad1.0-0 libflite1 libavif16 gstreamer1.0-libav xvfb