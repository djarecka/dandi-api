FROM python:3.9-slim
# Install system librarires for Python packages:
# * psycopg2
RUN apt-get update && \
    apt-get install --no-install-recommends --yes \
    libpq-dev gcc libc6-dev git && \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Only copy the setup.py and setup.cfg, it will still force all install_requires to be installed,
# but find_packages() will find nothing (which is fine). When Docker Compose mounts the real source
# over top of this directory, the .egg-link in site-packages resolves to the mounted directory
# and all package modules are importable.
COPY ./setup.cfg /opt/django-project/setup.cfg
COPY ./setup.py /opt/django-project/setup.py

# Copy git folder for setuptools_scm
COPY ./.git/ /opt/django-project/.git/

RUN pip install --editable /opt/django-project[dev]

# Use a directory name which will never be an import name, as isort considers this as first-party.
WORKDIR /opt/django-project
