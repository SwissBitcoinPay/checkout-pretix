# The end-to-end test's pretix (e2e/run.sh): pretix's own image, and the plugin installed in it
# from the wheel the build made, as pretix's documentation installs a plugin into its image. The
# wheels come from the build context alone (no index): the plugin's, the SDK's (built from sdk/,
# PyPI's in production), the SDK's dependencies at the lockfile's versions, and the test-only fake
# of the SDK (e2e/fake). Mails are written to files: the test's pretix has no mail server. Its
# nginx listens on IPv4 alone: it also listens on IPv6 in pretix's image, which some Docker hosts
# do not offer (nginx then fails to start), and the test reaches it on 127.0.0.1.
#
# pretix's documentation then runs `make production`, which builds pretix's own assets again
# (npm, its translations) and collects the static files: the plugin has no asset to build, and
# its static files are collected here.
ARG PRETIX_IMAGE
FROM ${PRETIX_IMAGE}
USER root
COPY wheels/ /tmp/wheels/
RUN pip3 install --no-cache-dir --no-index --find-links /tmp/wheels \
        pretix-sattleme pretix-sattleme-e2e \
    && rm -rf /tmp/wheels \
    && printf '\nEMAIL_BACKEND = "%s"\nEMAIL_FILE_PATH = "/data/mails"\n' \
        django.core.mail.backends.filebased.EmailBackend >> /pretix/src/production_settings.py \
    && sed -i '/listen \[::\]:80/d' /etc/nginx/nginx.conf
USER pretixuser
RUN cd /pretix/src && python3 -m pretix collectstatic --noinput
