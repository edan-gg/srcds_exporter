FROM python:3.7.12-alpine3.14

WORKDIR /usr/src/app

RUN apk add build-base --update --no-cache

COPY ./requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN addgroup -S exporter && adduser -S exporter -G exporter
RUN chown exporter:exporter -R .
USER exporter

COPY --chown=exporter ./main.py ./response.j2 ./

CMD ["python", "main.py"]

EXPOSE 9591
