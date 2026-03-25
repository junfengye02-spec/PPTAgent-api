FROM node:lts-bookworm-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && \
    apt-get install -y --fix-missing --no-install-recommends \
        ca-certificates && \
    update-ca-certificates && \
    apt-get install -y --no-install-recommends \
        git bash curl wget unzip g++ locales \
        chromium \
        fonts-liberation libappindicator3-1 libasound2 \
        libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
        libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 \
        libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 xdg-utils \
        fonts-dejavu fonts-noto fonts-noto-cjk fonts-noto-cjk-extra \
        fonts-noto-color-emoji fonts-freefont-ttf fonts-urw-base35 \
        fonts-roboto fonts-wqy-zenhei fonts-wqy-microhei \
        fonts-arphic-ukai fonts-arphic-uming \
        imagemagick poppler-utils \
        docker.io && \
    fc-cache -f && \
    rm -rf /var/lib/apt/lists/*

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && locale-gen
ENV LANG=en_US.UTF-8 LANGUAGE=en_US:en LC_ALL=en_US.UTF-8

WORKDIR /app

COPY PPTAgent/ ./PPTAgent/

RUN npm install --prefix PPTAgent/deeppresenter/html2pptx --ignore-scripts && \
    npm exec --prefix PPTAgent/deeppresenter/html2pptx playwright install chromium

ENV PATH="/opt/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV="/opt/.venv"

RUN uv venv --python 3.13 $VIRTUAL_ENV && \
    uv pip install -e ./PPTAgent/

COPY requirements_api.txt .
RUN uv pip install -r requirements_api.txt

COPY api_server.py metaso_search.py ./

RUN mkdir -p /app/outputs /app/downloads

EXPOSE 8000

CMD ["python", "api_server.py"]
