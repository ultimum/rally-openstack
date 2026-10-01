FROM ubuntu:24.04

MAINTAINER Michal Arbet

ARG NO_PROXY
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG GITHUB_TOKEN

RUN apt update \
    && apt install -y git curl python3-pip dumb-init vim qemu-utils iputils-ping

RUN echo "cachebust=${CACHEBUST}" \
    && cd /opt  \
    && mkdir -p /etc/rally \
    && git clone https://${GITHUB_TOKEN}@github.com/ultimum/rally.git \
    && git clone https://${GITHUB_TOKEN}@github.com/ultimum/rally-openstack.git

RUN curl -L https://raw.githubusercontent.com/openstack/requirements/refs/heads/master/upper-constraints.txt \
  -o /opt/upper-constraints.txt

RUN pip3 install --break-system-packages -e /opt/rally /opt/rally-openstack -c /opt/upper-constraints.txt \
    && mkdir -p /etc/ultimum

# Use the Ultimum tasks from this build context alongside their wrappers.
COPY tasks/ultimum/ /opt/rally-openstack/tasks/ultimum/
COPY ultimum/rally-* /usr/local/bin/
COPY ultimum/ultimum-* /usr/local/bin/
COPY ultimum/rally.conf /opt/ultimum/rally.conf
COPY ultimum/ultimum_validation/*.py /opt/ultimum/ultimum_validation/
COPY ultimum/plugins/*.py /opt/ultimum/plugins/
COPY ultimum/ultimum.yaml.example ultimum/SCENARIOS.rst /opt/ultimum/
COPY ultimum/runner-requirements.txt /opt/ultimum/runner-requirements.txt
RUN pip3 install --break-system-packages -r /opt/ultimum/runner-requirements.txt -c /opt/upper-constraints.txt \
    && ln -sf /etc/ultimum/rally.conf /etc/rally/rally.conf
RUN chmod +x /usr/local/bin/rally-* /usr/local/bin/ultimum-*
RUN echo 'source /usr/local/bin/rally-welcome' >> /etc/bash.bashrc

ENV HOME=/data/ultimum/home
ENV PYTHONUNBUFFERED=1
WORKDIR /data/ultimum/home

USER root

ENTRYPOINT ["dumb-init", "--single-child", "--"]

CMD ["rally-start"]
