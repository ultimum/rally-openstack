FROM ubuntu:24.04

MAINTAINER Michal Arbet

ARG NO_PROXY
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG GITHUB_TOKEN

RUN apt update \
    && apt install -y git curl python3-pip dumb-init vim qemu-utils

RUN echo "cachebust=${CACHEBUST}" \
    && cd /opt  \
    && mkdir -p /etc/rally \
    && git clone https://${GITHUB_TOKEN}@github.com/ultimum/rally.git \
    && git clone https://${GITHUB_TOKEN}@github.com/ultimum/rally-openstack.git

RUN curl -L https://raw.githubusercontent.com/openstack/requirements/refs/heads/master/upper-constraints.txt \
  -o /opt/upper-constraints.txt

RUN pip3 install --break-system-packages -e /opt/rally /opt/rally-openstack -c /opt/upper-constraints.txt \
    && mkdir -p /opt/rally-workdir

COPY ultimum/rally-* /usr/local/bin/
COPY ultimum/rally.conf /etc/rally/
RUN chmod +x /usr/local/bin/rally-*
RUN echo 'source /usr/local/bin/rally-welcome' >> /root/.bashrc

WORKDIR /opt/rally-workdir

USER root

ENTRYPOINT ["dumb-init", "--single-child", "--"]

CMD ["rally-start"]
