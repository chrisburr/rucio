# Copyright 2021 CERN
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Authors:
# - Benedikt Ziemons <benedikt.ziemons@cern.ch>, 2021

FROM docker.io/fedora:32
ARG PYTHON

RUN test "x${PYTHON}" = "x3.8" && \
    dnf update -y && \
    dnf install -y which findutils gridsite libaio memcached httpd mod_ssl python3-pip python3-mod_wsgi python3-gfal2 sqlite gcc \
            python3-devel python3-kerberos krb5-devel libxml2-devel xmlsec1-devel xmlsec1-openssl-devel libtool-ltdl-devel && \
    alternatives --install /usr/bin/python python /usr/bin/python3.8 1 && \
    alternatives --install /usr/bin/python3 python3 /usr/bin/python3.8 1 && \
    python -m pip --no-cache-dir install --upgrade pip && \
    python -m pip --no-cache-dir install --upgrade setuptools wheel && \
    dnf clean all

WORKDIR /usr/local/src/rucio

COPY etc etc

RUN mkdir -p /var/log/rucio/trace && \
    chmod -R 777 /var/log/rucio && \
    cp etc/certs/hostcert_rucio.pem /etc/grid-security/hostcert.pem && \
    cp etc/certs/hostcert_rucio.key.pem /etc/grid-security/hostkey.pem && chmod 0400 /etc/grid-security/hostkey.pem && \
    cp etc/docker/test/extra/httpd.conf /etc/httpd/conf/httpd.conf && \
    cp etc/docker/test/extra/rucio.conf /etc/httpd/conf.d/rucio.conf && \
    cp etc/docker/test/extra/00-mpm.conf /etc/httpd/conf.modules.d/00-mpm.conf && \
    rm /etc/httpd/conf.d/ssl.conf /etc/httpd/conf.d/autoindex.conf /etc/httpd/conf.d/userdir.conf /etc/httpd/conf.d/welcome.conf /etc/httpd/conf.d/zgridsite.conf && \
    cp etc/certs/rucio_ca.pem etc/rucio_ca.pem && \
    cp etc/certs/ruciouser.pem etc/ruciouser.pem && \
    cp etc/certs/ruciouser.key.pem etc/ruciouser.key.pem && \
    chmod 0400 etc/ruciouser.key.pem

RUN rpm -i https://yum.oracle.com/repo/OracleLinux/OL8/oracle/instantclient21/x86_64/getPackage/oracle-instantclient-basiclite-21.1.0.0.0-1.x86_64.rpm && \
    echo "/usr/lib/oracle/21/client64/lib" > /etc/ld.so.conf.d/oracle-instantclient.conf && \
    ldconfig

# pre-install requirements
RUN python -m pip --no-cache-dir install --upgrade -r etc/pip-requires -r etc/pip-requires-client -r etc/pip-requires-test

# copy everything else except the git-dir (anything above is cache-friendly)
COPY .flake8 .pep8 .pycodestyle pylintrc setup.py setup_rucio.py setup_rucio_client.py setup_webui.py ./
COPY tools tools
COPY bin bin
COPY lib lib

# Install Rucio server + dependencies
RUN python -m pip --no-cache-dir install --upgrade .[oracle,postgresql,mysql,kerberos,dev,saml] && \
    python -m pip list

WORKDIR /opt/rucio
RUN cp -r /usr/local/src/rucio/{lib,bin,tools,etc} ./

CMD ["httpd","-D","FOREGROUND"]
