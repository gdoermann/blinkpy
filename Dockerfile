FROM python:3.8.20-alpine
LABEL maintainer="Kevin Fronczak <kfronczak@gmail.com>"

VOLUME /media

RUN python -m pip install --upgrade pip
RUN pip3 install blinkpy

COPY blinkapp/ .

ENTRYPOINT ["python", "./blinkapp.py"]
CMD []
